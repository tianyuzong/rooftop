const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(require('node:path').join(__dirname,'../app/static/app.js'),'utf8');
const starts=[...source.matchAll(/^(?:async )?function (\w+)\(/gm)];
const context={money:v=>Number(v).toFixed(2),safe:v=>String(v??''),plainQuantReason:v=>v,plainQuantText:v=>v,localTimestamp:v=>v};
vm.createContext(context);
for(const name of ['quantAllocationState','quantDecisionSummary','quantRelativeCandidatesHtml','quantDecisionOutputHtml']){
  const i=starts.findIndex(x=>x[1]===name);vm.runInContext(source.slice(starts[i].index,starts[i+1].index),context);
}
const pick={name:'相对第一',symbol:'600001',relative_rank:1,composite_score:-1,probability_up:.4,blockers:['模型未通过'],minimum_lot_shares:100,minimum_lot_cost:3008,capital_affordable:true};
const d={status:'AVAILABLE',summary:'相对领先：相对第一',version:{status:'SNAPSHOT'},result:{request:{capital:10000},recommendation:{positions:[],relative_candidate_count:30,relative_recommendations:[pick],affordable_relative_recommendations:[pick]}}};
const html=context.quantDecisionOutputHtml(d);
assert.match(html,/相对第一/);assert.match(html,/模型未通过/);assert.match(html,/3008/);
assert.match(html,/本金可覆盖/);assert.match(html,/20%/);
assert.doesNotMatch(html,/当前没有达到重点关注最低条件|当前无可执行研究标的/);
console.log('Relative candidate fallback UI passed');

assert.ok(html.indexOf('可覆盖的候选') < html.indexOf('综合排名靠前'),'affordable picks must be shown first');
