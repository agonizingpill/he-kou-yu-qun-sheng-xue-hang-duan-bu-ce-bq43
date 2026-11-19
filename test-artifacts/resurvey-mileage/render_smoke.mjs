// 前端页面渲染冒烟：桩掉浏览器全局，验证缺测补航页在含里程额度/建议数据时能完整渲染。
global.localStorage={getItem:()=>null,setItem:()=>{},removeItem:()=>{}};
global.document={getElementById:()=>null};
const {store}=await import('/workspace/frontend/src/stores/workspace.js');
store.selected=1;store.session={username:'supervisor',role:'supervisor'};
store.data={
 boats:[{id:1,code:'T1',name:'潮研一号'}],
 cruises:[{id:1,code:'CY031',boat_id:1,depart:'2026-11-18T14:15:00+08:00',arrive:'2026-11-18T17:00:00+08:00',purpose:'河口鱼群声学航段与补测',status:'sailing',resurvey_budget_m:500}],
 navigation_closures:[],
 transects:[
  {id:1,cruise_id:1,parent_id:null,code:'E1',start_lon:120.1,start_lat:30.1,end_lon:120.113,end_lat:30.1,length_m:1253,probe_khz:120,gain_db:36,data_version:1,status:'reviewed'},
  {id:2,cruise_id:1,parent_id:null,code:'E2',start_lon:120.1,start_lat:30.103,end_lon:120.113,end_lat:30.103,length_m:1253,probe_khz:120,gain_db:36,data_version:1,status:'reviewed'},
  {id:3,cruise_id:1,parent_id:1,code:'E1-S1',start_lon:120.103,start_lat:30.1,end_lon:120.106,start_lat:30.1,length_m:300,probe_khz:120,gain_db:36,data_version:0,status:'planned'}],
 echoes:[],school_analyses:[],school_reviews:[],nets:[],net_calibrations:[],bio_samples:[],
 missing_intervals:[
  {id:1,transect_id:1,start_m:300,end_m:600,reason:'探头断续缺测300米',status:'open'},
  {id:2,transect_id:2,start_m:700,end_m:1000,reason:'电缆干扰断续缺测300米',status:'open'}],
 resurveys:[{id:1,gap_id:1,continuation_id:3,status:'planned',evidence:''}],
 resurvey_plans:[{id:1,cruise_id:1,status:'suggested',fingerprint:'abc',budget_m:500,used_m:300,total_m:300,total_risk:48.5,
  items:[{gap_id:2,transect_id:2,transect_code:'E2',code:'E2-S1',start_m:700,end_m:1000,length_m:300,risk:48.5,analysis_id:2,left:{window_m:150,count:24,clusters:[1],density:0.16},right:{window_m:150,count:23,clusters:[2],density:0.153}}],
  skipped:[{gap_id:1,transect_id:1,transect_code:'E1',start_m:300,end_m:600,length_m:300,reason:'该缺口已有在途补航，无需重复编排'}],
  resurvey_ids:[],created_by:'supervisor'}],
 survey_releases:[]};
const resurveys=await import('/workspace/frontend/src/pages/resurveys.js');
const html=resurveys.render();
const need=['已占 300m / 额度 500m','剩余 200m','建议#1','待确认','E2-S1','取舍依据','在途补航','确认整组排定','手排','退还里程'];
const missing=need.filter(s=>!html.includes(s));
if(missing.length){console.error('缺测补航页缺少内容:',missing);process.exit(1);}
resurveys.bind(()=>{});
// 已排定状态也应渲染
store.data.resurvey_plans[0]={...store.data.resurvey_plans[0],status:'confirmed',resurvey_ids:[7]};
const html2=resurveys.render();
if(!html2.includes('已整组排定')||!html2.includes('#7')||html2.includes('确认整组排定（')){console.error('已排定建议渲染异常');process.exit(1);}
// 作废状态
store.data.resurvey_plans[0]={...store.data.resurvey_plans[0],status:'stale'};
if(!resurveys.render().includes('已作废')){console.error('作废建议渲染异常');process.exit(1);}
const cruises=await import('/workspace/frontend/src/pages/cruises.js');
if(!cruises.render().includes('补测里程额度')){console.error('航次页缺少额度输入');process.exit(1);}
console.log('前端渲染冒烟通过：额度条、建议卡（待确认/已排定/已作废）、取舍依据、手排表单均正常');
