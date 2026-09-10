const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../app/static/app.js'),'utf8');
const functions=[...source.matchAll(/^(?:async )?function (\w+)\(/gm)];
const extract=name=>{const i=functions.findIndex(f=>f[1]===name);assert.ok(i>=0,name);return source.slice(functions[i].index,functions[i+1]?.index||source.length);};
const decision={status:'AVAILABLE',data_asof:'2026-09-04',version:{status:'ACTIVE'},result:{recommendation:{positions:[]}}};
const controls={};
const calls=[];
let renderCount=0;
const makePanel=()=>({open:false,isConnected:true,children:{},append(node){this.children['.'+node.className]=node;},querySelector(selector){return this.children[selector];}});
controls['#harnessView']={set innerHTML(value){
  this.markup=value;renderCount++;
  controls['#quantDecisionOutput']={innerHTML:'',canUse:true};
  controls['#quickQuantRun']={disabled:false};
  controls['#quantAvailabilityStatus']={textContent:''};
  controls['#harnessAdvanced']=makePanel();
}};
const state={quantRequestSerial:0,quantDecision:null,quantDecisionKey:null,quantViewKey:null,harnessDetailsBusy:false,quantDraft:{capital:100000},dashboard:{meta:{market_session_open:false}}};
const context=vm.createContext({
  state,window:{setTimeout,clearTimeout},AbortController,
  $:key=>controls[key],document:{createElement:()=>({}),querySelector:()=>null},
  quantDecisionInput:()=>({...state.quantDraft}),quantDecisionPanelHtml:()=>'<input id="quickCapital"><button>查看推荐</button>',
  bindQuantDecisionInputs:()=>{},
  renderQuantOutcome:value=>{controls['#quantDecisionOutput'].innerHTML=JSON.stringify(value);},
  renderHarness:()=>{throw new Error('Unexpected advanced render');},updateHarnessWorkflowFields:()=>{},scheduleHarnessPoll:()=>{}
});
for(const name of ['quantInputKey','quantRequest','quantReadStatus','loadHarnessDetails','loadHarness'])vm.runInContext(extract(name),context);

(async()=>{
  let resolveDecision;
  context.request=async url=>{calls.push(url);if(url==='/api/harness')throw new Error('Background unavailable');return await new Promise(resolve=>{resolveDecision=resolve;});};
  const pending=context.loadHarness();
  assert.equal(renderCount,1,'form must render before any response');
  assert.equal(controls['#quickQuantRun'].disabled,false);
  assert.deepEqual(calls,['/api/quant/decision'],'optional background status must not block the main page');
  resolveDecision(decision);await pending;
  assert.match(controls['#quantDecisionOutput'].innerHTML,/2026-09-04/);
  assert.match(controls['#quantAvailabilityStatus'].textContent,/周末与节假日均可查看/);

  const savedHtml=controls['#quantDecisionOutput'].innerHTML;
  context.request=async()=>{throw new Error('Temporary network failure');};
  await context.loadHarness();
  assert.equal(controls['#quantDecisionOutput'].innerHTML,savedHtml);
  assert.match(controls['#quantAvailabilityStatus'].textContent,/保留上次成功结果/);
  assert.equal(controls['#quickQuantRun'].disabled,false);

  await context.loadHarnessDetails();
  assert.equal(controls['#quantDecisionOutput'].innerHTML,savedHtml);
  assert.match(controls['#harnessAdvanced'].children['.harness-details-status'].textContent,/上方股票推荐仍可使用/);
  assert.equal(state.harnessDetailsBusy,false);

  context.request=async()=>new Promise(resolve=>{resolveDecision=resolve;});
  const stale=context.loadHarness();
  state.quantDraft.capital=200000;state.quantRequestSerial++;
  resolveDecision({...decision,data_asof:'2099-01-01'});await stale;
  assert.doesNotMatch(controls['#quantDecisionOutput'].innerHTML,/2099/,'late responses for old conditions must be ignored');

  let signal;
  context.request=async(_url,options)=>{signal=options.signal;return new Promise(()=>{});};
  await assert.rejects(context.quantRequest('/api/quant/decision',{},5),/读取超时/);
  assert.equal(signal.aborted,true);
  console.log('PASS: form ready before data, closed-market cached result, optional status isolation, failed refresh retention, stale response protection, bounded timeout.');
})().catch(error=>{console.error(error);process.exitCode=1;});
