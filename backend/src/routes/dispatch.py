from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from src.config.database import get_db
from src.middlewares.auth import current_user, admin_only
from src.models import Dispatch, DispatchEvent
from src.schemas.dispatch import DispatchInput, CancelInput, ManualResolveInput
from src.services import dispatch as service
from src.services.audit import record
from src.utils.serialization import dispatch_dict, leg_dict

router = APIRouter(prefix='/dispatches')


def _load(db, dispatch_id):
    d = db.get(Dispatch, dispatch_id)
    if not d:
        raise HTTPException(404, '派发单不存在')
    return d


def _view(db, d):
    return dispatch_dict(d, service.legs_of(db, d), lambda leg: service.partner_truth(db, leg))


@router.get('')
def listing(db=Depends(get_db), u=Depends(current_user)):
    rows = db.scalars(select(Dispatch).order_by(Dispatch.id.desc()).limit(200))
    return [dispatch_dict(d, service.legs_of(db, d), lambda leg: service.partner_truth(db, leg)) for d in rows]


@router.post('', status_code=201)
def create(data: DispatchInput, db=Depends(get_db), u=Depends(admin_only)):
    d = service.create(db, data, u)
    record(db, u.email, 'dispatch.create', {'id': d.id, 'ticket_no': d.ticket_no, 'drill': d.is_drill})
    db.commit()
    return _view(db, d)

@router.get('/{id}')
def detail(id: int, db=Depends(get_db), u=Depends(current_user)):
    return _view(db, _load(db, id))


@router.post('/{id}/cancel')
def cancel(id: int, data: CancelInput | None = None, db=Depends(get_db), u=Depends(admin_only)):
    d = _load(db, id)
    service.request_cancel(db, d, u, (data.reason if data else '计划变更'))
    record(db, u.email, 'dispatch.cancel', {'id': d.id, 'ticket_no': d.ticket_no, 'status': d.status})
    db.commit()
    return _view(db, d)


@router.post('/{id}/verify')
def verify(id: int, db=Depends(get_db), u=Depends(admin_only)):
    d = _load(db, id)
    d, checked = service.verify(db, d, u)
    record(db, u.email, 'dispatch.verify', {'id': d.id, 'ticket_no': d.ticket_no, 'checked': checked, 'status': d.status})
    db.commit()
    return _view(db, d)


@router.post('/{id}/manual')
def manual(id: int, data: ManualResolveInput, db=Depends(get_db), u=Depends(admin_only)):
    d = _load(db, id)
    service.resolve_manual(db, d, u, data.note, data.outcome)
    record(db, u.email, 'dispatch.manual_resolve', {'id': d.id, 'ticket_no': d.ticket_no, 'outcome': data.outcome})
    db.commit()
    return _view(db, d)


@router.get('/{id}/events')
def events(id: int, db=Depends(get_db), u=Depends(current_user)):
    _load(db, id)
    rows = db.scalars(select(DispatchEvent).where(DispatchEvent.dispatch_id == id).order_by(DispatchEvent.id))
    return [{'id': e.id, 'at': e.at, 'actor': e.actor, 'action': e.action, 'detail': e.detail} for e in rows]
