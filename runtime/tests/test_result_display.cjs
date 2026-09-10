const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../app/static/app.js'),'utf8');
// Synthetic fixture: a screened candidate costs more per lot than its allocation limit.
const decision={status:'AVAILABLE',summary:'测试模型筛选结果',data_asof:'2026-01-05',version:{status:'ACTIVE',version_key:'test-version'},
  result:{run_key:'test-run',request:{capital:100000,horizon_months:12,max_positions:6,execution:{lot_size:100}},
    recommendation:{profile:'aggressive',profile_label:'激进',data_asof:'2026-01-05',parameters:{max_position_pct:.1667},
      positions:[{symbol:'001001',name:'测试公司',reference_price:412.22,shares:0,amount:0,weight:0}],
      candidate_ranking:[{symbol:'001001',name:'测试公司',eligible:true}],cash_weight:1,
      portfolio_forecast:{status:'AVAILABLE',capital:100000,curve:[]}}}};
const declarations=[...source.matchAll(/^(?:async )?function (\w+)\(/gm)];
function extract(name){
  const index=declarations.findIndex(match=>match[1]===name);
  assert.ok(index>=0,name);
  return source.slice(declarations[index].index,declarations[index+1]?.index||source.length);
}
const events=[];
const controls={
  '#quickQuantRun':{},'#quantDecisionMessage':{},
  '#quantDecisionOutput':{innerHTML:'',scrollIntoView(){events.push('scroll');},focus(){events.push('focus');}},
  '.daily-decision-state':{querySelector(){return {};}}
};
const context=vm.createContext({
  money:(v,d=2)=>Number(v).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}),
  safe:v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  plainQuantText:v=>String(v??''),plainQuantPillar:v=>v,plainQuantReason:v=>v,
  localTimestamp:v=>v,quantVersionLabel:v=>v,
  quantTimeframeForecastHtml:()=>'<div>Individual forecasts</div>',quantTimeframeForecastDetailHtml:()=>'',
  portfolioFutureCurveSvg:()=>'<svg data-portfolio-curve></svg>',
  window:{setTimeout,clearTimeout},AbortController,
  $:selector=>controls[selector],state:{quantRequestSerial:0},quantDecisionInput:()=>({capital:100000}),
  showToast:message=>events.push(message),request:async()=>({decision})
});
for(const name of ['quantAllocationState','quantDecisionSummary','quantResultHtml','quantDecisionOutputHtml','quantInputKey','quantRequest','quantReadStatus','renderQuantOutcome','viewQuickQuantRecommendation'])vm.runInContext(extract(name),context);

(async()=>{
  const blocked=context.quantDecisionOutputHtml(decision);
  assert.match(blocked,/当前没有可投入的研究组合/);
  assert.match(blocked,/测试公司/);
  assert.match(blocked,/41,222/);
  assert.match(blocked,/16,670/);
  assert.match(blocked,/本次资金未分配/);
  assert.doesNotMatch(blocked,/建议投入 ¥ 0\.00/);
  assert.doesNotMatch(blocked,/达到当前正式条件/);
  assert.doesNotMatch(blocked,/data-portfolio-curve/);

  const funded=structuredClone(decision);
  Object.assign(funded.result.recommendation.positions[0],{shares:100,amount:41222,weight:.41222});
  funded.result.recommendation.cash_weight=.58778;
  delete funded.result.run_key;
  const fundedHtml=context.quantDecisionOutputHtml(funded);
  assert.match(fundedHtml,/建议投入 ¥ 41,222\.00/);
  assert.match(fundedHtml,/正式推荐股票/);
  assert.match(fundedHtml,/data-portfolio-curve/);
  assert.doesNotMatch(fundedHtml,/本次资金未分配/);

  const incomplete=context.quantDecisionOutputHtml({status:'AVAILABLE',result:{}});
  assert.match(incomplete,/结果记录缺少推荐详情/);

  await context.viewQuickQuantRecommendation();
  assert.match(controls['#quantDecisionOutput'].innerHTML,/本次资金未分配/);
  assert.equal(controls['#quickQuantRun'].disabled,false);
  assert.ok(events.includes('scroll')&&events.includes('focus'));
  assert.match(controls['#quantDecisionMessage'].textContent,/暂无可投入组合/);
  const preserved=controls['#quantDecisionOutput'].innerHTML;
  context.request=async()=>{throw new Error('模拟连接失败');};
  await context.viewQuickQuantRecommendation();
  assert.equal(controls['#quantDecisionOutput'].innerHTML,preserved);
  assert.equal(controls['#quickQuantRun'].disabled,false);
  assert.match(controls['#quantDecisionMessage'].textContent,/读取推荐失败/);
  console.log('PASS: zero-share explanation, funded result without run id, incomplete result, immediate result focus, failure recovery.');
})().catch(error=>{console.error(error);process.exitCode=1;});
