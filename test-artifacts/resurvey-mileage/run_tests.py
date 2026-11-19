#!/usr/bin/env python3
"""返航前补测里程额度：本地验证脚本（SQLite + TestClient，不依赖 docker/redis）。

覆盖需求点：
  1. 用缺口两侧已复核群落估算漏测风险，按剩余里程挑出总风险最高的一组并给出取舍依据
  2. 手排与自动排扣同一份额度；超额被拒绝（两人同时提交由同一航次根锁串行，见第9组）
  3. 确认重试不重复扣里程（幂等）
  4. 撤销未采集补航退里程；已采到回波的不能撤销、仍占额度
  5. 确认前群落复核/余量/缺口变化，原建议作废
  6. 确认后整组才算排定（任一项失败整组回滚）
运行：python3 test-artifacts/resurvey-mileage/run_tests.py
"""
import os,sys,tempfile
os.environ['DATABASE_URL']='sqlite:///'+tempfile.mkdtemp(prefix='resurvey-mileage-')+'/test.db'
os.environ['REDIS_URL']='redis://127.0.0.1:6390/0'  # 本测试不触达队列
os.environ['SEED_DEMO']='true'
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','..','backend'))

from fastapi.testclient import TestClient
from sqlalchemy import select
from src.app import app
from src.database import Session
from src.models import SchoolAnalysis,SchoolReview,Echo,Transect,Resurvey,MissingInterval
from src.algorithm import analyze
from src.resurvey_service import choose,occupied

PASS=[];FAIL=[]
def check(name,cond,detail=''):
    (PASS if cond else FAIL).append(name)
    print(('PASS ' if cond else 'FAIL ')+name+((' | '+str(detail)) if detail and not cond else ''))
def must(name,cond,detail=''):
    check(name,cond,detail)
    if not cond:print('中止：关键前置失败');sys.exit(1)

with TestClient(app) as client:
    sup={'Authorization':'Bearer '+client.post('/auth/login',json={'username':'supervisor','password':'Estuary@2026'}).json()['token']}
    op={'Authorization':'Bearer '+client.post('/auth/login',json={'username':'operator','password':'Estuary@2026'}).json()['token']}
    def post(path,body=None,h=sup):return client.post(''+path,json=body if body is not None else {},headers=h)
    def ws():return client.get('/workspace',headers=sup).json()
    def cruise(code):return next(c for c in ws()['cruises'] if c['code']==code)
    def remaining(code):
        d=ws();c=cruise(code);used=sum(t['length_m'] for t in d['transects'] for r in d['resurveys'] if r['continuation_id']==t['id'] and r['status'] in('planned','completed') and t['cruise_id']==c['id'])
        return c['resurvey_budget_m']-used
    def review_all(codes):
        d=ws()
        for t in d['transects']:
            if t['code'] not in codes:continue
            with Session() as db:
                echoes=[{'id':e.id,'lon':e.lon,'lat':e.lat,'depth_m':e.depth_m,'strength_db':e.strength_db} for e in db.scalars(select(Echo).where(Echo.transect_id==t['id']).order_by(Echo.id))]
                tt=db.get(Transect,t['id']);assignments,metrics=analyze(echoes,1.2,4)
                a=SchoolAnalysis(transect_id=t['id'],data_version=tt.data_version,eps=1.2,min_samples=4,status='reviewed',metrics=metrics,assignments=assignments)
                db.add(a);db.flush();db.add(SchoolReview(analysis_id=a.id,noise_note='测试复核：孤立回波为底质干扰，已现场解释',decision='复核通过，可用于漏测风险评估'));db.commit()
    def launch(code,depart,arrive):
        c=cruise(code)
        r=client.patch(f"/cruises/{c['id']}/schedule",json={'depart':depart,'arrive':arrive},headers=sup);must(f'{code} 改航窗',r.status_code==200,r.text)
        r=post(f"/cruises/{c['id']}/launch");must(f'{code} 开航',r.status_code==200,r.text)
    def transect_of(code,tcode):return next(t for t in ws()['transects'] if t['cruise_id']==cruise(code)['id'] and t['code']==tcode)

    # ---- 准备：CY031（种子：额度500m，E1缺口300–600，E2缺口700–1000）----
    launch('CY031','2026-11-18T14:15:00+08:00','2026-11-18T17:00:00+08:00')
    for tc in ('E1','E2'):
        r=post(f"/transects/{transect_of('CY031',tc)['id']}/simulate");must(f'{tc} 模拟回波',r.status_code==200,r.text)
    review_all(('E1','E2'))

    # ---- 1. 建议：剩余里程内总风险最高的一组 + 取舍依据 ----
    r=post('/resurvey-plans/suggest',{'cruise_id':cruise('CY031')['id']});must('生成建议',r.status_code==200,r.text);b1=r.json()
    check('两组300m缺口只能编入一组',len(b1['items'])==1 and b1['total_m']==300.0,f"items={b1['items']}")
    check('未编入的缺口给出取舍依据',len(b1['skipped'])==1 and '优先总风险更高' in b1['skipped'][0]['reason'],b1['skipped'])
    it=b1['items'][0]
    check('风险由两侧已复核群落估算',it['risk']>0 and it['left']['count']>0 and it['right']['count']>0 and it['analysis_id'],it)
    check('建议记录生成时额度与已占里程',b1['budget_m']==500.0 and b1['used_m']==0.0,b1)

    # ---- 2. 确认整组排定 + 重试幂等 ----
    n_transects=len(ws()['transects'])
    r=post(f"/resurvey-plans/{b1['id']}/confirm");must('确认整组排定',r.status_code==200,r.text);b1c=r.json()
    check('确认后状态为已排定并记录补航',b1c['status']=='confirmed' and len(b1c['resurvey_ids'])==1,b1c)
    check('确认后占用里程300m',abs(remaining('CY031')-200)<1e-6,remaining('CY031'))
    r=post(f"/resurvey-plans/{b1['id']}/confirm")
    check('确认重试返回同一结果',r.status_code==200 and r.json()['resurvey_ids']==b1c['resurvey_ids'],r.text)
    check('重试不重复扣里程、不重复建航段',len(ws()['transects'])==n_transects+1 and abs(remaining('CY031')-200)<1e-6)

    # ---- 3. 手排扣同一份额度 ----
    other_gap=next(g for g in ws()['missing_intervals'] if g['status']=='open' and g['id'] not in [i['gap_id'] for i in b1['items']])
    r=post('/resurveys',{'gap_id':other_gap['id'],'code':'E9-S9'})
    check('剩余200m时手排300m被拒绝',r.status_code==409 and '剩余' in r.json()['detail'],r.text)

    # ---- 4. 撤销未采集补航退里程；余量变化作废旧建议 ----
    r=post('/resurvey-plans/suggest',{'cruise_id':cruise('CY031')['id']});b2=r.json()
    rid=b1c['resurvey_ids'][0]
    r=post(f'/resurveys/{rid}/cancel');must('撤销未采集补航',r.status_code==200,r.text)
    check('撤销后里程退还',abs(remaining('CY031')-500)<1e-6,remaining('CY031'))
    r=post(f"/resurvey-plans/{b2['id']}/confirm")
    check('余量变化后旧建议作废',r.status_code==409 and '作废' in r.json()['detail'],r.text)
    check('作废建议状态落库',next(p for p in ws()['resurvey_plans'] if p['id']==b2['id'])['status']=='stale')

    # ---- 5. 群落复核变化作废旧建议 ----
    r=post('/resurvey-plans/suggest',{'cruise_id':cruise('CY031')['id']});b3=r.json()
    review_all(('E1',))  # E1产生新的已复核识别
    r=post(f"/resurvey-plans/{b3['id']}/confirm")
    check('群落复核更新后旧建议作废',r.status_code==409 and '作废' in r.json()['detail'],r.text)

    # ---- 6. 额度调整作废旧建议；额度不能调到已占以下 ----
    r=post('/resurvey-plans/suggest',{'cruise_id':cruise('CY031')['id']});b4=r.json()
    r=post(f"/cruises/{cruise('CY031')['id']}/resurvey-budget",{'resurvey_budget_m':800});must('调整额度',r.status_code==200,r.text)
    r=post(f"/resurvey-plans/{b4['id']}/confirm")
    check('额度变化后旧建议作废',r.status_code==409,r.text)
    r=post(f"/resurvey-plans/{b4['id']}/confirm")
    check('作废建议再次确认仍拒绝',r.status_code==409,r.text)

    # ---- 7. 额度800m时两组300m同时编入，整组排定 ----
    r=post('/resurvey-plans/suggest',{'cruise_id':cruise('CY031')['id']});b5=r.json()
    check('额度足够时整组编入',len(b5['items'])==2 and b5['total_m']==600.0,b5)
    r=post(f"/resurvey-plans/{b5['id']}/confirm");must('确认两组',r.status_code==200,r.text)
    check('两组排定后占用600m',abs(remaining('CY031')-200)<1e-6,remaining('CY031'))
    r=post(f"/cruises/{cruise('CY031')['id']}/resurvey-budget",{'resurvey_budget_m':100})
    check('额度不能调到已占里程以下',r.status_code==409,r.text)

    # ---- 8. 已采到回波的补航不能撤销、仍占额度 ----
    planned=[r for r in ws()['resurveys'] if r['status']=='planned']
    cont=next(t for t in ws()['transects'] if t['id']==planned[0]['continuation_id'])
    parent=next(t for t in ws()['transects'] if t['id']==cont['parent_id'])
    pts=[{'along_m':x,'lon':cont['start_lon']+(cont['end_lon']-cont['start_lon'])*x/cont['length_m'],'lat':cont['start_lat']+(cont['end_lat']-cont['start_lat'])*x/cont['length_m'],'depth_m':10.0,'strength_db':-50.0} for x in (10,20,30,40,50,60,70,80)]
    r=post(f"/transects/{cont['id']}/echoes",{'points':pts});must('补航采集回波',r.status_code==200,r.text)
    r=post(f"/resurveys/{planned[0]['id']}/cancel")
    check('已采回波的补航不能撤销',r.status_code==409,r.text)
    check('已采回波的补航仍占额度',abs(remaining('CY031')-200)<1e-6,remaining('CY031'))
    r=post(f"/resurveys/{planned[1]['id']}/cancel");must('撤销另一空补航',r.status_code==200,r.text)
    check('空补航撤销后退还里程',abs(remaining('CY031')-500)<1e-6,remaining('CY031'))

    # ---- 9. 两人同时提交不能超额（同一额度，串行占额）----
    r=post('/resurvey-plans/suggest',{'cruise_id':cruise('CY031')['id']});b6=r.json()  # 余500，编入1组300
    r=post('/resurvey-plans/suggest',{'cruise_id':cruise('CY031')['id']});b7=r.json()  # 另一人同时拿到相同建议
    r=post(f"/resurvey-plans/{b6['id']}/confirm");must('第一人确认',r.status_code==200,r.text)
    r=post(f"/resurvey-plans/{b7['id']}/confirm")
    check('第二人确认同一额度被拒绝',r.status_code==409 and abs(remaining('CY031')-200)<1e-6,r.text)

    # ---- 10. 新航次：确认原子性（整组才算排定）----
    r=post('/cruises',{'code':'CY101','boat_id':1,'depart':'2026-11-19T08:00:00+08:00','arrive':'2026-11-19T11:00:00+08:00','purpose':'补测原子性验证航次','resurvey_budget_m':1000});must('建CY101',r.status_code==200,r.text)
    cid=r.json()['id']
    for tcode,lat in (('A1',30.1),('A2',30.103)):
        r=post('/transects',{'cruise_id':cid,'code':tcode,'start_lon':120.1,'start_lat':lat,'end_lon':120.113,'end_lat':lat,'probe_khz':120,'gain_db':36},op);must(f'建航段{tcode}',r.status_code==200,r.text)
    launch('CY101','2026-11-19T08:00:00+08:00','2026-11-19T11:00:00+08:00')
    for tcode in ('A1','A2'):
        t=transect_of('CY101',tcode)
        post(f"/transects/{t['id']}/simulate")
        post('/gaps',{'transect_id':t['id'],'start_m':300,'end_m':600,'reason':'探头断续缺测300米'},op)
    review_all(('A1','A2'))
    r=post('/resurvey-plans/suggest',{'cruise_id':cid});b8=r.json()
    check('CY101建议两组都编入',len(b8['items'])==2,b8)
    # 确认前航段编号被占用 -> 整组作废，一个都不排
    r=post('/transects',{'cruise_id':cid,'code':b8['items'][0]['code'],'start_lon':120.2,'start_lat':30.1,'end_lon':120.21,'end_lat':30.1,'probe_khz':120,'gain_db':36},op)
    must('占用建议航段号',r.status_code==200,r.text)
    n_res=len(ws()['resurveys'])
    r=post(f"/resurvey-plans/{b8['id']}/confirm")
    check('编号冲突整组拒绝',r.status_code==409 and '作废' in r.json()['detail'],r.text)
    check('整组回滚：无补航创建、缺口仍开',len(ws()['resurveys'])==n_res and all(g['status']=='open' for g in ws()['missing_intervals'] if g['transect_id'] in (transect_of('CY101','A1')['id'],transect_of('CY101','A2')['id'])))
    # 重新建议（避开占用编号）可正常排定
    r=post('/resurvey-plans/suggest',{'cruise_id':cid});b9=r.json()
    r=post(f"/resurvey-plans/{b9['id']}/confirm");must('重新建议后整组排定',r.status_code==200,r.text)
    check('整组两条补航一次排定',len(r.json()['resurvey_ids'])==2,r.text)

    # ---- 11. 权限：调查员/观察员不能生成或确认建议 ----
    r=post('/resurvey-plans/suggest',{'cruise_id':cid},op)
    check('调查员不能生成建议',r.status_code==403,r.text)
    r=post(f"/resurvey-plans/{b9['id']}/confirm",None,op)
    check('调查员不能确认建议',r.status_code==403,r.text)

# ---- 12. 背包选择单元验证 ----
cands=[{'gap_id':1,'length_m':300.0,'risk':50.0},{'gap_id':2,'length_m':300.0,'risk':40.0},{'gap_id':3,'length_m':200.0,'risk':30.0}]
check('背包选总风险最高组合',choose(cands,500.0)=={1,3},choose(cands,500.0))
check('里程不足全部跳过',choose(cands,199.0)==set())
check('同风险取里程更省',choose([{'gap_id':1,'length_m':300.0,'risk':10.0},{'gap_id':2,'length_m':200.0,'risk':10.0}],300.0)=={2})
check('零风险组合不选',choose([{'gap_id':1,'length_m':100.0,'risk':0.0}],500.0)==set())

print(f"\n{len(PASS)} 项通过，{len(FAIL)} 项失败")
if FAIL:print('失败项：');[print(' -',f) for f in FAIL];sys.exit(1)
