import time, json, logging
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, Dispatch
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import dispatch as dispatch_service
from src.schemas.dispatch import DispatchInput
from src.utils.security import hash_password
from src.utils.serialization import checksum


def _seed_dispatches(db, job, admin):
    """只播种一次：一张已落实的常规派发 + 一张待查证的演练单，供页面查看状态。"""
    if db.scalar(select(Dispatch).limit(1)):
        return
    normal = dispatch_service.create(db, DispatchInput(
        job_id=job.id, title='午餐备餐单 · 中央厨房与配送站', auto_advance=False,
    ), admin)
    dispatch_service.advance(db, normal, 'seed')
    drill = dispatch_service.create(db, DispatchInput(
        job_id=job.id, title='演练 · 配送站接单后丢响应', is_drill=True,
        simulation={'station': 'accept_drop'}, auto_advance=False,
    ), admin)
    dispatch_service.advance(db, drill, 'seed')
    db.add(Audit(actor='seed', action='demo.dispatch', detail={'normal': normal.ticket_no, 'drill': drill.ticket_no}, at=time.time()))


def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))
        db.commit()
        baseline=db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name']))
        admin=db.scalar(select(User).where(User.role=='admin'))
        if baseline and admin:
            # 派发演练单独成事务：失败不得回滚已建好的账号、批次与基线结果。
            try:
                _seed_dispatches(db, baseline, admin)
                db.commit()
            except Exception:
                db.rollback()
                logging.exception('Demo dispatch seed skipped')
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,Dispatch]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
