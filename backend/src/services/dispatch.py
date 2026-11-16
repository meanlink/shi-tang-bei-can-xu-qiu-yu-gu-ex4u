"""备餐单两端派发：中央厨房 + 配送站。

不变量
------
1. 逐日份数、单号、两端订单号在创建时冻结，任何重试复用原值。
2. 超时/连接失败只记为“未知”，必须向对端查证；未知永远不等于拒单。
3. 一端拒单即撤回另一端；已开工的一端撤不回时转人工，保留责任去向。
4. 撤单请求一旦成立，派发单永远不会回到“已落实”；对账以对端最终状态为准，
   低于已应用版本号的乱序回执直接丢弃。
5. 对端按订单号幂等收单；中断恢复只恢复原单，不生成第二张对端订单。
"""
import time, secrets
from datetime import date
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.models import Dispatch, DispatchLeg, DispatchEvent, PartnerOrder
from src.services import partners
from src.utils.serialization import checksum

PARTY_ORDER = ('kitchen', 'station')
CANCELLED_LIKE = ('cancelled', 'rejected')


def now():
    return time.time()


def _event(db, dispatch, actor, action, detail=None):
    db.add(DispatchEvent(dispatch_id=dispatch.id, at=now(), actor=actor, action=action, detail=detail or {}))


def _evidence(leg, source, state=None, note=''):
    entry = {'at': now(), 'source': source, 'note': note}
    if state is not None:
        entry.update({'status': state.get('status'), 'revision': state.get('revision'), 'started': state.get('started'), 'reason': state.get('reason', '')})
    leg.evidence = (list(leg.evidence or []) + [entry])[-40:]


def legs_of(db, dispatch):
    return list(db.scalars(select(DispatchLeg).where(DispatchLeg.dispatch_id == dispatch.id).order_by(DispatchLeg.id)))


def _leg(db, dispatch, party):
    return db.scalar(select(DispatchLeg).where(DispatchLeg.dispatch_id == dispatch.id, DispatchLeg.party == party))


def _party_label(party):
    return partners.PARTY_LABELS.get(party, party)


def _frozen_items(job):
    table = (job.result or {}).get('table') or {}
    columns = [c.get('key') for c in table.get('columns', [])]
    if 'date' not in columns or 'plan' not in columns:
        raise HTTPException(409, '该分析结果缺少逐日备餐份数，不能派发')
    items = []
    for row in table.get('rows', []):
        try:
            portions = int(row['plan'])
            day = str(row['date'])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(409, '分析结果中的逐日份数无效')
        if portions < 0:
            raise HTTPException(409, '备餐份数不能为负')
        items.append({'date': day, 'weekday': row.get('weekday', ''), 'forecast': row.get('forecast'), 'buffer': row.get('buffer'), 'portions': portions})
    if not items:
        raise HTTPException(409, '该分析结果没有可派发的日期')
    return items


def _unique_ticket(db):
    """单号创建即冻结且全局唯一；冲突时换一个随机后缀，绝不覆盖已有派发单。"""
    prefix = f'MP-{date.today().strftime("%Y%m%d")}'
    for _ in range(8):
        ticket = f'{prefix}-{secrets.token_hex(3).upper()}'
        if not db.scalar(select(Dispatch.id).where(Dispatch.ticket_no == ticket)):
            return ticket
    raise HTTPException(503, '单号生成冲突过多，请稍后重试')


def create(db, data, user):
    """冻结已批准结果，生成两端派发单。幂等键重试只返回原单。"""
    if getattr(data, 'idempotency_key', None):
        existing = db.scalar(select(Dispatch).where(Dispatch.idempotency_key == data.idempotency_key))
        if existing:
            return existing
    from src.models import Job
    job = db.get(Job, data.job_id)
    if not job or job.status != 'completed' or not job.approved:
        raise HTTPException(409, '请先选择已完成且获批准的分析结果')
    items = _frozen_items(job)
    modes = getattr(data, 'simulation', None) or {}
    unknown = set(modes) - set(PARTY_ORDER)
    if unknown:
        raise HTTPException(422, f'未知的演练对象：{", ".join(sorted(unknown))}')
    for party in PARTY_ORDER:
        if str(modes.get(party, 'normal')) not in partners.MODES:
            raise HTTPException(422, f'{_party_label(party)}的演练模式无效')
    ticket = _unique_ticket(db)
    stamp = now()
    dispatch = Dispatch(
        ticket_no=ticket, job_id=job.id, job_name=job.name, title=data.title,
        status='pending_send', items=items, total_portions=sum(i['portions'] for i in items),
        snapshot_checksum=checksum({'job': job.id, 'items': items}), is_drill=bool(getattr(data, 'is_drill', False)),
        auto_advance=bool(getattr(data, 'auto_advance', True)),
        idempotency_key=getattr(data, 'idempotency_key', None), created_by=user.id,
        created_at=stamp, updated_at=stamp, next_action_at=stamp,
    )
    db.add(dispatch)
    db.flush()
    for party in PARTY_ORDER:
        mode = str(modes.get(party, 'normal'))
        db.add(DispatchLeg(
            dispatch_id=dispatch.id, party=party, status='pending',
            order_id=f'{ticket}-{"K" if party == "kitchen" else "S"}',
            simulation_mode=mode, updated_at=stamp,
        ))
    _event(db, dispatch, user.email, 'dispatch.create', {
        'ticket_no': ticket, 'total_portions': dispatch.total_portions, 'days': len(items),
        'checksum': dispatch.snapshot_checksum, 'simulation': modes,
    })
    try:
        db.flush()
    except IntegrityError:
        # 并发同幂等键只会有一张派发单胜出，另一请求返回已存在的那张。
        db.rollback()
        if getattr(data, 'idempotency_key', None):
            existing = db.scalar(select(Dispatch).where(Dispatch.idempotency_key == data.idempotency_key))
            if existing:
                return existing
        raise HTTPException(409, '单号或幂等键冲突，请重试')
    return dispatch


def _apply_observation(db, dispatch, leg, state, source, actor):
    """把对端观测写入本端。版本号更旧的回执被丢弃，防止乱序回执翻案。"""
    revision = int(state.get('revision') or 0)
    if revision and revision < int(leg.applied_revision or 0):
        _evidence(leg, source, state, note='回执版本过旧，已忽略')
        _event(db, dispatch, actor, 'dispatch.stale_receipt', {
            'party': leg.party, 'order_id': leg.order_id, 'receipt_revision': revision,
            'applied_revision': leg.applied_revision, 'receipt_status': state.get('status'),
        })
        return False
    status = state.get('status')
    if (dispatch.cancel_requested and leg.status == 'cancelled' and status == 'accepted'
            and revision <= int(leg.applied_revision or 0)):
        # 撤回已确认，迟到的同版本/旧版本接单回执不得把结果改回成功。
        _evidence(leg, source, state, note='撤单已确认，迟到的接单回执不改写结果')
        _event(db, dispatch, actor, 'dispatch.stale_receipt', {
            'party': leg.party, 'order_id': leg.order_id, 'receipt_status': status,
            'receipt_revision': revision, 'applied_revision': leg.applied_revision, 'reason': 'cancel_confirmed',
        })
        return False
    stamp = now()
    leg.last_contact_at = stamp
    if status == 'accepted':
        withdrawing = leg.status == 'cancelling'
        leg.status = 'accepted'
        leg.started = bool(state.get('started'))
        leg.accepted_at = leg.accepted_at or stamp
        # 撤回在途却收到成功回执：以对端当前版本为准，但继续撤回，不视为已落实。
        leg.last_error = '撤回在途，收到接单回执，继续撤回' if withdrawing else ''
    elif status == 'rejected':
        leg.status = 'rejected'
        leg.last_error = state.get('reason') or '对端拒单'
        leg.started = False
        leg.accepted_at = None
        leg.cancelled_at = None
    elif status == 'cancelled':
        leg.status = 'cancelled'
        leg.cancelled_at = leg.cancelled_at or stamp
        leg.last_error = ''
    elif status == 'locked':
        leg.status = 'locked'
        leg.started = True
        leg.last_error = state.get('reason') or '对端已开工，无法撤回'
    elif status == 'not_found':
        if dispatch.cancel_requested:
            leg.status = 'cancelled'
            leg.cancelled_at = leg.cancelled_at or stamp
            leg.last_error = ''
            state = {**state, 'status': 'not_found', 'reason': state.get('reason') or '对端无此单，视为未收单'}
        else:
            # 请求确实没落到对端；恢复为待发送，按原单号重发，对端不会收到第二张单。
            leg.status = 'pending'
            leg.last_error = '对端未收到订单，将按原单号重发'
            _evidence(leg, source, state, note='对端无此单，保留原单号恢复待发送')
            _event(db, dispatch, actor, 'dispatch.not_found', {'party': leg.party, 'order_id': leg.order_id})
            return True
    else:
        leg.last_error = f'未识别的对端状态：{status}'
        _evidence(leg, source, state, note='未识别状态')
        return False
    leg.applied_revision = max(int(leg.applied_revision or 0), revision)
    leg.updated_at = stamp
    _evidence(leg, source, state, note='已应用对端状态')
    _event(db, dispatch, actor, 'dispatch.observation', {
        'party': leg.party, 'order_id': leg.order_id, 'receipt_status': status,
        'leg_status': leg.status, 'revision': revision, 'started': bool(state.get('started')),
    })
    return True


def _payload(dispatch, leg):
    return {
        'ticket_no': dispatch.ticket_no, 'order_id': leg.order_id, 'party': leg.party,
        'title': dispatch.title, 'items': dispatch.items, 'total_portions': dispatch.total_portions,
        'snapshot_checksum': dispatch.snapshot_checksum, 'simulation_mode': leg.simulation_mode,
        'attempt': leg.attempt,
    }


def _send_one(db, dispatch, leg, actor):
    stamp = now()
    # 先落库“发送中”，进程中断后可恢复为未知并查证，而不是重复下单。
    leg.status = 'sending'
    leg.attempt = int(leg.attempt or 0) + 1
    leg.send_count = int(leg.send_count or 0) + 1
    leg.updated_at = stamp
    _event(db, dispatch, actor, 'dispatch.send', {'party': leg.party, 'order_id': leg.order_id, 'attempt': leg.attempt, 'ticket_no': dispatch.ticket_no})
    db.commit()
    try:
        state = partners.receive(db, leg.order_id, leg.party, _payload(dispatch, leg))
    except partners.PartnerTimeout as exc:
        leg.status = 'unknown'
        leg.last_error = f'响应超时：{exc}'
        leg.updated_at = now()
        _evidence(leg, 'send', None, note='已发出但未收到响应，状态未知，须查证')
        _event(db, dispatch, actor, 'dispatch.timeout', {'party': leg.party, 'order_id': leg.order_id, 'attempt': leg.attempt})
        db.commit()
        return
    except Exception as exc:  # 连接失败等同样只代表未知，绝不按拒单处理
        leg.status = 'unknown'
        leg.last_error = f'调用失败待查证：{str(exc)[:400]}'
        leg.updated_at = now()
        _evidence(leg, 'send', None, note='调用异常，状态未知，须查证')
        _event(db, dispatch, actor, 'dispatch.unknown', {'party': leg.party, 'order_id': leg.order_id, 'error': str(exc)[:300]})
        db.commit()
        return
    _apply_observation(db, dispatch, leg, state, 'send', actor)
    leg.updated_at = now()
    db.commit()


def _inquire_one(db, dispatch, leg, actor):
    leg.verify_count = int(leg.verify_count or 0) + 1
    try:
        state = partners.inquire(db, leg.order_id)
    except Exception as exc:
        leg.last_error = f'查证失败待重试：{str(exc)[:400]}'
        leg.updated_at = now()
        _evidence(leg, 'verify', None, note='查证调用失败，仍为未知')
        _event(db, dispatch, actor, 'dispatch.verify_failed', {'party': leg.party, 'order_id': leg.order_id, 'error': str(exc)[:300]})
        db.commit()
        return
    _apply_observation(db, dispatch, leg, state, 'verify', actor)
    leg.updated_at = now()
    db.commit()


def _cancel_one(db, dispatch, leg, actor):
    leg.status = 'cancelling'
    leg.cancel_count = int(leg.cancel_count or 0) + 1
    leg.updated_at = now()
    db.commit()
    _event(db, dispatch, actor, 'dispatch.cancel_request', {'party': leg.party, 'order_id': leg.order_id, 'attempt': leg.cancel_count})
    try:
        state = partners.cancel(db, leg.order_id)
    except Exception as exc:
        # 撤回结果未知：保持“撤回中/已接单”，下一轮按对端实情对账，绝不臆断已撤。
        leg.status = 'accepted'
        leg.last_error = f'撤回未确认，稍后按对端状态对账：{str(exc)[:400]}'
        leg.updated_at = now()
        _evidence(leg, 'cancel', None, note='撤回请求异常，未确认撤回，稍后重试')
        _event(db, dispatch, actor, 'dispatch.cancel_unknown', {'party': leg.party, 'order_id': leg.order_id, 'error': str(exc)[:300]})
        db.commit()
        return
    _apply_observation(db, dispatch, leg, state, 'cancel', actor)
    leg.updated_at = now()
    db.commit()


def request_cancel(db, dispatch, user, reason):
    if dispatch.status == 'cancelled':
        raise HTTPException(409, '该派发单已撤单，不能重复撤单')
    if dispatch.status == 'manual':
        raise HTTPException(409, '该派发单已转人工处置，请在人工处置中记录结论')
    if not dispatch.cancel_requested:
        dispatch.cancel_requested = True
        dispatch.cancel_reason = (reason or '')[:300]
        dispatch.cancel_requested_by = user.email
        dispatch.updated_at = now()
        _event(db, dispatch, user.email, 'dispatch.cancel', {'reason': dispatch.cancel_reason, 'status': dispatch.status})
        db.commit()
    advance(db, dispatch, user.email)


def _begin_withdrawal(db, dispatch, reason, actor):
    if not dispatch.cancel_requested:
        dispatch.cancel_requested = True
        dispatch.cancel_reason = reason[:300]
        dispatch.cancel_requested_by = actor
        dispatch.updated_at = now()
        _event(db, dispatch, actor, 'dispatch.auto_cancel', {'reason': dispatch.cancel_reason})


def _withdraw(db, dispatch, actor):
    for leg in legs_of(db, dispatch):
        if leg.status in ('pending', 'sending'):
            if leg.status == 'sending':
                # 发送在半途，结果未知：先查对端，绝不把“未知”当“未送达”。
                leg.status = 'unknown'
                leg.updated_at = now()
                _evidence(leg, 'cancel', None, note='撤单时发现发送未回执，转查证')
            else:
                leg.status = 'cancelled'
                leg.cancelled_at = now()
                leg.updated_at = now()
                _evidence(leg, 'cancel', None, note='尚未送达对端，本地撤回')
                _event(db, dispatch, actor, 'dispatch.cancelled_local', {'party': leg.party, 'order_id': leg.order_id})
                db.commit()
                continue
        if leg.status in CANCELLED_LIKE + ('locked',):
            continue
        if leg.status == 'unknown':
            # 未知状态先查对端实情，再决定撤回；不把超时当拒单或空单。
            _inquire_one(db, dispatch, leg, actor)
            if leg.status in ('pending', 'unknown'):
                continue  # 对端无单或仍未知，下一轮继续查证/重发
        if leg.status in ('accepted', 'cancelling'):
            _cancel_one(db, dispatch, leg, actor)


def _reconcile(db, dispatch, actor):
    legs = legs_of(db, dispatch)
    if not dispatch.cancel_requested:
        rejected = [leg for leg in legs if leg.status == 'rejected']
        if rejected:
            # 任一端拒单：整张派发单进入撤回流程，另一端即使已接单也要撤回。
            others = [leg for leg in legs if leg.status != 'rejected']
            reason = f'{_party_label(rejected[0].party)}拒单，撤回另一端' if others else '两端均拒单，派发未成立'
            _begin_withdrawal(db, dispatch, reason, actor)
            _withdraw(db, dispatch, actor)
    statuses = [leg.status for leg in legs]
    if 'locked' in statuses:
        dispatch.status = 'manual'
        return
    if dispatch.cancel_requested:
        if statuses and all(s in CANCELLED_LIKE for s in statuses):
            dispatch.status = 'cancelled'
        else:
            dispatch.status = 'cancelling'
        return
    if 'unknown' in statuses or 'sending' in statuses or 'cancelling' in statuses:
        dispatch.status = 'awaiting_verification'
    elif statuses and all(s == 'accepted' for s in statuses):
        dispatch.status = 'fulfilled'
    elif statuses and all(s == 'pending' for s in statuses):
        dispatch.status = 'pending_send'
    elif 'rejected' in statuses:
        dispatch.status = 'cancelling'
    else:
        dispatch.status = 'awaiting_verification'


def advance(db, dispatch, actor='worker'):
    """推进一张派发单：发送待发送端、查证未知端、对账并落库。"""
    if dispatch.status == 'manual':
        return dispatch
    if dispatch.status == 'cancelled':
        # 撤单后若对端以更新版本重新接单，仍按对端最终状态对账并再次撤回。
        if not any(leg.status in ('accepted', 'cancelling', 'unknown') for leg in legs_of(db, dispatch)):
            return dispatch
        dispatch.cancel_requested = True
    if dispatch.status == 'fulfilled' and not dispatch.cancel_requested:
        return dispatch
    if dispatch.cancel_requested:
        _withdraw(db, dispatch, actor)
    else:
        sent = set()
        for leg in legs_of(db, dispatch):
            if leg.status == 'pending':
                _send_one(db, dispatch, leg, actor)
                sent.add(leg.id)
        for leg in legs_of(db, dispatch):
            # 本轮刚刚发送失败的“未知”留给下一轮/人工查证，让待查证可见、可控。
            # 演练单不自动查证：由页面上的“重试查证”显式推进，便于演练与留痕。
            if leg.status == 'unknown' and leg.id not in sent and not dispatch.is_drill:
                _inquire_one(db, dispatch, leg, actor)
    _reconcile(db, dispatch, actor)
    dispatch.updated_at = now()
    if dispatch.status in ('cancelled', 'manual', 'fulfilled'):
        dispatch.next_action_at = now()
    elif dispatch.is_drill and dispatch.status == 'awaiting_verification':
        # 演练单等待人工“重试查证”，不后台轮询，便于逐步留痕。
        dispatch.next_action_at = now() + 3600
    else:
        dispatch.next_action_at = now() + 5
    db.commit()
    return dispatch


def verify(db, dispatch, user):
    """人工重试查证：只向对端问实情；对端无单则按原单号恢复发送，绝不生成新单。"""
    if dispatch.status == 'cancelled':
        raise HTTPException(409, '该派发单已撤单，无需查证')
    if dispatch.status == 'fulfilled' and not dispatch.cancel_requested:
        raise HTTPException(409, '两端均已接单，无需查证')
    if dispatch.status == 'manual':
        raise HTTPException(409, '该派发单已转人工处置，请直接记录处置结论')
    legs = legs_of(db, dispatch)
    checked = 0
    for leg in legs:
        if leg.status in ('unknown', 'sending', 'cancelling', 'accepted', 'locked', 'rejected'):
            if leg.status == 'sending':
                leg.status = 'unknown'
            _inquire_one(db, dispatch, leg, user.email)
            checked += 1
    if not dispatch.cancel_requested:
        for leg in legs:
            # 查证确认对端无单后，按原单号恢复发送；订单号不变，对端幂等。
            if leg.status == 'pending':
                _send_one(db, dispatch, leg, user.email)
                checked += 1
    if dispatch.cancel_requested:
        _withdraw(db, dispatch, user.email)
    _reconcile(db, dispatch, user.email)
    dispatch.updated_at = now()
    db.commit()
    return dispatch, checked


def recover(db):
    """执行中断恢复：发送/撤回中的腿降级为未知，原单号不变，随后查证而非重下单。"""
    recovered = []
    for leg in db.scalars(select(DispatchLeg).where(DispatchLeg.status.in_(('sending', 'cancelling')))):
        leg.status = 'unknown'
        leg.last_error = '执行中断，结果未知，待向对端查证'
        leg.updated_at = now()
        _evidence(leg, 'recovery', None, note='执行中断恢复，保留原订单号')
        dispatch = db.get(Dispatch, leg.dispatch_id)
        _event(db, dispatch, 'system', 'dispatch.recovered', {'party': leg.party, 'order_id': leg.order_id})
        recovered.append(dispatch.id)
    db.commit()
    for dispatch_id in sorted(set(recovered)):
        dispatch = db.get(Dispatch, dispatch_id)
        _reconcile(db, dispatch, 'system')
        dispatch.updated_at = now()
    db.commit()
    return sorted(set(recovered))


def resolve_manual(db, dispatch, user, note, outcome='acknowledged'):
    if dispatch.status != 'manual':
        raise HTTPException(409, '仅待人工处置的派发单可以记录处置结论')
    if not note or len(note.strip()) < 2:
        raise HTTPException(422, '请填写人工处置结论与责任去向')
    dispatch.resolved_by = user.email
    dispatch.resolved_at = now()
    dispatch.resolution_note = note.strip()[:500]
    dispatch.updated_at = now()
    _event(db, dispatch, user.email, 'dispatch.manual_resolved', {
        'note': dispatch.resolution_note, 'outcome': outcome,
        'cancel_requested_by': dispatch.cancel_requested_by,
        'legs': {leg.party: {'status': leg.status, 'order_id': leg.order_id, 'reason': leg.last_error} for leg in legs_of(db, dispatch)},
    })
    db.commit()
    return dispatch


def pending_dispatches(db, due_at=None, limit=20):
    due_at = now() if due_at is None else due_at
    return list(db.scalars(select(Dispatch).where(
        Dispatch.status.in_(('pending_send', 'awaiting_verification', 'cancelling')),
        Dispatch.auto_advance.is_(True),
        Dispatch.next_action_at.is_not(None), Dispatch.next_action_at <= due_at,
    ).order_by(Dispatch.id).limit(limit)))


def partner_truth(db, leg):
    order = db.scalar(select(PartnerOrder).where(PartnerOrder.order_id == leg.order_id))
    if not order:
        return None
    return {
        'status': order.status, 'started': bool(order.started), 'revision': order.revision,
        'reason': order.reason, 'updated_at': order.updated_at,
    }


def detail_dict(db, dispatch):
    from src.utils.serialization import dispatch_dict
    return dispatch_dict(dispatch, legs_of(db, dispatch), lambda leg: partner_truth(db, leg))
