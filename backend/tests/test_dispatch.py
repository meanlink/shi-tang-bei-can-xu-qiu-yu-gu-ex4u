"""可重复的两端派发演练与端到端测试。

运行：在 backend/ 下执行 python -m pytest tests -q（使用临时 SQLite，不碰 MySQL）。
"""
import os, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault('DATABASE_URL', 'sqlite:///./test-dispatch.db')

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.config.database import Base
from src import models
from src.models import Dispatch, DispatchLeg, DispatchEvent, PartnerOrder, Job, Dataset, User
from src.services import dispatch as service
from src.services import partners
from src.utils.serialization import checksum
import time


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path}/t.db')
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        user = User(email='admin@demo.local', name='负责人', role='admin', password='x')
        session.add(user)
        session.flush()
        dataset = Dataset(name='d', source='t', records=[], checksum='x', created_at=time.time())
        session.add(dataset)
        session.flush()
        rows = [{'date': f'2026-11-{i + 1:02d}', 'weekday': '周一', 'forecast': 100, 'buffer': 8, 'plan': 108} for i in range(3)]
        job = Job(name='已批准备餐', dataset_id=dataset.id, params={}, status='completed', approved=True,
                  result={'table': {'columns': [{'key': 'date'}, {'key': 'weekday'}, {'key': 'forecast'}, {'key': 'buffer'}, {'key': 'plan'}], 'rows': rows}},
                  created_by=user.id, created_at=time.time())
        session.add(job)
        session.commit()
        yield session, user, job, Session


class Data:
    def __init__(self, job, **kw):
        self.job_id = job.id
        self.title = kw.get('title', '测试备餐单')
        self.idempotency_key = kw.get('idempotency_key')
        self.is_drill = kw.get('is_drill', False)
        self.auto_advance = kw.get('auto_advance', True)
        self.simulation = kw.get('simulation', {})


def make(db, user, job, **kw):
    return service.create(db, Data(job, **kw), user)


def get_leg(db, d, party):
    return service._leg(db, d, party)


def test_normal_both_accept_fulfilled(db):
    session, user, job, Session = db
    d = make(session, user, job)
    assert d.items == [{'date': '2026-11-01', 'weekday': '周一', 'forecast': 100, 'buffer': 8, 'portions': 108},
                       {'date': '2026-11-02', 'weekday': '周一', 'forecast': 100, 'buffer': 8, 'portions': 108},
                       {'date': '2026-11-03', 'weekday': '周一', 'forecast': 100, 'buffer': 8, 'portions': 108}]
    assert d.total_portions == 324
    service.advance(session, d)
    session.refresh(d)
    assert d.status == 'fulfilled'
    assert {l.party: l.status for l in service.legs_of(session, d)} == {'kitchen': 'accepted', 'station': 'accepted'}
    assert session.query(PartnerOrder).count() == 2


def test_accept_then_drop_response_is_unknown_not_rejected(db):
    session, user, job, Session = db
    d = make(session, user, job, simulation={'station': 'accept_drop'})
    service.advance(session, d)
    session.refresh(d)
    st = get_leg(session, d, 'station')
    assert st.status == 'unknown'
    assert d.status == 'awaiting_verification'
    # 对端其实已接单：查证后必须落实，不能当成拒单
    assert d.cancel_requested is False
    service.verify(session, d, user)
    session.refresh(d)
    assert get_leg(session, d, 'station').status == 'accepted'
    assert d.status == 'fulfilled'


def test_reject_one_side_withdraws_other(db):
    session, user, job, Session = db
    d = make(session, user, job, simulation={'kitchen': 'reject'})
    service.advance(session, d)
    session.refresh(d)
    assert d.status == 'cancelled'
    legs = {l.party: l for l in service.legs_of(session, d)}
    assert legs['kitchen'].status == 'rejected'
    assert legs['station'].status == 'cancelled'
    # 配送站对端确实被撤回
    assert partners.inquire(session, legs['station'].order_id)['status'] == 'cancelled'
    # 责任去向保留在轨迹中
    actions = [e.action for e in session.query(DispatchEvent).filter_by(dispatch_id=d.id)]
    assert 'dispatch.auto_cancel' in actions


def test_started_side_cannot_be_withdrawn_goes_manual(db):
    session, user, job, Session = db
    d = make(session, user, job, simulation={'kitchen': 'locked'})
    service.advance(session, d)
    session.refresh(d)
    assert d.status == 'fulfilled'  # 两端都接单，确实已落实
    # 再撤单：厨房已开工拒撤 → 待人工
    service.request_cancel(session, d, user, '临时停餐')
    session.refresh(d)
    assert d.status == 'manual'
    legs = {l.party: l for l in service.legs_of(session, d)}
    assert legs['kitchen'].status == 'locked'
    assert legs['station'].status == 'cancelled'
    assert d.cancel_requested_by == user.email


def test_cancel_confirmed_never_flips_back_on_late_success(db):
    session, user, job, Session = db
    d = make(session, user, job)
    service.advance(session, d)
    session.refresh(d)
    assert d.status == 'fulfilled'
    # 对方撤单回执后，又收到旧的“已接单”回执（乱序）
    service.request_cancel(session, d, user, '计划取消')
    session.refresh(d)
    assert d.status == 'cancelled'
    stale = {'status': 'accepted', 'revision': 1, 'started': False, 'reason': ''}
    st = get_leg(session, d, 'kitchen')
    service._apply_observation(session, d, st, stale, 'stale', user.email)
    session.commit()
    session.refresh(d)
    assert d.status == 'cancelled'
    assert get_leg(session, d, 'kitchen').status == 'cancelled'


def test_cancel_and_accept_concurrent_reconciles_to_partner_truth(db):
    session, user, job, Session = db
    d = make(session, user, job, auto_advance=False)
    service.advance(session, d)
    session.refresh(d)
    assert d.status == 'fulfilled'
    service.request_cancel(session, d, user, '临时停止')
    session.refresh(d)
    assert d.status == 'cancelled'
    for party in ('kitchen', 'station'):
        assert partners.inquire(session, get_leg(session, d, party).order_id)['status'] == 'cancelled'


def test_retry_send_does_not_duplicate_partner_order(db):
    session, user, job, Session = db
    d = make(session, user, job)
    service.advance(session, d)
    session.refresh(d)
    first = {l.party: l.order_id for l in service.legs_of(session, d)}
    # 再推进多次，不应新增对端订单
    for _ in range(3):
        service.advance(session, d)
    assert session.query(PartnerOrder).count() == 2
    assert {l.party: l.order_id for l in service.legs_of(session, d)} == first


def test_interruption_recovery_keeps_same_ticket_and_queries(db):
    session, user, job, Session = db
    d = make(session, user, job, auto_advance=False)
    # 模拟进程在发送前崩溃：一条腿卡在 sending
    k = get_leg(session, d, 'kitchen')
    k.status = 'sending'
    session.commit()
    ticket = d.ticket_no
    order_id = k.order_id
    assert service.recover(session) == [d.id]
    session.refresh(d)
    session.refresh(k)
    assert d.ticket_no == ticket
    assert k.order_id == order_id
    assert k.status == 'unknown'
    assert d.status == 'awaiting_verification'
    # 查证发现对端无单 → 按原单号重发，不生成第二张单
    service.verify(session, d, user)
    session.refresh(d)
    assert get_leg(session, d, 'kitchen').status == 'accepted'
    assert session.query(PartnerOrder).filter_by(order_id=order_id).count() == 1


def test_idempotency_key_returns_same_dispatch(db):
    session, user, job, Session = db
    d1 = make(session, user, job, idempotency_key='fixed-key')
    d2 = make(session, user, job, idempotency_key='fixed-key')
    assert d1.id == d2.id
    assert session.query(Dispatch).count() == 1


def test_unapproved_or_incomplete_rejected(db):
    session, user, job, Session = db
    job.approved = False
    session.commit()
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        make(session, user, job)


def test_manual_resolution_requires_note_and_keeps_trail(db):
    session, user, job, Session = db
    d = make(session, user, job, simulation={'kitchen': 'locked'})
    service.advance(session, d)
    session.refresh(d)
    service.request_cancel(session, d, user, '临时停餐')
    session.refresh(d)
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        service.resolve_manual(session, d, user, '')
    service.resolve_manual(session, d, user, '厨房已开工60份，配送站撤回完成，损耗记计划组')
    session.refresh(d)
    assert d.status == 'manual'
    assert d.resolved_by == user.email
    actions = [e.action for e in session.query(DispatchEvent).filter_by(dispatch_id=d.id)]
    assert 'dispatch.manual_resolved' in actions


def test_drill_unknown_is_not_auto_verified_by_worker(db):
    session, user, job, Session = db
    d = make(session, user, job, simulation={'station': 'accept_drop'}, is_drill=True)
    service.advance(session, d)
    session.refresh(d)
    assert d.status == 'awaiting_verification'
    assert get_leg(session, d, 'station').status == 'unknown'
    # 演练单不再被 worker 自动推进，停留待查证供人工重试
    service.advance(session, d)
    session.refresh(d)
    assert d.status == 'awaiting_verification'
    assert get_leg(session, d, 'station').verify_count == 0
    service.verify(session, d, user)
    session.refresh(d)
    assert d.status == 'fulfilled'


def test_pending_dispatches_respects_auto_advance_and_due(db):
    session, user, job, Session = db
    d = make(session, user, job, auto_advance=False)
    assert service.pending_dispatches(session, due_at=time.time() + 999) == []
    d.auto_advance = True
    session.commit()
    assert [x.id for x in service.pending_dispatches(session, due_at=time.time() + 999)] == [d.id]


def test_cancel_while_unknown_queries_truth_then_withdraws(db):
    session, user, job, Session = db
    d = make(session, user, job, simulation={'station': 'accept_drop'})
    service.advance(session, d)
    session.refresh(d)
    assert d.status == 'awaiting_verification'
    # 此时撤单：不能把“未知”当作未送达，必须先查对端，再撤回
    service.request_cancel(session, d, user, '接单状态未知时撤单')
    session.refresh(d)
    assert d.status == 'cancelled'
    st = get_leg(session, d, 'station')
    assert st.status == 'cancelled'
    assert st.verify_count >= 1
    assert partners.inquire(session, st.order_id)['status'] == 'cancelled'
    orders = session.query(PartnerOrder).filter_by(order_id=st.order_id).count()
    assert orders == 1


def test_older_receipt_cannot_reopen_cancelled_even_with_cancel_flag(db):
    session, user, job, Session = db
    d = make(session, user, job)
    service.advance(session, d)
    service.request_cancel(session, d, user, '取消')
    session.refresh(d)
    assert d.status == 'cancelled'
    st = get_leg(session, d, 'kitchen')
    applied = st.applied_revision
    # 旧回执（低于已应用版本）被丢弃，不能把已撤单改回成功
    service._apply_observation(session, d, st, {'status': 'accepted', 'revision': applied - 1, 'started': False, 'reason': ''}, 'stale', user.email)
    session.commit(); session.refresh(d); session.refresh(st)
    assert st.status == 'cancelled'
    assert d.status == 'cancelled'
    # 对端以更新版本重新接单：按对端最终状态对账，并再次撤回，绝不留在“已落实”
    order = session.query(PartnerOrder).filter_by(order_id=st.order_id).one()
    order.status = 'accepted'; order.revision = applied + 1; order.started = False
    session.commit()
    service._apply_observation(session, d, st, {'status': 'accepted', 'revision': applied + 1, 'started': False, 'reason': ''}, 'fresh', user.email)
    session.commit()
    service.advance(session, d, user.email)
    session.refresh(d); session.refresh(st)
    assert d.status == 'cancelled'
    assert st.status == 'cancelled'
    assert partners.inquire(session, st.order_id)['status'] == 'cancelled'


def test_both_rejected_marks_cancelled_without_extra_orders(db):
    session, user, job, Session = db
    d = make(session, user, job, simulation={'kitchen': 'reject', 'station': 'reject'})
    service.advance(session, d)
    session.refresh(d)
    assert d.status == 'cancelled'
    assert {l.party: l.status for l in service.legs_of(session, d)} == {'kitchen': 'rejected', 'station': 'rejected'}
    assert session.query(PartnerOrder).count() == 2


def test_drill_reject_withdraws_other_side_and_keeps_actor(db):
    session, user, job, Session = db
    d = make(session, user, job, is_drill=True, simulation={'kitchen': 'reject'})
    service.advance(session, d)
    session.refresh(d)
    assert d.status == 'cancelled'
    legs = {l.party: l for l in service.legs_of(session, d)}
    assert legs['station'].status == 'cancelled'
    assert legs['station'].evidence
    assert d.cancel_reason.startswith('中央厨房拒单')
    events = session.query(DispatchEvent).filter_by(dispatch_id=d.id).all()
    assert any(e.action == 'dispatch.auto_cancel' and '中央厨房' in e.detail.get('reason', '') for e in events)
