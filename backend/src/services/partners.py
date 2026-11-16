"""中央厨房与配送站的对端适配层。

生产环境应替换为真实接口；当前实现把对端收单台账落在数据库里，使演练可以
跨请求、跨重启保留“对端实情”。所有写操作都以 order_id 为幂等键：同一订单
重复投递只会返回已有状态，绝不生成第二张对端订单。
"""
import time
from sqlalchemy import select
from src.models import PartnerOrder

MODES = ('normal', 'accept_drop', 'reject', 'locked')
PARTY_LABELS = {'kitchen': '中央厨房', 'station': '配送站'}


class PartnerTimeout(Exception):
    """请求已发出但未拿到响应；不等于对方拒单。"""


class PartnerUnavailable(Exception):
    """连接失败等暂时不可用；不等于对方拒单。"""


def _state(order):
    if not order:
        return {'status': 'not_found', 'revision': 0, 'started': False, 'reason': '', 'order_id': ''}
    return {
        'status': order.status,
        'revision': order.revision,
        'started': bool(order.started),
        'reason': order.reason,
        'order_id': order.order_id,
        'party': order.party,
        'payload': order.payload,
        'created_at': order.created_at,
        'updated_at': order.updated_at,
    }


def find(db, order_id):
    return db.scalar(select(PartnerOrder).where(PartnerOrder.order_id == order_id))


def receive(db, order_id, party, payload):
    """幂等收单。已存在同一单号时直接返回对端最终状态。"""
    order = find(db, order_id)
    if order:
        return _state(order)
    mode = payload.get('simulation_mode', 'normal')
    now = time.time()
    if mode == 'reject':
        order = PartnerOrder(order_id=order_id, party=party, payload=payload, status='rejected',
                             revision=1, reason='档口产能不足，拒单', created_at=now, updated_at=now)
        db.add(order)
        db.flush()
        return _state(order)
    order = PartnerOrder(order_id=order_id, party=party, payload=payload, status='accepted',
                         revision=1, reason='', created_at=now, updated_at=now)
    db.add(order)
    db.flush()
    state = _state(order)
    if mode == 'locked':
        state['started'] = True
        order.started = True
        order.updated_at = now
        db.flush()
        state = _state(order)
    if mode == 'accept_drop':
        # 对端已接单，但响应在回程丢失。调用方只知道超时，不知道结果。
        db.flush()
        raise PartnerTimeout('对端已接单但响应丢失，须查证')
    return state


def inquire(db, order_id):
    return _state(find(db, order_id))


def cancel(db, order_id):
    """撤回对端订单。已开工的订单拒撤并返回 locked。"""
    order = find(db, order_id)
    if not order:
        return {'status': 'not_found', 'revision': 0, 'started': False, 'reason': '', 'order_id': order_id}
    now = time.time()
    if order.status == 'rejected':
        return _state(order)
    if order.status == 'cancelled':
        return _state(order)
    if order.status == 'locked':
        return _state(order)
    if order.started:
        order.status = 'locked'
        order.reason = '备餐已开工，无法撤回'
        order.revision += 1
        order.updated_at = now
        db.flush()
        return _state(order)
    order.status = 'cancelled'
    order.reason = '按派发单撤回'
    order.revision += 1
    order.updated_at = now
    db.flush()
    return _state(order)
