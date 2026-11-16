from sqlalchemy import String, JSON, Double, ForeignKey, Boolean, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class Dispatch(Base):
    """一张已批准备餐单发往中央厨房与配送站的派发单。

    ticket_no 与逐日份数在创建时冻结；后续任何重试都复用同一单号与对端订单号。
    """
    __tablename__ = 'dispatches'
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_no: Mapped[str] = mapped_column(String(40), unique=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'))
    job_name: Mapped[str] = mapped_column(String(160), default='')
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(24), default='pending_send')
    items: Mapped[list] = mapped_column(JSON, default=list)
    total_portions: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_checksum: Mapped[str] = mapped_column(String(64), default='')
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_reason: Mapped[str] = mapped_column(String(300), default='')
    cancel_requested_by: Mapped[str] = mapped_column(String(150), default='')
    resolved_by: Mapped[str] = mapped_column(String(150), default='')
    resolved_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    resolution_note: Mapped[str] = mapped_column(String(500), default='')
    is_drill: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_advance: Mapped[bool] = mapped_column(Boolean, default=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)
    next_action_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class DispatchLeg(Base):
    """派发单的一端：中央厨房或配送站。"""
    __tablename__ = 'dispatch_legs'
    __table_args__ = (UniqueConstraint('dispatch_id', 'party', name='uq_dispatch_party'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    dispatch_id: Mapped[int] = mapped_column(ForeignKey('dispatches.id'))
    party: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(24), default='pending')
    order_id: Mapped[str] = mapped_column(String(80), unique=True)
    simulation_mode: Mapped[str] = mapped_column(String(32), default='normal')
    send_count: Mapped[int] = mapped_column(Integer, default=0)
    verify_count: Mapped[int] = mapped_column(Integer, default=0)
    cancel_count: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    applied_revision: Mapped[int] = mapped_column(Integer, default=0)
    started: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str] = mapped_column(String(500), default='')
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    sent_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    accepted_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    cancelled_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    last_contact_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    updated_at: Mapped[float] = mapped_column(Double)


class DispatchEvent(Base):
    """派发单的追加式责任轨迹：谁在什么状态下做了什么、依据是什么。"""
    __tablename__ = 'dispatch_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    dispatch_id: Mapped[int] = mapped_column(ForeignKey('dispatches.id'))
    at: Mapped[float] = mapped_column(Double)
    actor: Mapped[str] = mapped_column(String(150))
    action: Mapped[str] = mapped_column(String(80))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class PartnerOrder(Base):
    """演练用对端台账，模拟中央厨房/配送站真实收到的订单。

    真实接入时可替换为外部接口；本表让演练能跨进程、跨重启保留对端实情。
    """
    __tablename__ = 'partner_orders'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[str] = mapped_column(String(80), unique=True)
    party: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default='accepted')
    revision: Mapped[int] = mapped_column(Integer, default=1)
    started: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(300), default='')
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)
