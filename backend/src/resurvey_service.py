import hashlib,json,math
from fastapi import HTTPException
from sqlalchemy import func,select
from .models import MissingInterval,Resurvey,ResurveyPlan,Transect,Echo,BioSample
from .common import get,reject,finish,cruise_lock,transect_lock,dump
from .school_service import latest

WINDOW_M=150.0
def occupied(db,cruise_id):
    # 同一份里程额度：在途与已接续的补航都占额度；撤销（未采集）与已取消的不占。
    return float(db.scalar(select(func.coalesce(func.sum(Transect.length_m),0.0)).select_from(Resurvey).join(Transect,Transect.id==Resurvey.continuation_id).where(Transect.cruise_id==cruise_id,Resurvey.status.in_(['planned','completed'])))or 0.0)
def open_gaps(db,cruise_id):
    ts={t.id:t for t in db.scalars(select(Transect).where(Transect.cruise_id==cruise_id,Transect.status!='cancelled'))}
    return ts,[g for g in db.scalars(select(MissingInterval).where(MissingInterval.status=='open').order_by(MissingInterval.id)) if g.transect_id in ts]
def estimate(db,t,g):
    # 用缺口两侧已复核群落的沿程密度外推缺口内漏测的集群回波当量。
    a=latest(db,t.id,True)
    if not a:return None
    labels={x['echo_id']:x['label'] for x in a.assignments}
    echoes=list(db.scalars(select(Echo).where(Echo.transect_id==t.id)))
    def side(lo,hi):
        pts=[e for e in echoes if lo<=e.along_m<hi and labels.get(e.id,-1)>=0];span=hi-lo
        return {'window_m':round(span,1),'count':len(pts),'clusters':sorted({labels[e.id] for e in pts}),'density':round(len(pts)/span,6) if span>0 else 0.0}
    left=side(max(0.0,g.start_m-WINDOW_M),g.start_m);right=side(g.end_m,min(t.length_m,g.end_m+WINDOW_M))
    return {'analysis_id':a.id,'left':left,'right':right,'risk':round((g.end_m-g.start_m)*(left['density']+right['density'])/2,2)}
def choose(candidates,remaining_m):
    # 0/1背包：剩余里程（0.1米粒度上取整）内总风险最高；同风险取里程更省、缺口编号更小的组合，保证结果确定。
    cap=int(math.floor(remaining_m*10+1e-6));best=[(0.0,0,())]*(cap+1)
    def better(a,b):
        if abs(a[0]-b[0])>1e-9:return a[0]>b[0]
        if a[1]!=b[1]:return a[1]<b[1]
        return a[2]<b[2]
    for it in candidates:
        cost=math.ceil(it['length_m']*10-1e-9);risk=it['risk'];gid=it['gap_id']
        for j in range(cap,cost-1,-1):
            r,c,ids=best[j-cost];cand=(round(r+risk,6),c+cost,tuple(sorted(ids+(gid,))))
            if better(cand,best[j]):best[j]=cand
    out=best[0]
    for b in best[1:]:
        if better(b,out):out=b
    return set(out[2])
def fingerprint(db,c):
    # 建议依据快照：额度、已占里程、待补缺口与相关航段最新已复核识别。任一变化即作废。
    ts,gaps=open_gaps(db,c.id)
    reviews={}
    for tid in {g.transect_id for g in gaps}:
        a=latest(db,tid,True);reviews[str(tid)]=[a.id,a.data_version] if a else None
    payload={'cruise':c.id,'budget':round(c.resurvey_budget_m,3),'occupied':round(occupied(db,c.id),3),'gaps':[[g.id,g.transect_id,round(g.start_m,3),round(g.end_m,3)] for g in gaps],'reviews':reviews}
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
def next_code(db,cruise_id,parent_code,taken):
    n=1
    while True:
        code=f'{parent_code}-S{n}'
        if code not in taken and not db.scalar(select(Transect).where(Transect.cruise_id==cruise_id,Transect.code==code)):return code
        n+=1
def continuation(c,parent,g,code):
    a=g.start_m/parent.length_m;b=g.end_m/parent.length_m
    return Transect(cruise_id=c.id,parent_id=parent.id,code=code,start_lon=parent.start_lon+(parent.end_lon-parent.start_lon)*a,start_lat=parent.start_lat+(parent.end_lat-parent.start_lat)*a,end_lon=parent.start_lon+(parent.end_lon-parent.start_lon)*b,end_lat=parent.start_lat+(parent.end_lat-parent.start_lat)*b,length_m=g.end_m-g.start_m,probe_khz=parent.probe_khz,gain_db=parent.gain_db)
def gap(db,data,user):
    c,t=transect_lock(db,data.transect_id,True)
    reject(data.end_m<=data.start_m or data.end_m>t.length_m,'缺测区间必须在原航段范围内且正向')
    reject(db.scalar(select(MissingInterval).where(MissingInterval.transect_id==t.id,MissingInterval.start_m<data.end_m,MissingInterval.end_m>data.start_m)) is not None,'缺测区间与已有记录重叠')
    row=MissingInterval(**data.model_dump());db.add(row);db.flush();return finish(db,user,'标记缺测区间',row)
def plan(db,data,user):
    initial=get(db,MissingInterval,data.gap_id);c,parent=transect_lock(db,initial.transect_id,True);g=get(db,MissingInterval,data.gap_id,True)
    reject(g.status!='open','缺测区间已接续')
    reject(db.scalar(select(Resurvey).where(Resurvey.gap_id==g.id,Resurvey.status=='planned')) is not None,'该缺测已有在途补航')
    reject(db.scalar(select(Transect).where(Transect.cruise_id==c.id,Transect.code==data.code)) is not None,'补航航段编号重复')
    used=occupied(db,c.id)
    reject(used+(g.end_m-g.start_m)>c.resurvey_budget_m+1e-6,f'补测里程超出本航次剩余额度（剩余{round(c.resurvey_budget_m-used,1)}m）')
    row=continuation(c,parent,g,data.code)
    db.add(row);db.flush();res=Resurvey(gap_id=g.id,continuation_id=row.id);db.add(res);db.flush();return finish(db,user,'规划缺测补航',res)
def suggest(db,data,user):
    c=cruise_lock(db,data.cruise_id,True)
    ts,gaps=open_gaps(db,c.id)
    reject(not gaps,'本航次没有待补缺口')
    used=occupied(db,c.id);remaining=round(c.resurvey_budget_m-used,6)
    reject(remaining<=0,'本航次补测里程额度已用完')
    inflight={r.gap_id for r in db.scalars(select(Resurvey).where(Resurvey.status=='planned'))}
    candidates=[];skipped=[]
    for g in gaps:
        t=ts[g.transect_id];base={'gap_id':g.id,'transect_id':t.id,'transect_code':t.code,'start_m':g.start_m,'end_m':g.end_m,'length_m':round(g.end_m-g.start_m,3)}
        if g.id in inflight:skipped.append({**base,'reason':'该缺口已有在途补航，无需重复编排'});continue
        est=estimate(db,t,g)
        if est is None:skipped.append({**base,'reason':'所在航段最新回波版本尚无已复核群落，无法估算漏测风险'});continue
        if est['risk']<=0:skipped.append({**base,**est,'reason':'缺口两侧已复核群落为空，漏测风险为0'});continue
        candidates.append({**base,**est})
    chosen=choose(candidates,remaining);taken=set()
    for it in candidates:
        if it['gap_id'] in chosen:it['code']=next_code(db,c.id,ts[it['transect_id']].code,taken);taken.add(it['code'])
        else:skipped.append({**it,'reason':f"剩余里程{round(remaining,1)}m内优先总风险更高的组合（本缺口需{round(it['length_m'],1)}m、风险{it['risk']}）"})
    items=[it for it in candidates if it['gap_id'] in chosen]
    batch=ResurveyPlan(cruise_id=c.id,fingerprint=fingerprint(db,c),budget_m=c.resurvey_budget_m,used_m=round(used,3),total_m=round(sum(i['length_m'] for i in items),3),total_risk=round(sum(i['risk'] for i in items),2),items=items,skipped=skipped,created_by=user.username)
    db.add(batch);db.flush();return finish(db,user,'生成返航前补测建议',batch)
def confirm(db,id,user):
    initial=get(db,ResurveyPlan,id)
    if initial.status=='confirmed':return dump(initial)
    c=cruise_lock(db,initial.cruise_id,True);batch=get(db,ResurveyPlan,id,True)
    if batch.status=='confirmed':return dump(batch)
    reject(batch.status=='stale','该建议已作废，请重新生成')
    def stale(msg):
        batch.status='stale';db.commit();raise HTTPException(409,msg)
    if fingerprint(db,c)!=batch.fingerprint:stale('群落复核或里程余量已变化，原建议作废，请重新生成')
    reject(not batch.items,'建议组为空，无需排定')
    if occupied(db,c.id)+batch.total_m>c.resurvey_budget_m+1e-6:stale('里程余量不足，原建议作废，请重新生成')
    codes={t.code for t in db.scalars(select(Transect).where(Transect.cruise_id==c.id))};created=[]
    for it in batch.items:
        g=get(db,MissingInterval,it['gap_id'],True);parent=get(db,Transect,g.transect_id,True)
        if g.status!='open' or it['code'] in codes or db.scalar(select(Resurvey).where(Resurvey.gap_id==g.id,Resurvey.status=='planned')) is not None:stale('缺口状态或航段编号已变化，原建议作废，请重新生成')
        row=continuation(c,parent,g,it['code']);db.add(row);db.flush()
        res=Resurvey(gap_id=g.id,continuation_id=row.id);db.add(res);db.flush();codes.add(it['code']);created.append(res.id)
    batch.status='confirmed';batch.resurvey_ids=created
    return finish(db,user,'确认补测建议整组排定',batch)
def set_budget(db,id,data,user):
    c=cruise_lock(db,id);used=occupied(db,c.id)
    reject(data.resurvey_budget_m<used-1e-6,f'额度低于已占里程{round(used,1)}m，无法下调')
    c.resurvey_budget_m=data.resurvey_budget_m;return finish(db,user,'调整补测里程额度',c)
def complete(db,id,data,user):
    initial=get(db,Resurvey,id);c,t=transect_lock(db,initial.continuation_id,True);res=get(db,Resurvey,id,True);g=get(db,MissingInterval,res.gap_id,True)
    reject(res.status!='planned','补航已接续或撤销')
    a=latest(db,t.id,True);reject(not a,'补航回波群落尚未复核')
    reject(not db.scalar(select(BioSample).where(BioSample.transect_id==t.id,BioSample.analysis_id==a.id)),'补航缺少对应群落的网具抽样')
    reject(db.scalar(select(MissingInterval).where(MissingInterval.transect_id==t.id,MissingInterval.status=='open')) is not None,'补航自身仍存在缺测')
    res.status='completed';res.evidence=data.evidence;g.status='covered';return finish(db,user,'确认缺测补航接续',res)
def cancel(db,id,user):
    initial=get(db,Resurvey,id);c,t=transect_lock(db,initial.continuation_id,True);row=get(db,Resurvey,id,True)
    reject(row.status!='planned' or db.scalar(select(Echo).where(Echo.transect_id==t.id)) is not None,'已采集回波或已结束的补航不能撤销')
    row.status='cancelled';t.status='cancelled';return finish(db,user,'撤销空补航并重新安排',row)
