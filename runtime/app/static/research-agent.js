(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const api = (...args) => window.argusRequest(...args);
  const post = (url, body) => api(url, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const el = (tag, text, cls) => {const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;};
  const num = (v,d=2) => typeof v==='number' && Number.isFinite(v) ? v.toLocaleString('zh-CN',{maximumFractionDigits:d}) : '—';
  const pct = v => typeof v==='number' ? num(v*100,1)+'%' : '—';
  const terminal = new Set(['COMPLETED','FAILED','CANCELLED','NEEDS_INPUT','INTERRUPTED']);
  const forecastLabels={WALK_FORWARD_CALIBRATED_FULL:'请求期限已校准',WALK_FORWARD_CALIBRATED_PARTIAL:'部分期限通过校准',WALK_FORWARD_CALIBRATED_PARTIAL_WITH_BASELINE_EXTENSION:'短期已校准，长期为历史基准情景',BASELINE_SCENARIO_ONLY:'仅历史基准情景',UNAVAILABLE_NOT_CALIBRATED:'暂无可用预测'};
  const names={COMPLETED:'已完成',FAILED:'失败',CANCELLED:'已取消',NEEDS_INPUT:'待补充',INTERRUPTED:'已中断',QUEUED:'排队中',RUNNING:'研究中',WAITING:'等待下一轮',STOPPED:'已停止',DISABLED:'未启用'};
  let current=null, timer=null, loaded=false, loading=false;
  const list = (parent, items) => {if(!items?.length)return;const ul=el('ul');items.forEach(x=>ul.append(el('li',x)));parent.append(ul);};
  function message(text, error=false){const box=$('agentProgress');box.hidden=false;box.replaceChildren(el('p',text,error?'agent-error':''));}
  async function refreshStatus(){
    const [state, services]=await Promise.all([api('/api/research-agent'),api('/api/services')]);
    $('agentConnection').textContent=state.runtime.codex.ready?'● Codex 已连接':'○ '+state.runtime.codex.detail;
    const serviceBox=$('agentServices');serviceBox.replaceChildren();
    for(const s of services.runtime.services){const row=el('div',undefined,'agent-service');row.append(el('span',s.label),el('span',s.key==='codex'&&!services.agent.current_run?'待命':s.state==='RUNNING'?'运行中':names[s.state]||s.state,s.alive?'':'bad'));row.title=(s.heartbeat_at?'最近心跳 '+s.heartbeat_at:'')+(s.last_error?' · '+s.last_error:'');serviceBox.append(row);}
    const evo=$('agentEvolution'),l=services.learning,e=services.evolution; evo.replaceChildren();
    evo.append(el('p',`预测已评分 ${num(l.predictions?.scored,0)} 条 · 待评分 ${num(l.predictions?.pending,0)} 条`,'agent-muted'));
    evo.append(el('p',`最近学习：${l.status||'暂无'} · 数据 ${l.data_asof||'未知'}`,'agent-muted'));
    evo.append(el('p',`源码候选：${e.last_status||'暂无'}；${e.rollback_available?'支持版本回滚':'暂无可回滚版本'}`,'agent-muted'));
    if(e.gate){const passed=Object.values(e.gate).filter(v=>v===true).length;evo.append(el('p',`评测检查：${passed}/${Object.keys(e.gate).length} 项通过。候选必须通过测试和样本外门槛后才能晋级。`,'agent-muted'));}
    if(l.errors?.length)evo.append(el('p',`最近学习有 ${l.errors.length} 项数据或训练异常，结果保留缺失标记。`,'agent-error'));
    const history=$('agentHistory');history.replaceChildren();
    for(const run of state.runs){const b=el('button',run.question.slice(0,70),'agent-history-item');b.type='button';b.append(el('small',`${names[run.status]||run.status} · ${new Date(run.created_at).toLocaleString('zh-CN')}`));b.onclick=()=>openRun(run.run_key);history.append(b);}
    if(!state.runs.length)history.append(el('p','还没有研究记录','agent-muted'));
    if(!current && state.runs.length && !terminal.has(state.runs[0].status))await openRun(state.runs[0].run_key);
  }
  function renderProgress(run){
    const box=$('agentProgress');box.hidden=false;box.replaceChildren();
    const header=el('div',undefined,'agent-panel-title');header.append(el('h3',names[run.status]||run.status));
    if(!terminal.has(run.status)){const cancel=el('button','取消研究');cancel.type='button';cancel.onclick=async()=>{await post(`/api/research-agent/runs/${run.run_key}/cancel`,{});await openRun(run.run_key);};header.append(cancel);}
    box.append(header);for(const event of run.events.slice(-7))box.append(el('div',event.message,'agent-progress-event'));
    if(run.error)box.append(el('p',run.error,'agent-error'));
    if(['FAILED','INTERRUPTED'].includes(run.status)){const retry=el('button','重试这项研究','secondary-button');retry.onclick=()=>submit(run.question,run.parent_key);box.append(retry);}
  }
  const svgNode=(tag, attrs={})=>{const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,String(v));return n;};
  function forecastChart(points, caption, prefix='¥'){
    const figure=el('figure',undefined,'agent-chart');figure.append(el('figcaption',caption));
    const rows=(points||[]).filter(p=>['p10','p50','p90','trading_day'].every(k=>Number.isFinite(p[k])));
    if(rows.length<2){figure.append(el('p','数据不足，暂不绘制未来区间。','agent-muted'));return figure;}
    const w=760,h=270,pad={l:77,r:28,t:22,b:42},maxDay=Math.max(...rows.map(p=>p.trading_day));
    let lo=Math.min(...rows.map(p=>p.p10)),hi=Math.max(...rows.map(p=>p.p90));const span=hi-lo||Math.max(1,hi*.1);lo-=span*.1;hi+=span*.1;
    const x=v=>pad.l+(w-pad.l-pad.r)*(v/(maxDay||1)),y=v=>h-pad.b-(h-pad.t-pad.b)*(v-lo)/(hi-lo);
    const svg=svgNode('svg',{viewBox:`0 0 ${w} ${h}`,role:'img','aria-label':caption});
    for(let i=0;i<5;i++){const value=lo+(hi-lo)*i/4,yy=y(value);svg.append(svgNode('line',{x1:pad.l,y1:yy,x2:w-pad.r,y2:yy,stroke:'#29404f','stroke-dasharray':'3 6'}));const t=svgNode('text',{x:pad.l-10,y:yy+4,fill:'#87a7b8','font-size':11,'text-anchor':'end'});t.textContent=prefix+num(value,0);svg.append(t);}
    const path=(data,key)=>data.map((r,i)=>`${i?'L':'M'}${x(r.trading_day).toFixed(2)},${y(r[key]).toFixed(2)}`).join(' ');
    const band=path(rows,'p90')+' '+rows.slice().reverse().map(r=>`L${x(r.trading_day).toFixed(2)},${y(r.p10).toFixed(2)}`).join(' ')+' Z';
    svg.append(svgNode('path',{d:band,fill:'#31d6a0','fill-opacity':.13}));svg.append(svgNode('path',{d:path(rows,'p50'),fill:'none',stroke:'#59dbb6','stroke-width':2.5}));
    for(const r of rows){const c=svgNode('circle',{cx:x(r.trading_day),cy:y(r.p50),r:4,fill:r.validated===false?'#e5b46c':'#6aebc6'});const title=svgNode('title');title.textContent=`第 ${r.trading_day} 个交易日 · P10 ${num(r.p10)} / P50 ${num(r.p50)} / P90 ${num(r.p90)}`;c.append(title);svg.append(c);}
    for(const d of [0,Math.round(maxDay/2),maxDay]){const t=svgNode('text',{x:x(d),y:h-14,fill:'#87a7b8','font-size':11,'text-anchor':'middle'});t.textContent=d===0?'数据时点':`+${d}交易日`;svg.append(t);}
    figure.append(svg,el('figcaption','绿色带：P10–P90 区间；中线：P50。黄色圆点为历史基准情景，绿色圆点为已校准期限。悬停查看数值。'));return figure;
  }
  function renderResult(run){
    const root=$('agentResult');root.replaceChildren();const plan=run.plan||{},e=run.evidence||{},r=run.report||{};
    if(run.stale_analysis||(e.request&&e.calculation_version!=='continuous-data-v1')){root.append(el('h3','这份历史报告需要重新计算'),el('p','报告生成后，行情连续性检查已更新。为避免显示旧的异常区间，请重新运行研究；原始报告保留在研究历史中。','agent-assumptions'));const retry=el('button','按当前数据重新研究','primary-button');retry.onclick=()=>submit(run.question,run.parent_key);root.append(retry);return;}
    if(run.status==='NEEDS_INPUT'){root.append(el('h3','补充一点信息'));list(root,plan.questions?.length?plan.questions:['请说明关注的 A 股股票或板块。']);$('agentFollowup').checked=true;return;}
    if(!e.request && !r.summary)return;
    root.append(el('h2',plan.title||'投资研究'),el('p',r.summary||'计算证据已保存，正在生成分析报告…','agent-summary'));
    const req=e.request||{},kpis=el('div',undefined,'agent-kpis');for(const [label,value]of [['研究本金',num(req.capital)+' 元'],['投资期限',num(req.horizon_months,0)+' 个月'],['最大回撤约束',num(req.max_drawdown_pct)+'%'],['行情截至',e.data_asof||'未知']]){const card=el('div',undefined,'agent-kpi');card.append(el('small',label),el('strong',value));kpis.append(card);}root.append(kpis);
    if(plan.assumptions?.length){const box=el('div',undefined,'agent-assumptions');box.append(el('strong','本次采用的假设'));list(box,plan.assumptions);root.append(box);}
    for(const section of r.sections||[]){root.append(el('h3',section.title),el('p',section.analysis));const refs=el('div');for(const id of section.evidence_ids)refs.append(el('span',id,'agent-citation'));root.append(refs);}
    const pf=e.portfolio_forecast;if(pf?.curve?.length){root.append(el('h3','组合资金情景'));root.append(forecastChart(pf.curve,'组合资金路径 · '+(pf.horizon_reason||'依据共同有效期限计算')));}
    const stocks=e.stocks||[];if(stocks.length){root.append(el('h3','个股区间与研究分配'));const selector=el('select');selector.setAttribute('aria-label','选择股票查看预测区间');for(const stock of stocks){const o=el('option',`${stock.name||stock.symbol} · ${stock.symbol}`);o.value=stock.symbol;selector.append(o);}const chartBox=el('div'),control=el('div',undefined,'agent-chart');control.append(selector);root.append(control,chartBox);
      const renderStock=()=>{chartBox.replaceChildren();const s=stocks.find(x=>x.symbol===selector.value),f=s.forecast||{};chartBox.append(forecastChart(f.forecast_curve,`${s.name||s.symbol} · ${forecastLabels[f.validation_status]||'暂无验证结果'}${f.horizon_reason?' · '+f.horizon_reason:''}`));
        const wrap=el('div',undefined,'agent-table'),table=el('table'),head=el('tr');for(const label of ['期限','依据','P10','P50','P90'])head.append(el('th',label));const thead=el('thead');thead.append(head);table.append(thead);const body=el('tbody');for(const t of f.timeframes||[]){const row=el('tr');for(const value of [t.label,t.status==='AVAILABLE'?(t.period.endsWith('m')&&t.period!=='1mo'?'历史描述': '样本外校准'):t.status==='BASELINE_REFERENCE'?'历史基准情景':'数据不足',num(t.p10_price),num(t.p50_price),num(t.p90_price)])row.append(el('td',value));row.title=t.reason||'';body.append(row);}table.append(body);wrap.append(table);chartBox.append(wrap);};selector.onchange=renderStock;renderStock();
      for(const s of stocks.filter(x=>Number.isFinite(x.amount))){const row=el('div',undefined,'agent-allocation-row'),bar=el('div',undefined,'agent-allocation-bar'),fill=el('i');fill.style.width=Math.max(0,Math.min(100,s.amount/(req.capital||1)*100))+'%';bar.append(fill);row.append(el('span',s.name||s.symbol),bar,el('span',num(s.amount)+' 元'));root.append(row);}root.append(el('p','金额和股数为研究分配；未通过组合样本外评估时仅作待验证方案。','agent-muted'));
    }
    root.append(el('h3','风险与下一步'));list(root,[...(r.risks||[]),...(r.next_steps||[])]);
    if(e.limitations?.length){const details=el('details');details.append(el('summary','数据与模型限制'));list(details,e.limitations);root.append(details);}
    const sources=el('div',undefined,'agent-sources');sources.append(el('h3','证据来源'));for(const s of e.sources||[])sources.append(el('div',`[${s.id}] ${s.label}${s.data_asof?' · '+s.data_asof:''}`));root.append(sources);
    if(terminal.has(run.status)){const exportButton=el('button','导出研究 JSON','secondary-button');exportButton.onclick=()=>{const blob=new Blob([JSON.stringify(run,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=el('a');a.href=url;a.download=run.run_key+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};root.append(exportButton);}
  }
  async function openRun(key){clearTimeout(timer);try{const run=await api('/api/research-agent/runs/'+key);current=run;$('agentSubmit').disabled=!terminal.has(run.status);renderProgress(run);renderResult(run);if(!terminal.has(run.status))timer=setTimeout(()=>openRun(key),2000);else await refreshStatus();}catch(error){message(error.message,true);timer=setTimeout(()=>openRun(key),5000);}}
  async function submit(question,parent){$('agentSubmit').disabled=true;try{const run=await post('/api/research-agent/runs',{question,parent_key:parent||null});await openRun(run.run_key);}catch(error){message(error.message,true);$('agentSubmit').disabled=false;}}
  $('agentForm').onsubmit=event=>{event.preventDefault();submit($('agentQuestion').value,$('agentFollowup').checked?current?.run_key:null);};
  document.querySelectorAll('[data-example]').forEach(button=>button.onclick=()=>{$('agentQuestion').value=button.dataset.example;$('agentQuestion').focus();});
  $('agentRefresh').onclick=()=>refreshStatus().catch(error=>message(error.message,true));
  $('agentEvolve').onclick=async()=>{const b=$('agentEvolve');b.disabled=true;try{const result=await post('/api/research-agent/evolve',{});$('agentEvolutionMessage').textContent='学习任务已提交：'+(result.run?.run_key||'正在运行');await refreshStatus();}catch(error){$('agentEvolutionMessage').textContent=error.message;}finally{b.disabled=false;}};
  window.loadResearchAgent=async()=>{if(loading)return;loading=true;try{await refreshStatus();if(!loaded&&!current)$('agentResult').append(el('div','描述你的投资想法，或选择上方示例开始。','agent-empty'));loaded=true;}catch(error){message(error.message,true);}finally{loading=false;}};
  if(new URLSearchParams(location.search).get('view')==='agent')window.loadResearchAgent();
  setInterval(()=>{if($('agentView').classList.contains('active'))refreshStatus().catch(()=>{});},30000);
})();
