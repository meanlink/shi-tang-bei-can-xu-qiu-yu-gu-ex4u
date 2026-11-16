import hashlib, json
def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
def dataset_dict(d):
    return {'id':d.id, 'name':d.name, 'source':d.source, 'rows':len(d.records), 'checksum':d.checksum, 'created_at':d.created_at}
def job_dict(j):
    return {k:getattr(j,k) for k in ['id','name','dataset_id','params','status','result','error','approved','created_at','finished_at']}
def report_dict(r):
    return {k:getattr(r,k) for k in ['id','name','job_id','destination','interval_seconds','next_run','enabled']}
def delivery_dict(d):
    return {k:getattr(d,k) for k in ['id','report_id','created_at','checksum','filename','destination']}
LEG_FIELDS=['id','party','status','order_id','simulation_mode','send_count','verify_count','cancel_count','attempt','started','last_error','sent_at','accepted_at','cancelled_at','last_contact_at','updated_at']
def leg_dict(leg, partner_truth=None):
    data={k:getattr(leg,k) for k in LEG_FIELDS}
    data['evidence']=leg.evidence or []
    data['partner']=partner_truth(leg) if partner_truth else None
    return data
def dispatch_dict(dispatch, legs, partner_truth=None):
    return {
        'id':dispatch.id,'ticket_no':dispatch.ticket_no,'job_id':dispatch.job_id,'job_name':dispatch.job_name,
        'title':dispatch.title,'status':dispatch.status,'items':dispatch.items,'total_portions':dispatch.total_portions,
        'snapshot_checksum':dispatch.snapshot_checksum,'cancel_requested':dispatch.cancel_requested,
        'cancel_reason':dispatch.cancel_reason,'cancel_requested_by':dispatch.cancel_requested_by,
        'resolved_by':dispatch.resolved_by,'resolved_at':dispatch.resolved_at,'resolution_note':dispatch.resolution_note,
        'is_drill':dispatch.is_drill,'auto_advance':dispatch.auto_advance,
        'created_at':dispatch.created_at,'updated_at':dispatch.updated_at,
        'next_action_at':dispatch.next_action_at,
        'legs':[leg_dict(leg, partner_truth) for leg in legs],
    }
