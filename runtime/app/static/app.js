const state = { dashboard: null, selected: null, chart: null, period: '1d', chartWindow: null,
  colorConvention: localStorage.getItem('argus-color-convention') || 'redUp',
  chartDrag: null, strategyFocus:null, factorFocus:null,
  factorProfile:localStorage.getItem('argus-factor-profile')||'balanced', realtimeBusy:false,
  reportPollTimer:null, bulkReportPollTimer:null, harnessPollTimer:null,
  overviewAssets:{}, libraryTab:'sources', libraryResults:[], logicData:null,
  portfolioImportMode:'manual', portfolioPreview:null,
  harnessRunKey:null, harnessWorkflow:'quant_portfolio', quantDecision:null, quantDraft:{name:'白酒新能源半导体航天2%组合',capital:100000,
    horizon_months:12,target_return_pct:2,max_drawdown_pct:15,stop_loss_pct:8,
    take_profit_pct:20,trailing_stop_pct:8,sectors:'白酒,新能源,半导体,航空',stocks:'',max_candidates:30,
    max_positions:2,risk_profile:'balanced',max_iterations:10,backtest_window_years:3,
    strategy_style:'auto',preference_weights:{trend:25,fundamental:30,probability:20,liquidity:10,stability:15}} };
try{Object.assign(state.quantDraft,JSON.parse(localStorage.getItem('argus-quant-draft')||'{}'));}catch{}
const HARNESS_ACTIVE_POLL_MS=60*60*1000;
const $ = (selector) => document.querySelector(selector);
const money = (value, digits = 2) => Number(value).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
const pct = (value) => `${Number(value) >= 0 ? '+' : ''}${Number(value).toFixed(2)}%`;
const safe = (value) => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const plainQuantText = value => String(value ?? '')
  .replaceAll('参考收益达到目标，但未通过非退化门禁，仅列为研究候选', '历史参考收益达到目标，但稳定性检查没有全部通过，仅供重点关注')
  .replaceAll('最大回撤合格，但未通过非退化门禁，仅列为研究候选', '历史最大跌幅符合要求，但稳定性检查没有全部通过，仅供重点关注')
  .replaceAll('已评测模型的快照推荐', '已完成评估的数据')
  .replaceAll('最近交易日模型快照推荐', '最近交易日评估结果')
  .replaceAll('最近交易日模型快照', '最近交易日评估结果')
  .replaceAll('快照推荐', '最近交易日推荐')
  .replaceAll('激进档', '激进风险偏好')
  .replaceAll('平衡档', '中立风险偏好')
  .replaceAll('保守档', '保守风险偏好')
  .replaceAll('试探推荐', '重点关注')
  .replaceAll('（非正式持仓）', '（尚未达到正式推荐条件）')
  .replaceAll('非正式持仓', '尚未达到正式推荐条件')
  .replaceAll('当前建议 0 只股票', '目前没有达到正式推荐条件的股票')
  .replaceAll('非退化门禁', '没有明显变差的检查条件')
  .replaceAll('正式门禁', '正式推荐条件')
  .replaceAll('仅列为研究候选', '仅供重点关注')
  .replaceAll('研究候选', '重点关注对象');
const plainQuantReason = value => {
  const text=String(value||'');
  if(text.includes('趋势或动量未通过'))return '近期走势不够强';
  if(text.includes('在线模型上涨概率未通过'))return '系统估算的上涨概率偏低';
  if(text.includes('无可用基本面快照'))return '暂时没有可用的财务数据';
  if(text.includes('基本面综合分未达到'))return '财务评分未达到当前风险偏好的要求';
  if(text.includes('基本面覆盖率'))return '财务数据不够完整';
  return text.replaceAll('基本面','财务情况').replaceAll('模型','系统评估').replaceAll('门禁','条件').replaceAll('门槛','要求');
};
const plainQuantPillar = value => ({'趋势':'近期走势','在线模型':'上涨概率','公告日基本面':'财务情况'}[value]||plainQuantReason(value));
const stockEvidenceDestination = (label, symbol) => {
  const normalizedSymbol=String(symbol||'').replace(/\.(SH|SZ|BJ)$/i,''),encodedSymbol=encodeURIComponent(String(symbol||''));
  const marketPrefix=/^[569]/.test(normalizedSymbol)?'SH':/^[0123]/.test(normalizedSymbol)?'SZ':'BJ';
  const eastmoneyCode=encodeURIComponent(`${marketPrefix}${normalizedSymbol}`);
  if(label==='公司赚不赚钱、增长快不快'||label==='现金够不够、负债重不重')return{href:`https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/Index?type=web&code=${eastmoneyCode}`,text:'查公开财务表',title:'打开该股票的东方财富 F10 财务分析，查看利润表、资产负债表和现金流量表'};
  if(label==='现在的价格贵不贵')return{href:`https://emweb.securities.eastmoney.com/PC_HSF10/IndustryAnalysis/Index?type=web&code=${eastmoneyCode}`,text:'查公开估值页',title:'打开该股票的同行比较和估值比较资料'};
  if(label==='最近价格走势强不强'||label==='成交是否活跃、是否容易买卖')return{href:`/?view=overview&asset=${encodedSymbol}&period=1d`,text:'看本地行情',title:'打开该股票保存在本地的日线、成交量和成交额数据'};
  if(label==='模型估算上涨可能性')return{href:`/?view=logic&symbol=${encodedSymbol}`,text:'看计算方法',title:'打开盘后模型的输入、公式和当前参数'};
  return{href:`/?view=library&query=${encodedSymbol}`,text:'查公告与研报',title:'搜索该股票的公告、新闻和研报，并继续打开原文'};
};
function enhanceStockProfileLinks(root=document){
  const profiles=root.matches?.('details.stock-profile')?[root]:[...root.querySelectorAll?.('details.stock-profile')||[]];
  profiles.forEach(profile=>profile.querySelectorAll('li:not([data-evidence-linked])').forEach(item=>{
    const symbol=item.closest('tr')?.querySelector('td:nth-child(2) small')?.textContent?.trim();
    const label=item.querySelector('b')?.textContent?.trim();
    if(!symbol||!label)return;
    const destination=stockEvidenceDestination(label,symbol),link=document.createElement('a');
    link.className='stock-evidence-link';link.href=destination.href;link.target='_blank';link.rel='noopener noreferrer';
    link.textContent=`${destination.text} ↗`;link.title=destination.title;link.setAttribute('aria-label',`${label}：${destination.text}（新标签页打开）`);
    item.dataset.evidenceLinked='true';item.append(link);
  }));
}
const quantVersionLabel = value => ({RULE_SNAPSHOT:'即时预测结果',SNAPSHOT:'最近交易日结果',ACTIVE:'正式结果',ARCHIVED:'历史结果',REJECTED:'未采用',UNCHANGED:'结果未变化'}[value]||plainQuantText(value||'—'));
const localTimestamp = value => {
  const text = String(value || '');
  const parsed = new Date(text);
  if (!text || Number.isNaN(parsed.getTime())) return text.slice(0, 19).replace('T', ' ');
  return new Intl.DateTimeFormat('zh-CN', {year:'numeric',month:'2-digit',day:'2-digit',
    hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23',timeZone:'Asia/Shanghai'}).format(parsed).replaceAll('/', '-');
};
const beijingTimestamp = value => value ? `${localTimestamp(value)} 北京时间` : '尚无成功记录';
const chartTrendColor = value => Number(value)>=0?(state.colorConvention==='redUp'?'#ff5e6c':'#31d6a0'):(state.colorConvention==='redUp'?'#31d6a0':'#ff5e6c');

async function request(url, options = {}, retried = false) {
  const headers = new Headers(options.headers || {});
  const token = sessionStorage.getItem('argus-remote-token');
  if (token) headers.set('X-Argus-Token', token);
  const response = await fetch(url, {...options, headers});
  let payload = {};
  try { payload = await response.json(); } catch { payload = {error:`HTTP ${response.status}`}; }
  if (response.status === 401 && !retried) {
    const supplied = window.prompt('请输入 Rooftop 远程访问令牌');
    if (supplied) {
      sessionStorage.setItem('argus-remote-token', supplied.trim());
      return request(url, options, true);
    }
  }
  if (response.status === 403 && token) sessionStorage.removeItem('argus-remote-token');
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}
window.argusRequest = request;

function normalizeChartPayload(payload, requestedPeriod) {
  if (Array.isArray(payload.periods) && Array.isArray(payload.series)) return payload;
  // Compatibility with a Python process started before chart API v2.
  if (Array.isArray(payload.prices)) {
    const series = payload.prices.map((point, index, rows) => {
      const closes = rows.slice(Math.max(0, index - 4), index + 1).map(row => Number(row.close));
      return {...point, time:point.trade_date, amount:point.amount ?? null,
        ma5:closes.length === 5 ? closes.reduce((sum, value) => sum + value, 0) / 5 : null};
    });
    return {...payload, quote:payload.quote || null, periods:[{key:'1d', label:'日K（旧后台）'}],
      period:'1d', period_label:'日K', chart_type:'candlestick', series,
      supports_ma5:true, legacy_backend:true, requested_period:requestedPeriod};
  }
  throw new Error('图表接口响应不完整：缺少 periods/series，请重启本地后台');
}

function normalizeDashboard(payload) {
  const portfolio = payload.portfolio || {};
  return {...payload, meta:payload.meta || {}, markets:Array.isArray(payload.markets) ? payload.markets : [],
    analysis_assets:Array.isArray(payload.analysis_assets) ? payload.analysis_assets : [],
    hypotheses:Array.isArray(payload.hypotheses) ? payload.hypotheses : [],
    evidence:Array.isArray(payload.evidence) ? payload.evidence : [],
    risk_policies:Array.isArray(payload.risk_policies) ? payload.risk_policies : [],
    strategy_lab:{...(payload.strategy_lab||{}),
      strategies:Array.isArray(payload.strategy_lab?.strategies) ? payload.strategy_lab.strategies : [],
      factors:Array.isArray(payload.strategy_lab?.factors) ? payload.strategy_lab.factors : [],
      backtests:Array.isArray(payload.strategy_lab?.backtests) ? payload.strategy_lab.backtests : []},
    intelligence_sources:Array.isArray(payload.intelligence_sources) ? payload.intelligence_sources : [],
    source_health:Array.isArray(payload.source_health) ? payload.source_health : [],
    portfolio:{...portfolio, positions:Array.isArray(portfolio.positions) ? portfolio.positions : [],
      market_value:portfolio.market_value == null ? null : Number(portfolio.market_value),
      unrealized_pnl:portfolio.unrealized_pnl == null ? null : Number(portfolio.unrealized_pnl),
      priced_positions:Number(portfolio.priced_positions||0),total_positions:Number(portfolio.total_positions||0)}};
}

function showToast(message) {
  const toast = $('#toast');
  toast.textContent = message;
  toast.classList.add('show');
  window.setTimeout(() => toast.classList.remove('show'), 2600);
}

function renderSummary(portfolio, evidenceCoverage = null) {
  const hasPositions = portfolio.has_positions == null ? portfolio.positions.length > 0 : Boolean(portfolio.has_positions);
  const fullyValued = hasPositions && portfolio.valuation_status === 'COMPLETE' && portfolio.market_value != null;
  const value = $('#portfolioValue');
  const pnl = $('#portfolioPnl');
  if (!hasPositions) {
    value.textContent = '无持仓';
    $('#portfolioValueDetail').textContent = '未录入真实持仓';
    pnl.textContent = '—';
    pnl.className = '';
    $('#portfolioPnlDetail').textContent = '无持仓时不计算';
    $('#disciplineState').textContent = '未启用';
    $('#disciplineDetail').textContent = '录入真实持仓后计算';
  } else if (!fullyValued) {
    value.textContent = '待行情';
    $('#portfolioValueDetail').textContent = `${portfolio.priced_positions}/${portfolio.total_positions} 只价格已核验，暂不汇总`;
    pnl.textContent = '—';
    pnl.className = '';
    $('#portfolioPnlDetail').textContent = '不会用成本价代替当前价格';
    const alerting = portfolio.positions.filter(position => position.discipline && position.discipline.severity !== 'normal');
    $('#disciplineState').textContent = alerting.length ? `${alerting.length} 项需复核` : '待行情';
    $('#disciplineDetail').textContent = '可靠行情补齐后计算全部持仓';
  } else {
    value.textContent = `¥ ${money(portfolio.market_value)}`;
    $('#portfolioValueDetail').textContent = `人民币 · 行情截至 ${localTimestamp(portfolio.price_as_of)}`;
    pnl.textContent = `${portfolio.unrealized_pnl >= 0 ? '+' : ''}¥ ${money(portfolio.unrealized_pnl)}`;
    pnl.className = portfolio.unrealized_pnl >= 0 ? 'positive' : 'negative';
    $('#portfolioPnlDetail').textContent = '按用户确认的数量和成本计算';
    const alerting = portfolio.positions.filter(p => p.discipline && p.discipline.severity !== 'normal');
    $('#disciplineState').textContent = alerting.length ? `${alerting.length} 项需复核` : '纪律区间内';
    $('#disciplineDetail').textContent = alerting.length ? '出现研究/止盈止损提示' : '未触发当前规则阈值';
  }
  if (!hasPositions) {
    $('#portfolioSourceState').textContent = portfolio.source_type === 'MANUAL_CLEAR' ? '已清空' : '未导入';
    $('#portfolioSourceDetail').textContent = portfolio.source_type === 'MANUAL_CLEAR' ? `用户确认 · ${portfolio.as_of||'未知日期'}` : '仅接受用户确认的数据';
  } else {
    $('#portfolioSourceState').textContent = portfolio.verification_status === 'USER_CONFIRMED' ? '已确认' : '待确认';
    $('#portfolioSourceDetail').textContent = `${portfolio.source_name||'本地导入'} · ${portfolio.as_of||'未知日期'}`;
  }
  const coverage = evidenceCoverage == null ? null : Number(evidenceCoverage);
  $('#evidenceCoverage').textContent = coverage == null ? '暂无' : `${Math.round(coverage * 100)}%`;
  $('#evidenceCoverageDetail').textContent = coverage == null ? '尚无可计算的研究报告' : '来自已保存研究报告';
}

function renderPositions(portfolio) {
  const positions = portfolio.positions;
  $('#positionCount').textContent = `${positions.length} 项`;
  $('#portfolioClear').hidden = !positions.length;
  $('#portfolioMeta').textContent = positions.length
    ? `${portfolio.name||'我的本地账户'} · ${portfolio.source_name||'本地导入'} · 持仓日期 ${portfolio.as_of||'未知'} · 行情核验 ${portfolio.priced_positions}/${portfolio.total_positions}`
    : portfolio.source_type === 'MANUAL_CLEAR' ? `已于 ${portfolio.as_of||'最近一次操作'} 清空真实持仓` : '尚未导入真实持仓';
  $('#positionsBody').innerHTML = positions.length ? positions.map(position => `
    <tr>
      <td><strong>${safe(position.name)}</strong><small>${safe(position.symbol)} · ${safe(position.source_name||'本地导入')}</small></td>
      <td>${money(position.quantity, 0)}</td>
      <td>${position.market_value==null?'<span class="data-pending">待行情</span>':`¥ ${money(position.market_value)}`}</td>
      <td class="${position.pnl_pct==null?'':position.pnl_pct >= 0 ? 'positive' : 'negative'}">${position.pnl_pct==null?'—':pct(position.pnl_pct)}</td>
      <td><span class="status-tag ${position.discipline?'':'pending'}">${position.discipline?safe(actionLabel(position.discipline.action)):'等待可靠行情'}</span></td>
    </tr>`).join('') : '<tr><td colspan="5" class="positions-empty">尚未录入真实持仓；系统不会推测账户金额。</td></tr>';
}

function resetPortfolioPreview() {
  state.portfolioPreview = null;
  $('#portfolioPreview').hidden = true;
  $('#portfolioConfirmButton').hidden = true;
  $('#portfolioPreviewButton').hidden = false;
}

function showPortfolioErrors(errors) {
  const view = $('#portfolioImportErrors');
  if (!errors?.length) {
    view.hidden = true;
    view.innerHTML = '';
    return;
  }
  view.hidden = false;
  view.innerHTML = errors.map(item => `<p>${safe(item.message||item)}</p>`).join('');
}

function setPortfolioMode(mode) {
  state.portfolioImportMode = mode;
  document.querySelectorAll('[data-portfolio-mode]').forEach(button => {
    const active = button.dataset.portfolioMode === mode;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  });
  $('#portfolioManualPane').hidden = mode !== 'manual';
  $('#portfolioCsvPane').hidden = mode !== 'csv';
  resetPortfolioPreview();
  showPortfolioErrors([]);
}

function addPortfolioManualRow() {
  const row = document.createElement('tr');
  row.innerHTML = '<td><input data-portfolio-field="symbol" inputmode="numeric" maxlength="12" placeholder="例如 600519" aria-label="股票代码"></td><td><input data-portfolio-field="name" maxlength="80" placeholder="可选" aria-label="股票名称"></td><td><input data-portfolio-field="quantity" type="number" min="1" step="1" placeholder="100" aria-label="持仓数量"></td><td><input data-portfolio-field="cost_price" type="number" min="0.000001" step="0.000001" placeholder="100.00" aria-label="成本价"></td><td><button class="icon-button portfolio-remove-row" type="button" title="删除此行" aria-label="删除此行">×</button></td>';
  row.querySelector('.portfolio-remove-row').onclick = () => { row.remove(); resetPortfolioPreview(); };
  row.querySelectorAll('input').forEach(input => input.addEventListener('input', resetPortfolioPreview));
  $('#portfolioManualRows').append(row);
}

function portfolioManualPositions() {
  return [...$('#portfolioManualRows').querySelectorAll('tr')].map(row => Object.fromEntries(
    [...row.querySelectorAll('[data-portfolio-field]')].map(input => [input.dataset.portfolioField, input.value.trim()])
  )).filter(item => Object.values(item).some(Boolean));
}

function readPortfolioCsv(file, encoding) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result||''));
    reader.onerror = () => reject(new Error('无法读取 CSV 文件'));
    reader.readAsText(file, encoding);
  });
}

function renderPortfolioPreview(preview) {
  const reconciliation = preview.reconciliation;
  $('#portfolioPreviewSummary').innerHTML = `<strong>对账结果</strong><span>新增 ${reconciliation.added}</span><span>变更 ${reconciliation.changed}</span><span>移除 ${reconciliation.removed}</span><span>不变 ${reconciliation.unchanged}</span>`;
  $('#portfolioPreviewRows').innerHTML = preview.rows.map(row => {
    const valuation=row.valuation||{},verified=valuation.price!=null;
    return `<tr><td><strong>${safe(row.name)}</strong><small>${safe(row.symbol)}</small></td><td>${money(row.quantity,0)}</td><td>¥ ${money(row.cost_price,3)}</td><td>${verified?`<span class="verified-price">¥ ${money(valuation.price,3)} · ${safe(valuation.source)}</span><small>${safe(String(valuation.observed_at||'').slice(0,19).replace('T',' '))}</small>`:'<span class="data-pending">暂无可靠行情，不计算市值</span>'}</td></tr>`;
  }).join('');
  $('#portfolioPreview').hidden = false;
  $('#portfolioPreviewButton').hidden = true;
  $('#portfolioConfirmButton').hidden = false;
}

function bindPortfolioImport() {
  const dialog = $('#portfolioImportDialog');
  $('#portfolioAsOf').value = new Date().toLocaleDateString('sv-SE');
  addPortfolioManualRow();
  document.querySelectorAll('[data-portfolio-mode]').forEach(button => button.onclick = () => setPortfolioMode(button.dataset.portfolioMode));
  $('#portfolioAddRow').onclick = addPortfolioManualRow;
  $('#portfolioImportOpen').onclick = () => {
    const portfolio=state.dashboard?.portfolio;
    if(portfolio?.name&&portfolio.source_type!=='UNSET')$('#portfolioAccountName').value=portfolio.name;
    if(!$('#portfolioManualRows').children.length)addPortfolioManualRow();
    resetPortfolioPreview();showPortfolioErrors([]);dialog.showModal();
  };
  const close = () => dialog.close();
  $('#portfolioImportClose').onclick = close;
  $('#portfolioCancel').onclick = close;
  $('#portfolioImportForm').addEventListener('input', resetPortfolioPreview);
  $('#portfolioImportForm').addEventListener('submit', async event => {
    event.preventDefault();
    const button=$('#portfolioPreviewButton');button.disabled=true;button.textContent='校验中…';
    try{
      const payload={mode:state.portfolioImportMode,account_name:$('#portfolioAccountName').value,as_of:$('#portfolioAsOf').value};
      if(state.portfolioImportMode==='manual')payload.positions=portfolioManualPositions();
      else{
        const file=$('#portfolioCsvFile').files[0];if(!file)throw new Error('请选择 CSV 文件');
        payload.filename=file.name;payload.csv_text=await readPortfolioCsv(file,$('#portfolioCsvEncoding').value);
      }
      const preview=await request('/api/portfolio/imports/preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      resetPortfolioPreview();showPortfolioErrors(preview.errors||[]);
      if(preview.can_confirm){state.portfolioPreview=preview;renderPortfolioPreview(preview);}
    }catch(error){resetPortfolioPreview();showPortfolioErrors([{message:error.message}]);}
    finally{button.disabled=false;button.textContent='预览并对账';}
  });
  $('#portfolioConfirmButton').onclick = async () => {
    if(!state.portfolioPreview)return;
    const button=$('#portfolioConfirmButton');button.disabled=true;button.textContent='写入中…';
    try{
      await request('/api/portfolio/imports/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({preview_id:state.portfolioPreview.preview_id,confirmed:true})});
      dialog.close();showToast('真实持仓已确认写入');await initialize();
    }catch(error){showPortfolioErrors([{message:error.message}]);}
    finally{button.disabled=false;button.textContent='确认写入';}
  };
  $('#portfolioClear').onclick = async () => {
    if(!window.confirm('确认清空当前真实持仓？历史导入记录会保留用于审计。'))return;
    try{await request('/api/portfolio/clear',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirmed:true})});showToast('当前真实持仓已清空');await initialize();}
    catch(error){showToast(`清空失败：${error.message}`);}
  };
  $('#portfolioTemplateDownload').onclick = () => {
    const blob=new Blob(['\ufeff股票代码,股票名称,持仓数量,成本价\n600519,贵州茅台,100,1200.00\n'],{type:'text/csv;charset=utf-8'}),url=URL.createObjectURL(blob),link=document.createElement('a');
    link.href=url;link.download='Rooftop-持仓导入模板.csv';link.click();URL.revokeObjectURL(url);
  };
}

function renderHypotheses(hypotheses) {
  $('#hypothesisList').innerHTML = hypotheses.map(h => `
    <article class="hypothesis"><h3><span>（待验证观点）</span>${safe(h.title)}</h3><p>${safe(h.statement)}</p></article>`).join('');
}

function actionLabel(action) {
  return ({SELL_REVIEW:'已到止损价，请复核',TAKE_PROFIT_REVIEW:'已到止盈区，请复核',BUY_RESEARCH:'可继续研究',HOLD_DISCIPLINE:'未触发风险线'})[action] || action;
}

async function selectAsset(symbol) {
  const position = state.dashboard.portfolio.positions.find(p => p.symbol === symbol);
  const market = state.dashboard.markets.find(p => p.symbol === symbol);
  const analysis = state.dashboard.analysis_assets.find(p => p.symbol === symbol);
  const remembered=state.overviewAssets[symbol];
  state.selected = position || {symbol, name: remembered?.name || analysis?.name || market?.name || symbol,
    current_price:analysis?.current_price ?? market?.price,
    pnl_pct:analysis?.change_pct ?? market?.change_pct ?? 0,
    discipline: {action:'HOLD_DISCIPLINE',severity:'normal',reason:'非持仓标的，仅展示行情'}};
  const chartResponse = await request(`/api/assets/${encodeURIComponent(symbol)}/chart?period=${encodeURIComponent(state.period)}`);
  state.chart = normalizeChartPayload(chartResponse, state.period);
  if(state.chart.asset?.name)state.selected.name=state.chart.asset.name;
  state.overviewAssets[symbol]={symbol,name:state.selected.name};
  if($('#assetInput'))$('#assetInput').value=`${state.selected.name} ${symbol}`;
  if (state.chart.legacy_backend) {
    state.period = '1d';
    $('#warning').textContent = '检测到仍在运行的旧版后台：当前已安全降级为日K。请停止旧进程并重新运行 run.ps1，以启用全部周期、搜索和策略页面。';
    $('#warning').classList.add('negative');
  }
  $('#assetName').textContent = state.selected.name;
  const livePrice = state.chart.quote ? state.chart.quote.price : state.selected.current_price;
  $('#assetPrice').textContent = Number(livePrice)>0 ? `¥ ${money(livePrice, 3)}` : '待行情';
  const displayChange = state.chart.quote ? state.chart.quote.change_pct : state.selected.pnl_pct;
  $('#assetPnl').textContent = displayChange==null ? '—' : pct(displayChange);
  $('#assetPnl').className = 'change-pill';
  $('#assetPnl').style.color = chartTrendColor(displayChange || 0);
  renderPeriods(state.chart.periods);
  renderChart(state.chart);
  renderDiscipline(position || state.selected);
  $('#recheckButton').disabled = !position;
}

async function refreshRealtimeOverview(){
  if(state.realtimeBusy||document.hidden||!state.dashboard)return;
  state.realtimeBusy=true;
  try{
    if(document.querySelector('#overviewView.active')){const dashboard=normalizeDashboard(await request('/api/dashboard'));state.dashboard=dashboard;renderMarketSyncState(dashboard.meta);const assets=dashboard.analysis_assets.length?dashboard.analysis_assets:[...dashboard.portfolio.positions,...dashboard.markets.filter(m=>!dashboard.portfolio.positions.some(p=>p.symbol===m.symbol))];state.overviewAssets=Object.fromEntries(assets.map(item=>[item.symbol,{symbol:item.symbol,name:item.name}]));$('#assetSuggestions').innerHTML=assets.map(item=>`<option value="${safe(item.name)} ${safe(item.symbol)}"></option>`).join('');if(state.selected?.symbol){const response=normalizeChartPayload(await request(`/api/assets/${encodeURIComponent(state.selected.symbol)}/chart?period=${encodeURIComponent(state.period)}`),state.period),wasAtEnd=!state.chartWindow||state.chartWindow.end>=(state.chart?.series?.length||0);state.chart=response;if(wasAtEnd&&state.chartWindow){const span=state.chartWindow.end-state.chartWindow.start;state.chartWindow.end=response.series.length;state.chartWindow.start=Math.max(0,response.series.length-span);}renderChart(response);const livePrice=response.quote?.price??response.series.at(-1)?.close,change=response.quote?.change_pct??response.series.at(-1)?.change_pct;$('#assetPrice').textContent=Number(livePrice)>0?`¥ ${money(livePrice,3)}`:'待行情';$('#assetPnl').textContent=change==null?'—':pct(change);$('#assetPnl').style.color=chartTrendColor(change||0);$('#asOf').textContent=response.quote?.observed_at||response.series.at(-1)?.time||'—';}}
  }catch(error){console.warn('realtime overview refresh failed',error);}finally{state.realtimeBusy=false;}
}

function renderMarketSyncState(meta){
  if(!meta)return;
  const sync=meta.last_sync_at?` · 最近同步 ${localTimestamp(meta.last_sync_at)}`:'';
  $('#dataMode').textContent=meta.live_refresh_enabled?(meta.market_session_open?`● 盘中每分钟同步${sync}`:`● 闭市每日同步${sync}`):`○ 自动同步未启用${sync}`;
}

function renderDiscipline(position) {
  const card = $('#disciplineCard');
  const discipline=position?.discipline;
  card.className = `discipline-card ${discipline?.severity||''}`;
  card.innerHTML = discipline?`<div class="status">${safe(actionLabel(discipline.action))}</div><p>（模型输出）${safe(discipline.reason)}。执行任何操作前仍需确认行情时效、滑点、基本面与证据覆盖。</p>`:'<div class="status">等待可靠行情</div><p>真实持仓已经保存，但当前没有可核验的最新价格，因此不计算风险线。</p>';
  const lines = state.chart.lines;
  if (!lines) {
    if(!state.dashboard?.portfolio?.positions?.some(item=>item.symbol===position?.symbol))card.innerHTML = `<div class="status">行情观察</div><p>该标的不在当前持仓中，因此不生成成本、止盈或止损纪律线。</p>`;
    $('#lineList').innerHTML = '';
    return;
  }
  const rows = [
    ['#ff5e6c','止损红线',lines.stop_loss], ['#68a9ff','持仓成本线',lines.cost],
    ['#31d6a0','绿线研究价',lines.buy_watch], ['#f5bc60','止盈目标线',lines.take_profit]
  ];
  $('#lineList').innerHTML = rows.map(row => `<div class="line-row"><i style="background:${row[0]}"></i><span>${row[1]}</span><strong>¥ ${money(row[2], 3)}</strong></div>`).join('');
}

function renderPeriods(periods) {
  $('#periodSelector').innerHTML = periods.map(p => `<button class="period-button ${p.key === state.period ? 'active' : ''}" data-period="${safe(p.key)}">${safe(p.label)}</button>`).join('');
  document.querySelectorAll('.period-button').forEach(button => button.onclick = async () => {
    state.period = button.dataset.period;
    state.chartWindow = null;
    await selectAsset(state.selected.symbol);
  });
}

const compactNumber = value => { const n=Number(value||0),a=Math.abs(n); if(a>=1e8)return `${(n/1e8).toFixed(2)}亿`;if(a>=1e4)return `${(n/1e4).toFixed(2)}万`;return money(n,0); };
const signed = value => value==null?'--':`${Number(value)>=0?'+':''}${Number(value).toFixed(2)}%`;

function buildVolumeProfile(series, min, max, bins=120) {
  const step=(max-min)/bins || 1, profile=Array.from({length:bins},(_,i)=>({price:min+(i+.5)*step,volume:0}));
  series.forEach(point=>{const low=Math.max(0,Math.min(bins-1,Math.floor((Number(point.low)-min)/step))),high=Math.max(low,Math.min(bins-1,Math.floor((Number(point.high)-min)/step))),share=Number(point.volume||0)/(high-low+1||1);for(let i=low;i<=high;i++)profile[i].volume+=share;});
  const total=profile.reduce((sum,item)=>sum+item.volume,0)||1;
  const quantile=q=>{let sum=0;for(const item of profile){sum+=item.volume;if(sum/total>=q)return item.price;}return profile[profile.length-1].price;};
  const ranges={p70:[quantile(.15),quantile(.85)],p90:[quantile(.05),quantile(.95)]};
  const concentration=range=>(range[1]-range[0])/Math.max((range[1]+range[0])/2,.000001)*100;
  return {profile,ranges,c70:concentration(ranges.p70),c90:concentration(ranges.p90),maxVolume:Math.max(...profile.map(item=>item.volume),1)};
}

function hoverStats(point, absoluteIndex, payload) {
  const full=payload.series,turnover=point.turnover_rate ?? (absoluteIndex===full.length-1?payload.quote?.turnover_rate:null);
  const rangeStart=state.chartWindow?.start||0,baseClose=Number(full[rangeStart]?.close);
  const cumulative=baseClose?(Number(point.close)/baseClose-1)*100:null;
  const amountPrefix=point.amount_estimated?'≈':'';
  $('#chartHoverStats').innerHTML=`<strong>${safe(String(point.time).replace('T',' ').slice(0,19))}</strong><span>涨跌幅 <b style="color:${chartTrendColor(point.change_pct)}">${signed(point.change_pct)}</b></span><span>开 <b>${money(point.open,3)}</b></span><span>高 <b>${money(point.high,3)}</b></span><span>低 <b>${money(point.low,3)}</b></span><span>换 <b>${turnover==null?'--':Number(turnover).toFixed(2)+'%'}</b></span><span>量 <b>${compactNumber(point.volume)}</b></span><span>额 <b>${amountPrefix}${point.amount?compactNumber(point.amount):'--'}</b></span><span>至今涨幅 <b style="color:${chartTrendColor(cumulative)}">${signed(cumulative)}</b></span>`;
}

function indicatorSvg(series, scale, kind, upColor, downColor) {
  const {width,height,left,right,x}=scale,plotRight=width-right,plotWidth=plotRight-left,innerTop=18,innerBottom=8,innerHeight=height-innerTop-innerBottom;
  let content='',labels='';
  if(kind==='amount'){
    const values=series.map(p=>Number(p.amount||0)),max=Math.max(...values,1),bar=Math.max(1,Math.min(8,plotWidth/series.length*.72));
    content=series.map((p,i)=>{const h=values[i]/max*innerHeight,color=Number(p.close)>=Number(p.open)?upColor:downColor;return `<rect x="${x(i)-bar/2}" y="${height-innerBottom-h}" width="${bar}" height="${h}" fill="${color}" opacity=".72"/>`;}).join('');
    labels=`<text x="${left}" y="11">成交额　峰值 ${compactNumber(max)}</text>`;
  }else if(kind==='kdj'){
    const values=series.flatMap(p=>[Number(p.kdj_k??50),Number(p.kdj_d??50),Number(p.kdj_j??50)]),min=Math.min(-20,...values),max=Math.max(120,...values),yy=v=>innerTop+(max-v)*innerHeight/(max-min||1),line=(key,color)=>`<polyline fill="none" stroke="${color}" stroke-width="1.2" points="${series.map((p,i)=>`${x(i)},${yy(Number(p[key]??50))}`).join(' ')}"/>`;
    content=`<line class="indicator-grid" x1="${left}" y1="${yy(80)}" x2="${plotRight}" y2="${yy(80)}"/><line class="indicator-grid" x1="${left}" y1="${yy(20)}" x2="${plotRight}" y2="${yy(20)}"/>${line('kdj_k','#f5bc60')}${line('kdj_d','#68a9ff')}${line('kdj_j','#d889ff')}`;
    labels=`<text x="${left}" y="11">KDJ　<tspan fill="#f5bc60">K</tspan> <tspan fill="#68a9ff">D</tspan> <tspan fill="#d889ff">J</tspan></text>`;
  }else{
    const values=series.flatMap(p=>[Number(p.macd_dif||0),Number(p.macd_dea||0),Number(p.macd_hist||0)]),min=Math.min(0,...values),max=Math.max(0,...values),yy=v=>innerTop+(max-v)*innerHeight/(max-min||1),zero=yy(0),bar=Math.max(1,Math.min(7,plotWidth/series.length*.62)),line=(key,color)=>`<polyline fill="none" stroke="${color}" stroke-width="1.1" points="${series.map((p,i)=>`${x(i)},${yy(Number(p[key]||0))}`).join(' ')}"/>`;
    content=`<line class="indicator-grid" x1="${left}" y1="${zero}" x2="${plotRight}" y2="${zero}"/>${series.map((p,i)=>{const value=Number(p.macd_hist||0),yv=yy(value);return `<rect x="${x(i)-bar/2}" y="${Math.min(zero,yv)}" width="${bar}" height="${Math.max(1,Math.abs(zero-yv))}" fill="${value>=0?upColor:downColor}" opacity=".7"/>`;}).join('')}${line('macd_dif','#f5bc60')}${line('macd_dea','#68a9ff')}`;
    labels=`<text x="${left}" y="11">MACD(12,26,9)　<tspan fill="#f5bc60">DIF</tspan> <tspan fill="#68a9ff">DEA</tspan></text>`;
  }
  return `<div class="indicator-panel"><svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">${labels}${content}<line class="linked-crosshair crosshair" y1="0" y2="${height}" visibility="hidden"/></svg></div>`;
}

function renderChart(payload) {
  const full=payload.series||[];
  if(!full.length){$('#chart').innerHTML='<div class="empty-state">该周期暂无通达信行情，请刷新该标的。</div>';$('#chartFrequency').textContent=`${payload.period_label} · 0 条 · 暂无本地数据`;$('#chartFrequency').className='coverage-gap';$('#volatility').textContent='年化波动 —';$('#drawdown').textContent='最大回撤 —';$('#momentum').textContent='20日动量 —';return;}
  const defaultVisible=payload.chart_type==='line'?Math.min(full.length,state.period==='5d'?600:242):Math.min(full.length,90);
  if(!state.chartWindow||state.chartWindow.key!==`${payload.asset.symbol}:${payload.period}`)state.chartWindow={key:`${payload.asset.symbol}:${payload.period}`,start:Math.max(0,full.length-defaultVisible),end:full.length};
  const windowState=state.chartWindow;windowState.end=Math.min(full.length,windowState.end);windowState.start=Math.max(0,Math.min(windowState.start,windowState.end-2));
  const finitePrice=value=>value!==null&&value!==''&&Number.isFinite(Number(value));
  const series=full.slice(windowState.start,windowState.end).filter(point=>['open','high','low','close'].every(key=>finitePrice(point[key]))),width=Math.max(760,Math.round($('#chart').clientWidth||1000)),sideWidth=225,columnGap=8,mainWidth=width-sideWidth-columnGap,height=340,left=58,right=8,top=8,bottom=28,plotRight=mainWidth-right;
  if(!series.length){$('#chart').innerHTML='<div class="empty-state">该周期价格数据无效，请重新刷新。</div>';$('#chartFrequency').textContent=`${payload.period_label} · 0 条 · 数据校验失败`;$('#chartFrequency').className='coverage-gap';return;}
  const levels=(payload.lines?[payload.lines.cost,payload.lines.stop_loss,payload.lines.take_profit]:[]).filter(finitePrice).map(Number),lows=series.map(p=>Number(p.low)),highs=series.map(p=>Number(p.high));let min=Math.min(...lows,...levels),max=Math.max(...highs,...levels);const padding=Math.max((max-min)*.05,Math.abs(max)*.003,.001);min-=padding;max+=padding;
  const plotWidth=plotRight-left,plotHeight=height-top-bottom,x=i=>left+(i+.5)*plotWidth/series.length,y=v=>top+(max-v)*plotHeight/(max-min||1),redUp=state.colorConvention==='redUp',upColor=redUp?'#ff5e6c':'#31d6a0',downColor=redUp?'#31d6a0':'#ff5e6c';
  let grid='';for(let i=0;i<8;i++){const gy=top+i*plotHeight/7,gv=max-i*(max-min)/7;grid+=`<line class="grid" x1="${left}" y1="${gy}" x2="${plotRight}" y2="${gy}"/><text class="y-axis-label" x="4" y="${gy+3}">${money(gv,max<10?3:2)}</text>`;}
  let plot='';if(payload.chart_type==='line'){const points=series.map((p,i)=>`${x(i)},${y(Number(p.close))}`).join(' ');plot=`<polygon class="area" points="${left},${height-bottom} ${points} ${plotRight},${height-bottom}"/><polyline class="price-line" points="${points}"/>`;}else{const candleWidth=Math.max(1,Math.min(9,plotWidth/series.length*.68));plot=series.map((p,i)=>{const open=Number(p.open),close=Number(p.close),color=close>=open?upColor:downColor,topY=Math.min(y(open),y(close)),body=Math.max(1,Math.abs(y(open)-y(close)));return `<line class="wick" stroke="${color}" x1="${x(i)}" y1="${y(Number(p.high))}" x2="${x(i)}" y2="${y(Number(p.low))}"/><rect class="candle" x="${x(i)-candleWidth/2}" y="${topY}" width="${candleWidth}" height="${body}" fill="${color}"/>`;}).join('');const ma=series.map((p,i)=>p.ma5==null?null:`${x(i)},${y(Number(p.ma5))}`).filter(Boolean).join(' ');if(ma)plot+=`<polyline class="ma-line" points="${ma}"/>`;}
  const levelMeta=(payload.lines?[['成本',payload.lines.cost,'#68a9ff'],['止损',payload.lines.stop_loss,'#ff5e6c'],['止盈',payload.lines.take_profit,'#f5bc60']]:[]).filter(([,value])=>finitePrice(value)),levelSvg=levelMeta.map(([name,value,color])=>`<line class="level" stroke="${color}" x1="${left}" y1="${y(Number(value))}" x2="${plotRight}" y2="${y(Number(value))}"/><text class="tag" fill="${color}" x="${left+5}" y="${y(Number(value))-4}">${name} ${money(value,3)}</text>`).join('');
  const chips=buildVolumeProfile(series,min,max),leftStackHeight=599,chipFooterHeight=72,chipPlotHeight=leftStackHeight-chipFooterHeight,chipX=8,chipAxisWidth=46,chipAxisX=sideWidth-chipAxisWidth,chipMax=chipAxisX-chipX-4,chipY=price=>8+(max-price)*(chipPlotHeight-16)/(max-min||1),chipHeight=Math.max(.5,(chipPlotHeight-16)/chips.profile.length*.86),chipBars=chips.profile.map(item=>{const w=item.volume/chips.maxVolume*chipMax,color=item.price<=Number(series[series.length-1].close)?upColor:downColor;return `<rect class="chip-bar" x="${chipX}" y="${chipY(item.price)-chipHeight/2}" width="${w}" height="${chipHeight}" fill="${color}"/>`;}).join(''),chipAxisTicks=Array.from({length:8},(_,i)=>{const price=max-i*(max-min)/7,ty=chipY(price);return `<line class="chip-axis-grid" x1="${chipX}" y1="${ty}" x2="${chipAxisX}" y2="${ty}"/><line class="chip-axis-tick" x1="${chipAxisX-4}" y1="${ty}" x2="${chipAxisX+2}" y2="${ty}"/><text class="chip-axis-label" x="${chipAxisX+5}" y="${ty+3}">${money(price,max<10?3:2)}</text>`;}).join(''),chipHtml=`<aside class="chip-side-panel"><svg viewBox="0 0 ${sideWidth} ${chipPlotHeight}" preserveAspectRatio="none">${chipAxisTicks}${chipBars}<line class="chip-axis-line" x1="${chipAxisX}" y1="8" x2="${chipAxisX}" y2="${chipPlotHeight-8}"/></svg><div class="chip-footer"><strong>筹码分布（量价估算）</strong><span>70% ${money(chips.ranges.p70[0],3)}–${money(chips.ranges.p70[1],3)}　集中 ${chips.c70.toFixed(1)}%</span><span>90% ${money(chips.ranges.p90[0],3)}–${money(chips.ranges.p90[1],3)}　集中 ${chips.c90.toFixed(1)}%</span></div></aside>`;
  const labelAt=i=>{const t=String(series[i].time);return t.includes('T')?(state.period==='5d'?t.slice(5,10)+' '+t.slice(11,16):t.slice(11,16)):t.slice(2,10);},indices=[...new Set([0,Math.floor((series.length-1)/2),series.length-1])],labels=indices.map(i=>`<text x="${Math.max(left,x(i)-22)}" y="${height-8}">${safe(labelAt(i))}</text>`).join('');
  const indicatorScale={width:mainWidth,height:82,left,right,x};
  $('#chart').innerHTML=`<div id="chartHoverStats" class="chart-hover-stats"></div><div class="chart-body-grid"><div class="chart-left-stack"><div id="priceMainSurface" class="price-main-surface"><svg viewBox="0 0 ${mainWidth} ${height}" preserveAspectRatio="none"><defs><linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#67d9dc" stop-opacity=".22"/><stop offset="1" stop-color="#67d9dc" stop-opacity="0"/></linearGradient></defs>${grid}${plot}${levelSvg}${labels}<line id="crosshairX" class="crosshair" y1="${top}" y2="${height-bottom}" visibility="hidden"/><line id="crosshairY" class="crosshair" x1="${left}" x2="${plotRight}" visibility="hidden"/></svg></div><div class="indicator-stack">${indicatorSvg(series,indicatorScale,'amount',upColor,downColor)}${indicatorSvg(series,indicatorScale,'kdj',upColor,downColor)}${indicatorSvg(series,indicatorScale,'macd',upColor,downColor)}</div></div>${chipHtml}</div>`;
  hoverStats(series[series.length-1],windowState.end-1,payload);bindChartInteraction(payload,series,{width:mainWidth,height,left,right,top,bottom,plotRight,x,y});
  $('#volatility').textContent=`年化波动 ${(payload.risk.annualized_volatility*100).toFixed(1)}%`;$('#drawdown').textContent=`最大回撤 ${(payload.risk.max_drawdown*100).toFixed(1)}%`;$('#momentum').textContent=`20日动量 ${(payload.risk.momentum_20d*100).toFixed(1)}%`;const coverage=payload.coverage||{};const statusText=coverage.requirement_exempt?' · 固定五日窗口':(coverage.meets_required_history?' · 已达3年':' · 未达3年');const coverageText=coverage.first_bar?`${String(coverage.first_bar).slice(0,10)}—${String(coverage.last_bar).slice(0,10)}${statusText}`:'无本地数据';$('#chartFrequency').textContent=`${payload.period_label} · ${windowState.end-windowState.start}/${full.length} 条 · ${coverageText}`;$('#chartFrequency').className=coverage.requirement_exempt||coverage.meets_required_history?'coverage-ok':'coverage-gap';
}

function bindChartInteraction(payload,visible,scale){
  const element=$('#priceMainSurface'),windowState=state.chartWindow,full=payload.series;
  element.onwheel=event=>{event.preventDefault();const old=windowState.end-windowState.start,next=Math.max(12,Math.min(full.length,Math.round(old*(event.deltaY>0?1.18:.84)))),rect=element.getBoundingClientRect(),ratio=Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width*scale.width/scale.plotRight)),anchor=windowState.start+Math.round(old*ratio);windowState.start=Math.max(0,Math.min(full.length-next,anchor-Math.round(next*ratio)));windowState.end=windowState.start+next;renderChart(payload);};
  element.onpointerdown=event=>{if(event.button!==0)return;event.preventDefault();state.chartDrag={payload,startX:event.clientX,start:windowState.start,end:windowState.end,span:windowState.end-windowState.start,pixels:element.getBoundingClientRect().width,pendingX:event.clientX,frame:null};document.body.classList.add('chart-dragging');element.classList.add('dragging');};
  element.onpointerleave=()=>{if(!state.chartDrag)document.querySelectorAll('.crosshair').forEach(line=>line.setAttribute('visibility','hidden'));};
  element.onpointermove=event=>{if(state.chartDrag)return;const rect=element.getBoundingClientRect(),svgX=(event.clientX-rect.left)/rect.width*scale.width;if(svgX<scale.left||svgX>scale.plotRight)return;const index=Math.max(0,Math.min(visible.length-1,Math.floor((svgX-scale.left)/(scale.plotRight-scale.left)*visible.length))),point=visible[index],px=scale.x(index),py=scale.y(Number(point.close));hoverStats(point,windowState.start+index,payload);const crossX=$('#crosshairX'),crossY=$('#crosshairY');crossX.setAttribute('x1',px);crossX.setAttribute('x2',px);crossX.setAttribute('visibility','visible');crossY.setAttribute('y1',py);crossY.setAttribute('y2',py);crossY.setAttribute('visibility','visible');document.querySelectorAll('.linked-crosshair').forEach(line=>{line.setAttribute('x1',px);line.setAttribute('x2',px);line.setAttribute('visibility','visible');});};
}

document.addEventListener('pointermove',event=>{const drag=state.chartDrag;if(!drag)return;drag.pendingX=event.clientX;if(drag.frame)return;drag.frame=requestAnimationFrame(()=>{drag.frame=null;const delta=Math.round((drag.startX-drag.pendingX)/Math.max(drag.pixels,1)*drag.span),start=Math.max(0,Math.min(drag.payload.series.length-drag.span,drag.start+delta));if(start!==state.chartWindow.start){state.chartWindow.start=start;state.chartWindow.end=start+drag.span;renderChart(drag.payload);}});});
document.addEventListener('pointerup',()=>{if(!state.chartDrag)return;if(state.chartDrag.frame)cancelAnimationFrame(state.chartDrag.frame);state.chartDrag=null;document.body.classList.remove('chart-dragging');document.querySelectorAll('.price-main-surface').forEach(element=>element.classList.remove('dragging'));});
window.addEventListener('blur',()=>{state.chartDrag=null;document.body.classList.remove('chart-dragging');});

function renderLibraryView(data) {
  const summary=data.source_summary||{},primary=summary.primary||null,backup=summary.backup||null,alternatives=summary.alternatives||[];
  const sourceStatus=(item,fallback='尚未验证')=>item?.status_label||fallback;
  const sourceTone=item=>['good','warn','bad','muted'].includes(item?.status_tone)?item.status_tone:'muted';
  const primaryHtml=primary?`<article class="source-overview-card primary-source"><div class="source-overview-head"><span>当前在线行情</span><b class="source-state ${sourceTone(primary)}"><i></i>${safe(sourceStatus(primary))}</b></div><strong>${safe(primary.name)}</strong><p>${safe(primary.license_note)}</p><dl><div><dt>最近成功</dt><dd>${safe(beijingTimestamp(primary.last_success_at))}</dd></div><div><dt>当前用途</dt><dd>股票价格、K 线与历史行情</dd></div></dl></article>`:`<article class="source-overview-card primary-source"><div class="source-overview-head"><span>当前在线行情</span><b class="source-state bad"><i></i>不可用</b></div><strong>未找到在线行情源</strong><p>系统当前没有可识别的在线行情配置。</p></article>`;
  const backupHtml=backup?`<article class="source-overview-card backup-source"><div class="source-overview-head"><span>离线兜底</span><b class="source-state ${sourceTone(backup)}"><i></i>${safe(sourceStatus(backup))}</b></div><strong>${safe(backup.name)}</strong><p>在线刷新失败时保留最后一次可信数据；页面会明确提示数据时间，不会冒充实时行情。</p><dl><div><dt>保存位置</dt><dd>仅在本机</dd></div><div><dt>使用方式</dt><dd>自动读取，无需配置</dd></div></dl></article>`:'';
  const advancedCards=alternatives.map(item=>`<div class="source-card"><span class="source-state ${sourceTone(item)}"><i></i>${safe(sourceStatus(item))}</span><div><strong>${safe(item.name)}</strong><p>${safe(item.license_note)}</p><small>${safe(item.code)} · ${safe(item.access_mode)}${item.last_success_at?` · 最近成功 ${safe(beijingTimestamp(item.last_success_at))}`:''}</small></div><span class="source-connection">${safe(item.connection_label||'未接入')}</span></div>`).join('');
  const evidenceCards=data.evidence.map(e=>`<div class="source-card"><span class="label">${safe(e.label)}</span><div><strong>${safe(e.claim)}</strong><p>${safe(e.independent_check)}</p></div><span class="source-connection">${safe(e.status)}</span></div>`).join('');
  $('#libraryView').innerHTML = `${searchBar()}<div id="librarySearchResults"></div><div class="library-tabs" role="tablist" aria-label="资料分类"><button class="library-tab active" data-library-tab="sources">数据来源</button><button class="library-tab" data-library-tab="strategies">方案与风险</button><button class="library-tab" data-library-tab="reports">研究报告</button></div><section id="librarySourcesPane" class="library-pane active"><div class="panel-head library-section-head"><div><p class="eyebrow">行情连接</p><h2>当前数据状态</h2></div><span class="count">只读 · 本机保存</span></div><div class="source-overview-grid">${primaryHtml}${backupHtml}</div><p class="source-plain-note beginner-only">当前只需要关注上面两项：在线行情负责更新，离线快照负责在网络异常时保留最后一次可信数据。</p><details class="source-advanced professional-disclosure"><summary>专业数据源与原始状态 <span>${alternatives.length} 项</span></summary><div class="source-advanced-note">这里显示连接方式、内部代码、健康状态和权限要求。券商客户端、登录、行情权限或 API 密钥必须由你主动配置，系统不会代为开通。</div>${advancedCards||'<div class="empty-state">没有其他可选数据源</div>'}</details>${evidenceCards?`<section class="source-evidence"><div class="panel-head library-section-head"><div><p class="eyebrow">证据记录</p><h2>已保存的核验材料</h2></div></div>${evidenceCards}</section>`:''}</section><section id="libraryStrategiesPane" class="library-pane"></section><section id="libraryReportsPane" class="library-pane"></section>`;
  renderStrategyLab(data.strategy_lab);
  renderReportLibraryShell();
  bindLibrarySearch();
  bindLibraryTabs();
}

function renderReportLibraryShell(){
  $('#libraryReportsPane').innerHTML=`<section class="report-library"><div class="panel-head library-section-head"><div><p class="eyebrow">研报自动收集</p><h2>个股、公告和市场研究报告</h2></div><div class="report-actions"><span id="reportWatcherState" class="count">每日自动同步</span><button id="refreshReportsButton" class="secondary-button">立即检查</button></div></div><div class="report-stock-loader"><input id="reportStockInput" aria-label="股票名称或代码" placeholder="股票名称或代码"><button id="loadStockReportsButton" class="primary-button">获取个股研报</button></div><div class="report-bulk-sync"><div class="report-bulk-head"><div><strong>全市场历史研报</strong><span id="bulkReportState">等待启动</span></div><div class="report-actions"><button id="startBulkReportsButton" class="secondary-button">全量入库</button><button id="pauseBulkReportsButton" class="secondary-button" disabled>暂停</button></div></div><div class="report-progress"><i id="bulkReportProgress"></i></div><small id="bulkReportDetail">0 条研报</small></div><p class="lab-warning">系统只归档公开资料并保留原文链接；券商评级是外部观点，不代表一定会涨。</p><div id="reportLibraryList" class="report-list"><div class="search-loading">正在读取本地研报库…</div></div></section>`;
  $('#refreshReportsButton').onclick=async()=>{const button=$('#refreshReportsButton');button.disabled=true;button.textContent='后台检查中…';try{const result=await request('/api/reports/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});showToast(result.started?'已启动后台检查，页面会自动更新':'检查已在运行');window.setTimeout(loadReportLibrary,500);}catch(error){showToast(`检查失败：${error.message}`);}finally{button.disabled=false;button.textContent='立即检查';}};
  const loadStockReports=async()=>{const input=$('#reportStockInput'),button=$('#loadStockReportsButton'),value=input.value.trim();if(!value)return;button.disabled=true;button.textContent='正在获取…';try{const data=await request('/api/reports/stock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stock:value})});await loadReportLibrary();$('#librarySearchInput').value=data.stock.name;if(data.result.rows){$('#librarySearchButton').click();showToast(`${data.stock.name}已入库 ${data.result.rows} 条研报`);}else{$('#librarySearchResults').innerHTML=`<div class="empty-state">${safe(data.stock.name)}当前暂无公开券商研报</div>`;showToast(`${data.stock.name}当前暂无公开券商研报`);}}catch(error){showToast(`获取失败：${error.message}`);}finally{button.disabled=false;button.textContent='获取个股研报';}};
  $('#loadStockReportsButton').onclick=loadStockReports;$('#reportStockInput').onkeydown=event=>{if(event.key==='Enter')loadStockReports();};
  $('#startBulkReportsButton').onclick=async()=>{const button=$('#startBulkReportsButton');button.disabled=true;try{const data=await request('/api/reports/sync/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'full'})});showToast(data.started?'全量研报入库已启动':data.message);await loadBulkReportStatus();}catch(error){showToast(`启动失败：${error.message}`);}finally{button.disabled=false;}};
  $('#pauseBulkReportsButton').onclick=async()=>{const button=$('#pauseBulkReportsButton');button.disabled=true;try{await request('/api/reports/sync/pause',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});showToast('正在安全暂停，当前页会先完成入库');await loadBulkReportStatus();}catch(error){showToast(`暂停失败：${error.message}`);}};
  loadReportLibrary();
  loadBulkReportStatus();
}

async function loadReportLibrary(){try{const data=await request('/api/reports/library'),items=data.documents||[],refresh=data.refresh||{},sync=data.daily_sync||{};updateLibrarySyncState(sync);$('#reportWatcherState').textContent=sync.status==='RUNNING'?'正在同步全部资料…':`${sync.status_label||'每日自动同步'}${sync.completed_at?` · ${localTimestamp(sync.completed_at)}`:''}`;$('#reportLibraryList').innerHTML=items.length?items.map(item=>`<article class="report-item"><div><span class="badge">${safe(item.document_type)}</span><strong>${safe(item.title)}</strong><p>${safe(item.body)}</p><small>${safe(item.source_name)} · ${safe(item.published_at||item.captured_at||'时间未知')}</small></div>${item.source_url?`<a href="${safe(item.source_url)}" target="_blank" rel="noreferrer">查看原文 ↗</a>`:'<span class="count">索引记录</span>'}</article>`).join(''):'<div class="empty-state">尚未发现新的公开研报或公告；系统会在每日资料同步中继续检查。</div>';if(state.reportPollTimer)window.clearTimeout(state.reportPollTimer);state.reportPollTimer=refresh.status==='RUNNING'||sync.status==='RUNNING'?window.setTimeout(loadReportLibrary,2000):null;}catch(error){$('#reportLibraryList').innerHTML=`<div class="empty-state">${safe(error.message)}</div>`;}}

async function loadBulkReportStatus(){if(!$('#bulkReportState'))return;try{const data=await request('/api/reports/sync/status'),job=data.jobs.full||{},incremental=data.jobs.incremental||{},running=['RUNNING','PAUSING'].includes(job.status),labels={IDLE:'等待启动',RUNNING:'正在全量入库',PAUSING:'正在暂停',PAUSED:'已暂停',FAILED:'同步失败',INTERRUPTED:'等待续跑',SUCCESS:'全量入库完成'};$('#bulkReportState').textContent=labels[job.status]||job.status;$('#bulkReportProgress').style.width=`${Math.max(0,Math.min(100,Number(job.progress_pct||0)))}%`;const pages=job.total_pages?`${job.processed_pages}/${job.total_pages} 页`:`${job.processed_pages||0} 页`;const daily=incremental.completed_at?` · 最近增量 ${localTimestamp(incremental.completed_at)}`:' · 每日增量待命';$('#bulkReportDetail').textContent=`${pages} · 已处理 ${Number(job.fetched_rows||0).toLocaleString('zh-CN')} 条 · 本地共 ${Number(data.documents||0).toLocaleString('zh-CN')} 条${daily}${job.last_error?` · ${job.last_error}`:''}`;const start=$('#startBulkReportsButton'),pause=$('#pauseBulkReportsButton');start.disabled=running||job.status==='SUCCESS';start.textContent=['PAUSED','FAILED','INTERRUPTED'].includes(job.status)?'继续入库':job.status==='SUCCESS'?'已完成':'全量入库';pause.disabled=!running;if(state.bulkReportPollTimer)window.clearTimeout(state.bulkReportPollTimer);state.bulkReportPollTimer=window.setTimeout(loadBulkReportStatus,running?2000:60000);if(job.status==='SUCCESS')loadReportLibrary();}catch(error){$('#bulkReportState').textContent=`状态读取失败：${safe(error.message)}`;}}

function researchMetric(value,digits=2){return value===null||value===undefined||Number.isNaN(Number(value))?'—':Number(value).toFixed(digits);}

function miniCurveSvg(series,benchmark=[],width=420,height=132){
  if(!Array.isArray(series)||series.length<2)return '<div class="mini-chart-empty">运行后显示模拟净值曲线</div>';
  const all=[...series,...(Array.isArray(benchmark)?benchmark:[])].map(p=>Number(p.value)).filter(Number.isFinite),min=Math.min(...all),max=Math.max(...all),span=max-min||1,pad=9;
  const path=points=>points.map((point,index)=>`${index?'L':'M'} ${(pad+index/(points.length-1)*(width-pad*2)).toFixed(1)} ${(height-pad-(Number(point.value)-min)/span*(height-pad*2)).toFixed(1)}`).join(' ');
  const main=path(series),base=Array.isArray(benchmark)&&benchmark.length>1?`<path class="mini-benchmark" d="${path(benchmark)}"/>`:'';
  return `<svg class="mini-curve" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="模拟收益曲线"><defs><linearGradient id="miniFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#31d6a0" stop-opacity=".28"/><stop offset="1" stop-color="#31d6a0" stop-opacity="0"/></linearGradient></defs><path class="mini-area" d="${main} L ${width-pad} ${height-pad} L ${pad} ${height-pad} Z"/><path class="mini-main" d="${main}"/>${base}<text x="10" y="16">${safe(series[0].time)} · ${researchMetric(series[0].value)}</text><text x="${width-10}" y="16" text-anchor="end">${safe(series[series.length-1].time)} · ${researchMetric(series[series.length-1].value)}</text></svg>`;
}

function factorJudgement(item){const m=item.latest_metrics||{};if(m.ic===null||m.ic===undefined)return '尚未评估';const strength=Math.abs(Number(m.rank_ic||0));return strength>=.05?'在当前小样本中有一定截面区分度':strength>=.02?'区分度偏弱，需要扩大样本验证':'当前样本中几乎没有稳定区分度';}

function normalizedFactorScore(value){return value===null||value===undefined?'—':(50+Number(value)*50).toFixed(1);}

function renderCompositeWorkbench(lab){
  const models=lab.composite_models||{},profiles=['aggressive','balanced','conservative'];
  if(!models[state.factorProfile])state.factorProfile=models.balanced?'balanced':Object.keys(models)[0];
  const model=models[state.factorProfile];
  if(!model)return '<section class="panel composite-panel"><div class="empty-state">当前没有可计算的综合因子结果</div></section>';
  const cards=model.scorecards||[],regime=model.regime||{},weights=model.weights||{};
  const profileButtons=profiles.filter(key=>models[key]).map(key=>`<button class="factor-profile-button ${key===state.factorProfile?'active':''}" data-factor-profile="${key}">${safe(models[key].profile_label)}</button>`).join('');
  const weightRows=['technical','fundamental','alternative','risk'].map(key=>`<span><small>${safe({technical:'技术',fundamental:'基本面',alternative:'情绪',risk:'风险'}[key])}</small><strong>${Math.round(Number(weights[key]||0)*100)}%</strong></span>`).join('');
  const tableRows=cards.map(card=>{const formal=card.score===null||card.score===undefined?'—':Number(card.score).toFixed(1),partial=card.provisional_score===null||card.provisional_score===undefined?'—':Number(card.provisional_score).toFixed(1),groups=Object.fromEntries((card.groups||[]).map(group=>[group.key,group]));return `<tr class="${card.eligible?'composite-eligible':'composite-incomplete'}"><td><strong>${safe(card.name)}</strong><small>${safe(card.symbol)} · ${safe(card.data_asof||'时间未知')}</small></td><td><strong class="composite-score">${formal}</strong><small>${card.eligible?'正式研究分':'正式分暂缺'}</small></td><td><strong>${partial}</strong><small>已覆盖部分</small></td><td><div class="coverage-meter"><i style="width:${Math.round(Number(card.coverage||0)*100)}%"></i></div><small>${Math.round(Number(card.coverage||0)*100)}% / 最低 ${Math.round(Number(model.coverage_minimum||0)*100)}%</small></td><td>${['technical','fundamental','alternative','risk'].map(key=>`<span class="group-score ${groups[key]?.available?'':'missing'}">${safe({technical:'技',fundamental:'基',alternative:'情',risk:'险'}[key])} ${normalizedFactorScore(groups[key]?.score)}</span>`).join('')}</td><td><span class="composite-status ${card.eligible?'pass':'hold'}">${safe(card.status_label)}</span><small>${card.missing_groups?.length?`缺少：${safe(card.missing_groups.join('、'))}`:'门槛逐项核验'}</small></td></tr>`;}).join('');
  const mobileRows=cards.map(card=>{const groups=Object.fromEntries((card.groups||[]).map(group=>[group.key,group]));return `<article class="composite-mobile-row"><header><span><strong>${safe(card.name)}</strong><small>${safe(card.symbol)} · ${safe(card.data_asof||'时间未知')}</small></span><b>${card.score===null||card.score===undefined?'正式分 —':`正式分 ${researchMetric(card.score,1)}`}</b></header><div class="composite-mobile-metrics"><span>部分分<strong>${researchMetric(card.provisional_score,1)}</strong></span><span>覆盖率<strong>${Math.round(Number(card.coverage||0)*100)}%</strong></span><span>最低要求<strong>${Math.round(Number(model.coverage_minimum||0)*100)}%</strong></span></div><div class="composite-mobile-groups">${['technical','fundamental','alternative','risk'].map(key=>`<span class="group-score ${groups[key]?.available?'':'missing'}">${safe({technical:'技术',fundamental:'基本面',alternative:'情绪',risk:'风险'}[key])} ${normalizedFactorScore(groups[key]?.score)}</span>`).join('')}</div><footer><span class="composite-status ${card.eligible?'pass':'hold'}">${safe(card.status_label)}</span><small>${card.missing_groups?.length?`缺少：${safe(card.missing_groups.join('、'))}`:'门槛逐项核验'}</small></footer></article>`;}).join('');
  const plainRows=cards.map(card=>{const gates=card.risk_gates||[],passed=gates.filter(gate=>gate.status==='PASS').length,failed=gates.filter(gate=>gate.status==='FAIL').map(gate=>gate.label),missing=card.missing_groups||[],conclusion=card.eligible?'数据完整，可以进入正式比较':missing.length?`资料还不完整，暂不正式排名`:'仍有条件没有通过';return `<article class="composite-plain-card ${card.eligible?'ready':'waiting'}"><header><div><strong>${safe(card.name)}</strong><small>${safe(card.symbol)} · 数据截至 ${safe(card.data_asof||'时间未知')}</small></div><span>${card.eligible?'可正式比较':'继续观察'}</span></header><p>${safe(conclusion)}</p><dl><div><dt>资料完整度</dt><dd>${Math.round(Number(card.coverage||0)*100)}%</dd></div><div><dt>基础检查</dt><dd>${passed}/${gates.length||3} 项通过</dd></div></dl><footer>${missing.length?`还缺：${safe(missing.join('、'))}`:failed.length?`未通过：${safe(failed.join('、'))}`:'关键资料和基础检查均已通过'}</footer></article>`;}).join('');
  const detailRows=cards.map(card=>`<details class="composite-detail"><summary><span><strong>${safe(card.name)}</strong><small>${safe(card.symbol)}</small></span><b>${card.score===null||card.score===undefined?`部分 ${researchMetric(card.provisional_score,1)}`:`正式 ${researchMetric(card.score,1)}`}</b></summary><div class="composite-group-grid">${(card.groups||[]).map(group=>`<section><header><strong>${safe(group.label)} · 权重 ${Math.round(Number(group.weight||0)*100)}%</strong><span>${group.available?`组内 ${normalizedFactorScore(group.score)}`:'暂无数据'}</span></header>${(group.factors||[]).map(factor=>`<div class="factor-contribution ${factor.available?'':'missing'}"><span><strong>${safe(factor.name)}</strong><small>${safe(factor.value_label)} · ${safe(factor.as_of||'时间未知')}</small></span><b>${factor.available?normalizedFactorScore(factor.score):'不计分'}</b></div>`).join('')}</section>`).join('')}</div><div class="risk-gate-grid">${(card.risk_gates||[]).map(gate=>`<span class="gate-${String(gate.status).toLowerCase()}"><b>${gate.status==='PASS'?'通过':gate.status==='FAIL'?'未通过':'待核验'}</b><strong>${safe(gate.label)}</strong><small>${safe(gate.detail)}</small></span>`).join('')}</div></details>`).join('');
  const catalog=lab.factor_catalog||[],catalogGroups=['technical','fundamental','alternative','risk'].map(key=>{const items=catalog.filter(item=>item.group===key);if(!items.length)return '';return `<section><h3>${safe(items[0].group_label)}</h3>${items.map(item=>`<div class="catalog-factor"><span><strong>${safe(item.name)}</strong><small>${safe(item.formula)} · ${safe(item.data)}</small></span><b class="catalog-${String(item.status).toLowerCase()}">${safe(item.status_label)}</b></div>`).join('')}</section>`;}).join('');
  return `<section class="panel composite-panel"><div class="composite-head"><div><p class="eyebrow professional-only">MULTI-FACTOR SCORE</p><p class="eyebrow beginner-only">股票资料检查</p><h2><span class="beginner-only">哪些股票目前更值得继续看</span><span class="professional-only">多因子综合评分</span></h2></div><div class="factor-profile-switch" role="group" aria-label="综合评分风险档位">${profileButtons}</div></div><div class="regime-strip"><span>当前市场环境<strong>${safe(regime.label||'待判断')}</strong></span><p>${safe(regime.note||'')}</p></div><div class="composite-weight-grid professional-only">${weightRows}</div><p class="lab-warning beginner-only">资料不完整时不会硬给股票打正式分，也不会把缺失数据当成 0 分。</p><p class="lab-warning professional-only">${safe(model.note)} 缺失项不按 0 分处理，也不会按剩余因子伪造正式分数。</p><div class="composite-plain-list beginner-only">${plainRows||'<div class="empty-state">当前候选池没有足够的本地日线</div>'}</div><div class="table-wrap composite-table-wrap professional-only"><table class="composite-table"><thead><tr><th>股票</th><th>正式分</th><th>部分分</th><th>覆盖率</th><th>分组得分</th><th>结论</th></tr></thead><tbody>${tableRows||'<tr><td colspan="6">当前候选池没有足够的本地日线</td></tr>'}</tbody></table></div><div class="composite-mobile-list professional-only">${mobileRows||'<div class="empty-state">当前候选池没有足够的本地日线</div>'}</div><div class="composite-details professional-only">${detailRows}</div><details class="factor-catalog professional-only" open><summary>因子库 <span>${catalog.length} 项 · 可计算、条件计算与研究规划分开标记</span></summary><div class="factor-catalog-grid">${catalogGroups}</div></details></section>`;
}

function renderStrategyLab(lab) {
  const strategies=lab.strategies||[],factors=lab.factors||[],backtests=lab.backtests||[];
  const strategyAssets=state.dashboard?.analysis_assets?.length?state.dashboard.analysis_assets:[
    {symbol:'512400',name:'有色ETF南方'},{symbol:'562500',name:'机器人ETF华夏'},
    {symbol:'600519',name:'贵州茅台'},{symbol:'000001.SH',name:'上证指数'}];
  const strategyOptions=strategyAssets.map(asset=>`<option value="${safe(asset.symbol)}">${safe(asset.name)} · ${safe(asset.symbol)}</option>`).join('');
  const cards=strategies.map(item=>{const m=item.latest_metrics||{},curves=item.latest_curve||{};return `<article id="strategy-${safe(item.strategy_key)}" class="panel strategy-card ${state.strategyFocus===item.strategy_key?'result-focus':''}">
    <div class="panel-head"><div><span class="badge professional-only">${safe(item.category)} · ${safe(item.source_framework)}</span><h3>${safe(item.name)}</h3></div><span class="status-tag">${safe(item.status)}</span></div>
    <p>${safe(item.description)}</p>
    <div class="risk-box"><strong>该策略自己的风控</strong><div class="risk-metrics"><span>止损 <b>${(item.stop_loss_pct*100).toFixed(1)}%</b></span><span>止盈 <b>${(item.take_profit_pct*100).toFixed(1)}%</b></span><span>移动止损 <b>${(item.trailing_stop_pct*100).toFixed(1)}%</b></span><span>仓位上限 <b>${(item.max_position_pct*100).toFixed(0)}%</b></span><span>回撤红线 <b>${(item.max_drawdown_pct*100).toFixed(0)}%</b></span><span>单日损失 <b>${(item.max_daily_loss_pct*100).toFixed(1)}%</b></span></div><small>${safe(item.liquidity_rule)}</small></div>
    <div class="backtest-summary"><span>最近标的 <b>${safe(item.latest_symbol||'未运行')}</b></span><span>总收益 <b>${m.total_return_pct===undefined?'—':researchMetric(m.total_return_pct)+'%'}</b></span><span>最大回撤 <b>${m.max_drawdown_pct===undefined?'—':researchMetric(m.max_drawdown_pct)+'%'}</b></span><span class="professional-only">Sharpe <b>${researchMetric(m.sharpe_ratio)}</b></span></div>
    <div class="strategy-curve"><div class="curve-head"><strong>模拟盘净值</strong><span><i class="legend-main"></i>策略 <i class="legend-base"></i>买入持有</span></div>${miniCurveSvg(curves.strategy,curves.benchmark)}</div>
    <div id="strategy-result-${safe(item.strategy_key)}" class="inline-result">${m.total_return_pct===undefined?'选择标的后运行，结果会留在本卡片内。':`<strong>最近回测已完成</strong><span>${safe(item.latest_symbol)} · 收益 ${researchMetric(m.total_return_pct)}% · 年化 ${researchMetric(Number(m.annualized_return||0)*100)}% · 胜率 ${researchMetric(m.win_rate)}% · 交易 ${researchMetric(m.closed_trade_count,0)} 次</span>`}</div>
    <div class="research-actions"><select data-symbol-for="${safe(item.strategy_key)}">${strategyOptions}</select><button class="primary-button run-backtest" data-strategy="${safe(item.strategy_key)}">运行三年+回测</button></div>
  </article>`;}).join('');
  const factorCards=factors.map(item=>{const m=item.latest_metrics||{};return `<article id="factor-${safe(item.factor_key)}" class="panel factor-card ${state.factorFocus===item.factor_key?'result-focus':''}"><div class="panel-head"><div><span class="badge">${safe(item.family)} · ${item.direction>0?'正向':'反向'}</span><h3>${safe(item.name)}</h3></div><button class="secondary-button run-factor" data-factor="${safe(item.factor_key)}">重新评估</button></div><code>${safe(item.expression)}</code><p>${safe(item.description)}</p><div class="factor-score"><span>IC <b>${researchMetric(item.ic,4)}</b></span><span>Rank IC <b>${researchMetric(item.rank_ic,4)}</b></span><span>模拟收益 <b>${m.simulated_return_pct===undefined?'—':researchMetric(m.simulated_return_pct)+'%'}</b></span><span>选中胜率 <b>${m.selected_win_rate_pct===undefined?'—':researchMetric(m.selected_win_rate_pct)+'%'}</b></span></div><div class="factor-curve"><div class="curve-head"><strong>因子选股模拟净值</strong><span>每日选排名第一，持有至下一交易日</span></div>${miniCurveSvg(item.equity_curve)}</div><div class="factor-pros-cons"><div><strong>优点</strong><ul>${(item.advantages||[]).map(text=>`<li>${safe(text)}</li>`).join('')}</ul></div><div><strong>局限</strong><ul>${(item.limitations||[]).map(text=>`<li>${safe(text)}</li>`).join('')}</ul></div></div><div id="factor-result-${safe(item.factor_key)}" class="inline-result"><strong>${safe(factorJudgement(item))}</strong><span>${safe(m.note||'点击评估后，这里显示结果。')}</span></div></article>`;}).join('');
  const runRows=backtests.map(item=>`<tr><td>#${item.id}</td><td>${safe(item.strategy)}</td><td>${safe(item.asset_symbol)}</td><td>${safe(item.data_start||'—')} → ${safe(item.data_end||'—')}</td><td>${safe(item.status)}</td><td>${item.metrics?researchMetric(item.metrics.total_return_pct)+'%':'—'}</td><td>${item.metrics?researchMetric(item.metrics.max_drawdown_pct)+'%':'—'}</td></tr>`).join('');
  $('#libraryStrategiesPane').innerHTML=`
    ${renderCompositeWorkbench(lab)}
    <section class="strategy-plain-summary beginner-only"><strong>当前有 ${strategies.length} 套研究方案</strong><span>每套方案分别检查止损、止盈、仓位和历史最大跌幅；历史回测只用于研究，不代表未来收益。</span></section>
    <section class="research-summary professional-only"><article class="metric-card"><span>版本化策略</span><strong>${strategies.length}</strong><small>每个策略绑定独立风控</small></article><article class="metric-card"><span>因子表达式</span><strong>${factors.length}</strong><small>AKQuant FactorEngine</small></article><article class="metric-card"><span>已保存回测</span><strong>${backtests.length}</strong><small>分析专用，不连接券商</small></article><article class="metric-card accent"><span>试验标的池</span><strong>${(lab.universe||[]).length}</strong><small>小样本结果不可直接交易</small></article></section>
    <section class="strategy-grid">${cards}</section>
    <section class="factor-lab professional-only"><div class="panel factor-lab-head"><div class="panel-head"><div><p class="eyebrow">FACTOR MINING</p><h2>因子挖掘、优缺点与模拟盘曲线</h2></div><span class="count">IC使用未来5日收益 · 曲线使用下一日收益</span></div><p class="lab-warning">当前只有 4 个标的，IC/Rank IC 与模拟曲线只用于验证管线，不具备统计显著性。模拟收益没有包含完整滑点、冲击成本和涨跌停无法成交约束。</p></div><div class="factor-grid">${factorCards}</div></section>
    <section class="panel research-table professional-only"><div class="panel-head"><div><p class="eyebrow">BACKTEST AUDIT</p><h2>回测审计记录</h2></div><span class="count">${safe((lab.frameworks||[]).join(' · '))}</span></div><div class="table-wrap"><table><thead><tr><th>编号</th><th>策略</th><th>标的</th><th>数据区间</th><th>状态</th><th>总收益</th><th>最大回撤</th></tr></thead><tbody>${runRows||'<tr><td colspan="7">尚未运行</td></tr>'}</tbody></table></div></section>`;
  document.querySelectorAll('.factor-profile-button').forEach(button=>button.onclick=()=>{state.factorProfile=button.dataset.factorProfile;localStorage.setItem('argus-factor-profile',state.factorProfile);renderStrategyLab(lab);});
  bindStrategyLab();
}

function bindStrategyLab(){
  document.querySelectorAll('.run-backtest').forEach(button=>button.onclick=async()=>{const key=button.dataset.strategy,select=document.querySelector(`[data-symbol-for="${key}"]`),resultBox=$(`#strategy-result-${key}`);button.disabled=true;button.textContent='正在回测…';resultBox.className='inline-result running';resultBox.innerHTML='<strong>正在计算三年以上历史回测</strong><span>读取本地行情、执行策略专属风控并生成净值曲线…</span>';try{const result=await request('/api/research/backtest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({strategy_key:key,symbol:select.value})});state.strategyFocus=key;showToast(`回测完成：${result.symbol} ${researchMetric(result.metrics.total_return_pct)}%`);renderStrategyLab(await request('/api/strategy-lab'));requestAnimationFrame(()=>document.querySelector(`#strategy-${CSS.escape(key)}`)?.scrollIntoView({behavior:'smooth',block:'center'}));}catch(error){resultBox.className='inline-result failed';resultBox.innerHTML=`<strong>回测失败</strong><span>${safe(error.message)}</span>`;}finally{const live=document.querySelector(`#strategy-${CSS.escape(key)} .run-backtest`);if(live){live.disabled=false;live.textContent='运行三年+回测';}}});
  document.querySelectorAll('.run-factor').forEach(button=>button.onclick=async()=>{const key=button.dataset.factor,resultBox=$(`#factor-result-${key}`);button.disabled=true;button.textContent='计算中…';resultBox.className='inline-result running';resultBox.innerHTML='<strong>正在评估因子</strong><span>计算IC、Rank IC与模拟净值曲线…</span>';try{const result=await request('/api/research/factors/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({factor_key:key})});state.factorFocus=key;showToast(`因子完成：Rank IC ${researchMetric(result.metrics.rank_ic,4)}`);renderStrategyLab(await request('/api/strategy-lab'));requestAnimationFrame(()=>document.querySelector(`#factor-${CSS.escape(key)}`)?.scrollIntoView({behavior:'smooth',block:'center'}));}catch(error){resultBox.className='inline-result failed';resultBox.innerHTML=`<strong>因子评估失败</strong><span>${safe(error.message)}</span>`;}finally{const live=document.querySelector(`#factor-${CSS.escape(key)} .run-factor`);if(live){live.disabled=false;live.textContent='重新评估';}}});
}

function searchBar() {
  return `<section class="library-search"><div><p class="eyebrow">统一资料搜索</p><h2>一次搜索新闻、网页、研报和策略规则</h2><small>有日期的结果按最新发布优先；公开资料可直接打开原文。</small><div id="librarySearchSyncState" class="library-sync-state muted"><span><i></i>正在读取同步状态</span><small>资料每天自动更新，服务关闭期间会在下次启动时补跑。</small></div></div><div class="library-search-controls"><input id="librarySearchInput" placeholder="输入股票、代码、主题或想了解的问题"><select id="librarySearchScope" aria-label="资料分类"><option value="">全部资料</option><option value="research">新闻和网页</option><option value="reports">研究报告</option><option value="strategy">方案与风险</option></select><select id="librarySearchMode" aria-label="搜索方式"><option value="exact">按关键词</option><option value="semantic">按意思（Qwen3）</option></select><button id="librarySearchButton" class="primary-button">搜索</button></div></section>`;
}

function updateLibrarySyncState(sync){
  const target=$('#librarySearchSyncState');if(!target||!sync)return;
  const groups=sync.groups||{},reports=groups.reports||{},news=groups.news||{},tone=['good','warn','bad','muted'].includes(sync.status_tone)?sync.status_tone:'muted',completed=sync.completed_at?localTimestamp(sync.completed_at):'尚未成功';
  target.className=`library-sync-state ${tone}`;
  target.innerHTML=`<span><i></i>${safe(sync.status_label||'每日自动同步')}</span><small>最近完成 ${safe(completed)} · 跟踪 ${Number(sync.tracked_symbols||0)} 只股票 · 研报/公告 ${Number(reports.document_count||0).toLocaleString('zh-CN')} 条 · 新闻/舆情 ${Number(news.document_count||0).toLocaleString('zh-CN')} 条${Number(sync.error_count||0)?` · ${Number(sync.error_count)} 个来源待重试`:''}</small>`;
}

function selectLibraryTab(tab){
  state.libraryTab=tab;
  document.querySelectorAll('.library-tab').forEach(button=>button.classList.toggle('active',button.dataset.libraryTab===tab));
  document.querySelectorAll('.library-pane').forEach(pane=>pane.classList.toggle('active',pane.id===`library${tab[0].toUpperCase()+tab.slice(1)}Pane`));
  if(tab==='reports')loadReportLibrary();
}

function bindLibraryTabs(){
  document.querySelectorAll('.library-tab').forEach(button=>button.onclick=()=>selectLibraryTab(button.dataset.libraryTab));
  selectLibraryTab(state.libraryTab||'sources');
}

function renderLibrarySearchResults(data){
  const target=$('#librarySearchResults'),pageLabels={research:'新闻/网页',reports:'研究报告',strategy:'方案/风险'},modeLabels={exact:'关键词匹配',semantic:'按意思匹配'};
  updateLibrarySyncState(data.daily_sync);
  const notice=data.warning?`<div class="warning-strip">${safe(data.warning)}</div>`:'';
  target.innerHTML=notice+(data.results.length?`<section class="search-results"><div class="search-results-head"><strong>${data.results.length} 条结果</strong><span>最新发布优先</span></div>${data.results.map(r=>{const href=/^https?:\/\//i.test(String(r.source_ref||''))?r.source_ref:null,localTab=r.page==='strategy'?'strategies':r.page==='reports'?'reports':'sources';return `<article><div class="search-result-meta"><span>${safe(pageLabels[r.page]||'本地资料')}</span><time>${safe(r.published_at?localTimestamp(r.published_at):'未标日期')}</time></div><strong>${safe(r.title)}</strong><p>${safe(r.snippet)}</p><footer><small>${safe(r.source_name||modeLabels[r.match_mode]||'本地记录')}</small>${href?`<a href="${safe(href)}" target="_blank" rel="noopener noreferrer">打开原文 ↗</a>`:`<button class="search-local-link" data-library-target="${localTab}">查看本地内容</button>`}</footer></article>`;}).join('')}</section>`:'<div class="empty-state">没有找到匹配内容，可以换一个简称或6位股票代码。</div>');
  document.querySelectorAll('.search-local-link').forEach(button=>button.onclick=()=>{selectLibraryTab(button.dataset.libraryTarget);document.querySelector('.library-tabs')?.scrollIntoView({behavior:'smooth',block:'start'});});
}

function bindLibrarySearch() {
  const button=$('#librarySearchButton'),input=$('#librarySearchInput');
  if(!button)return;
  const run=async()=>{
    const q=input.value.trim();if(!q)return;
    const mode=$('#librarySearchMode').value,scope=$('#librarySearchScope').value,target=$('#librarySearchResults'),originalText=button.textContent;
    button.disabled=true;button.textContent='搜索中…';target.innerHTML='<div class="search-loading">正在查找本地资料…</div>';
    try{
      const pageQuery=scope?`&page=${encodeURIComponent(scope)}`:'';
      const data=await request(`/api/search?q=${encodeURIComponent(q)}&mode=${mode}${pageQuery}`);
      state.libraryResults=data.results||[];renderLibrarySearchResults(data);
    }catch(error){
      target.innerHTML=`<div class="empty-state">${safe(error.message)}。可以先改用“按关键词”。</div>`;
    }finally{
      button.disabled=false;button.textContent=originalText;
    }
  };
  button.onclick=run;input.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();run();}};
}

const clamp=(value,min=0,max=100)=>Math.max(min,Math.min(max,Number(value)||0));
const persistQuantDraft=()=>localStorage.setItem('argus-quant-draft',JSON.stringify(state.quantDraft));

function logicCandidateRows(decision){
  const result=decision?.result||{},rec=result.recommendation||{},liquidity=new Map((result.candidates||[]).map(item=>[String(item.symbol),item]));
  return (rec.candidate_ranking||[]).map(item=>({...item,...(liquidity.get(String(item.symbol))||{})}));
}

function logicPreferencePresets(profile){
  return ({aggressive:{trend:30,fundamental:20,probability:25,liquidity:15,stability:10},balanced:{trend:25,fundamental:30,probability:20,liquidity:10,stability:15},conservative:{trend:15,fundamental:40,probability:15,liquidity:5,stability:25}}[profile]||{trend:25,fundamental:30,probability:20,liquidity:10,stability:15});
}

function renderLogicSimulation(){
  const body=$('#logicSimulationBody');if(!body||!state.logicData)return;
  const weights={};document.querySelectorAll('[data-logic-weight]').forEach(input=>{weights[input.dataset.logicWeight]=Number(input.value);const output=$(`[data-logic-value="${input.dataset.logicWeight}"]`);if(output)output.textContent=`${input.value}%`;});
  const total=Object.values(weights).reduce((sum,value)=>sum+value,0)||1,rows=logicCandidateRows(state.logicData.decision),maxRank=Math.max(1,...rows.map(row=>Number(row.liquidity_rank||1)));
  state.quantDraft.preference_weights=weights;
  persistQuantDraft();
  const scored=rows.map(row=>{const backend=row.score_components||{},parts={trend:backend.trend===undefined?clamp(50+Number(row.momentum||0)*60+Number(row.trend||0)*200):clamp((Number(backend.trend)+1)*50),fundamental:backend.fundamental===undefined?clamp((Number(row.fundamental_score??-1)+1)*50):clamp((Number(backend.fundamental)+1)*50),probability:backend.probability===undefined?clamp(Number(row.probability_up||0)*100):clamp((Number(backend.probability)+1)*50),liquidity:backend.liquidity===undefined?clamp(100-(Number(row.liquidity_rank||maxRank)-1)/Math.max(1,maxRank-1)*100):clamp((Number(backend.liquidity)+1)*50),stability:backend.stability===undefined?clamp(100-Number(row.annual_volatility||1)*80):clamp((Number(backend.stability)+1)*50)};const score=Object.entries(weights).reduce((sum,[key,value])=>sum+parts[key]*value,0)/total;return{...row,parts,preference_score:score};}).sort((a,b)=>b.preference_score-a.preference_score).slice(0,8);
  $('#logicWeightTotal').textContent=`当前合计 ${total}%（系统会自动按比例归一化）`;
  body.innerHTML=scored.map((row,index)=>`<tr><td><b>${index+1}</b></td><td><strong>${safe(row.name)}</strong><small>${safe(row.symbol)}</small></td><td>${row.preference_score.toFixed(1)}</td><td>${row.parts.trend.toFixed(0)}</td><td>${row.parts.fundamental.toFixed(0)}</td><td>${row.parts.probability.toFixed(0)}</td><td>${row.parts.liquidity.toFixed(0)}</td><td>${row.parts.stability.toFixed(0)}</td><td><span class="status-tag">${row.eligible?'正式条件通过':row.research_recommended?'重点关注':'继续观察'}</span></td></tr>`).join('')||'<tr><td colspan="9">当前推荐结果中没有可试算的候选股票</td></tr>';
  const output=$('.logic-output-node');if(output){output.classList.remove('updated');requestAnimationFrame(()=>{output.classList.add('updated');window.setTimeout(()=>output.classList.remove('updated'),420);});}
}

function bindLogicView(){
  document.querySelectorAll('[data-logic-weight]').forEach(input=>input.oninput=renderLogicSimulation);
  document.querySelectorAll('[data-logic-preset]').forEach(button=>button.onclick=()=>{const preset=logicPreferencePresets(button.dataset.logicPreset);Object.entries(preset).forEach(([key,value])=>{const input=$(`[data-logic-weight="${key}"]`);if(input)input.value=value;});document.querySelectorAll('[data-logic-preset]').forEach(item=>item.classList.toggle('active',item===button));renderLogicSimulation();});
  $('#logicStrategyStyle').onchange=event=>{state.quantDraft.strategy_style=event.target.value;persistQuantDraft();};
  $('#logicBacktestWindow').onchange=event=>{state.quantDraft.backtest_window_years=Number(event.target.value);persistQuantDraft();};
  $('#logicApplyPreferences').onclick=async()=>{const button=$('#logicApplyPreferences');button.disabled=true;button.textContent='正在保存并重新计算…';try{renderLogicSimulation();const registered=await request('/api/quant/mandates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input:quantDecisionInput()})});state.quantDecision=registered.decision;showToast('设置已保存，并已用于当前推荐；正式版本等待盘后完整回测');await loadLogic();}catch(error){showToast(`保存失败：${error.message}`);button.disabled=false;button.textContent='保存并用于股票推荐';}};
  $('#logicOpenRecommendation').onclick=()=>document.querySelector('.nav-item[data-view="harness"]').click();
  renderLogicSimulation();
}

function renderLogicView(methodology,decision){
  state.logicData={methodology,decision};
  const result=decision?.result||{},rec=result.recommendation||{},params=rec.parameters||{},profile=params.profile||rec.profile||'balanced',profileData=methodology.profiles?.[profile]||{},preset=state.quantDraft.preference_weights||logicPreferencePresets(profile),steps=methodology.decision_steps||[];
  const dimensionBars=(profileData.dimension_weights||[]).map(item=>`<div class="logic-weight-bar"><span>${safe(item.label)}</span><i><b style="width:${Number(item.weight)*100}%"></b></i><strong>${(Number(item.weight)*100).toFixed(0)}%</strong></div>`).join('');
  const parameterRows=[['细分方法',params.strategy_style_label||'自动比较'],['短期均价',`${params.fast_window||'—'}个交易日`],['长期均价',`${params.slow_window||'—'}个交易日`],['动量观察期',`${params.momentum_window||'—'}个交易日`],['上涨概率最低要求',params.prediction_floor===undefined?'—':`${(Number(params.prediction_floor)*100).toFixed(1)}%`],['重新选股间隔',`${params.rebalance_days||'—'}个交易日`],['单只仓位上限',params.max_position_pct===undefined?'—':`${(Number(params.max_position_pct)*100).toFixed(0)}%`],['本次参数编号',decision?.mandate?.input?.preference_version||'等待保存']].map(([name,value])=>`<tr><td>${name}</td><td>${safe(value)}</td></tr>`).join('');
  const featureRows=(methodology.online_model?.features||[]).map(item=>`<tr><td>${safe(item.label)}</td><td><code>${safe(item.key)}</code></td><td>${Number(item.initial_coefficient).toFixed(2)}</td></tr>`).join('');
  const adjustRows=(methodology.adjustability||[]).map(item=>`<tr><td>${safe(item.item)}</td><td><span class="logic-mode">${safe(item.mode)}</span></td><td>${safe(item.effect)}</td></tr>`).join('');
  const sliders=[['trend','近期走势'],['fundamental','财务情况'],['probability','上涨概率'],['liquidity','成交活跃度'],['stability','波动稳定性']].map(([key,label])=>`<label class="logic-slider"><span>${label}<output data-logic-value="${key}">${preset[key]}%</output></span><input type="range" min="0" max="60" step="1" value="${preset[key]}" data-logic-weight="${key}"></label>`).join('');
  const styleOptions=(methodology.strategy_styles||[]).map(item=>`<option value="${safe(item.key)}" ${state.quantDraft.strategy_style===item.key?'selected':''}>${safe(item.label)}：${safe(item.plain_description)}</option>`).join('');
  $('#logicView').innerHTML=`<section class="logic-intro"><div><p class="eyebrow">推荐过程说明</p><h2>从你的条件，到最终推荐</h2><p class="beginner-only">系统先检查你的要求，再看股票资料是否完整、风险是否合格，最后才形成研究结论。</p><p class="professional-only">这里展示当前系统真实使用的步骤、公式、参数版本和可见数据截止日。所有推荐仅供研究，不会下单，也不代表保证收益。</p></div><div class="logic-version professional-only"><span>当前结果编号</span><strong>${safe(decision?.version?.version_key||'暂无')}</strong><small>数据截至 ${safe(decision?.data_asof||rec.data_asof||'—')}</small></div></section>
  <section class="logic-path" aria-label="推荐决策过程">${steps.map((step,index)=>`<article class="logic-step ${index===steps.length-1?'logic-output-node':''}"><b>${index+1}</b><div><strong>${safe(step.label)}</strong><small>${safe(step.detail)}</small></div></article>`).join('')}</section>
  <section class="logic-grid professional-only"><div class="logic-band"><div class="logic-section-head"><div><p class="eyebrow">当前正式公式</p><h2>股票分数怎么算</h2></div><span class="count">${safe(rec.profile_label||profileData.label||profile)}风险偏好</span></div><div class="formula-main">${safe(methodology.ranking_formula?.display||'')}</div><div class="formula-list"><p>${safe(methodology.ranking_formula?.momentum||'')}</p><p>${safe(methodology.ranking_formula?.trend||'')}</p><p>${safe(methodology.ranking_formula?.volatility||'')}</p><p><strong>能否正式入选：</strong>${safe(methodology.ranking_formula?.eligibility||'')}</p></div><div class="logic-dimensions"><div><strong>财务分内部构成</strong><small>${safe(profileData.label||profile)}预设，财务总权重 ${(Number(profileData.fundamental_weight||0)*100).toFixed(0)}%</small></div>${dimensionBars}</div></div><div class="logic-band"><div class="logic-section-head"><div><p class="eyebrow">本次生效参数</p><h2>当前到底用了什么</h2></div><span class="count">来自已评测版本</span></div><div class="table-wrap"><table class="logic-table"><tbody>${parameterRows}</tbody></table></div><p class="logic-note">止损、止盈、最大回撤等用户条件会覆盖同名预设；模型参数只有重新评测通过后才会成为正式版本。</p></div></section>
  <section class="logic-workbench"><div class="logic-section-head"><div><p class="eyebrow">你的推荐设置</p><h2><span class="beginner-only">选择你更看重什么</span><span class="professional-only">调整后保存，直接影响股票推荐</span></h2></div><div class="logic-presets"><button data-logic-preset="aggressive" class="${profile==='aggressive'?'active':''}">偏走势</button><button data-logic-preset="balanced" class="${profile==='balanced'?'active':''}">均衡</button><button data-logic-preset="conservative" class="${profile==='conservative'?'active':''}">偏财务与稳定</button></div></div><p class="logic-note strong beginner-only">选择侧重点后保存；没有通过历史检查的新设置不会替换上一套正式结果。</p><p class="logic-note strong professional-only">滑动时先预览排序；点击保存后，后端会用同一组设置重算当前推荐，并在下一个交易日盘后完成滚动回测。未通过样本外检查的设置不会覆盖上一正式版本。</p><div class="logic-controls professional-only"><label>细分选股方法<select id="logicStrategyStyle"><option value="auto" ${state.quantDraft.strategy_style==='auto'?'selected':''}>自动比较全部方法</option>${styleOptions}</select></label><label>每天回看多长历史<select id="logicBacktestWindow"><option value="1" ${Number(state.quantDraft.backtest_window_years)===1?'selected':''}>近1年（另加指标预热数据）</option><option value="3" ${Number(state.quantDraft.backtest_window_years)===3?'selected':''}>近3年（默认，约756个交易日）</option><option value="5" ${Number(state.quantDraft.backtest_window_years)===5?'selected':''}>近5年</option></select></label></div><div class="logic-simulator professional-only"><div class="logic-sliders">${sliders}<small id="logicWeightTotal"></small><p>综合分 = 五项分数 × 你的权重。成交活跃度来自成交额，不等于“主力资金”。</p></div><div class="table-wrap"><table class="logic-ranking"><thead><tr><th>#</th><th>股票</th><th>预览综合分</th><th>走势</th><th>财务</th><th>概率</th><th>活跃</th><th>稳定</th><th>当前状态</th></tr></thead><tbody id="logicSimulationBody"></tbody></table></div></div><div class="logic-workbench-actions"><button id="logicApplyPreferences" class="primary-button">保存并用于股票推荐</button><button id="logicOpenRecommendation" class="secondary-button">查看推荐结果</button></div></section>
  <section class="logic-grid logic-bottom professional-only"><div class="logic-band"><div class="logic-section-head"><div><p class="eyebrow">上涨概率模型</p><h2>每天学习哪些信息</h2></div><span class="count">开盘日更新</span></div><div class="table-wrap"><table class="logic-table"><thead><tr><th>通俗名称</th><th>程序字段</th><th>初始系数</th></tr></thead><tbody>${featureRows}</tbody></table></div><p class="logic-note">${safe(methodology.online_model?.coefficient_note||'')}</p></div><div class="logic-band"><div class="logic-section-head"><div><p class="eyebrow">能不能手动调</p><h2>哪些设置立即生效</h2></div></div><div class="table-wrap"><table class="logic-table"><thead><tr><th>设置</th><th>调整方式</th><th>实际影响</th></tr></thead><tbody>${adjustRows}</tbody></table></div></div></section>`;
  bindLogicView();
}

async function loadLogic(){
  const view=$('#logicView');if(!view)return;if(!view.children.length)view.innerHTML='<div class="search-loading">正在读取推荐公式和当前参数…</div>';
  try{const [methodology,decision]=await Promise.all([request('/api/quant/methodology'),request('/api/quant/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input:quantDecisionInput()})})]);const saved=decision?.mandate?.input||{};if(saved.preference_weights)state.quantDraft.preference_weights=Object.fromEntries(Object.entries(saved.preference_weights).map(([key,value])=>[key,Math.round(Number(value)*100)]));if(saved.strategy_style)state.quantDraft.strategy_style=saved.strategy_style;if(saved.backtest_window_years)state.quantDraft.backtest_window_years=Number(saved.backtest_window_years);renderLogicView(methodology,decision);}catch(error){view.innerHTML=`<div class="empty-state">${safe(error.message)}</div>`;}
}

function harnessJson(text,fallbackKey='query'){const value=String(text||'').trim();if(!value)return{};try{return JSON.parse(value);}catch{return{[fallbackKey]:value};}}
function harnessStatus(status){return({OBSERVED:'待补期望',READY:'待评测',RESOLVED:'已解决',IGNORED:'已忽略',DRAFT:'候选草案',EVALUATED:'已评测',ACTIVE:'已生效',SUCCESS:'通过',SUCCESS_WITH_WARNINGS:'完成，有警告',WARNING:'警告',FAILED:'失败',ERROR:'错误',QUEUED:'排队中',PENDING:'等待中',RUNNING:'运行中',RUNNING_MARKET:'正在补充行情数据',RUNNING_FUNDAMENTALS:'正在补充财务数据',COMPLETED_WITH_WARNINGS:'完成，有缺口',WAITING_APPROVAL:'等待批准',COMPLETED:'已完成',INTERRUPTED:'可恢复',CANCELLED:'已取消',PROPOSED:'待执行',RETRYING:'重试中',SUCCEEDED:'成功',SKIPPED:'已跳过',APPROVED:'已批准',REJECTED:'已拒绝'}[status]||status);}
function harnessWorkflow(key){return({stock_analysis:'股票研判',quality_audit:'质量巡检',quant_portfolio:'量化选股与组合',strategy_evolution:'策略自进化',continuous_learning:'持续自进化',strategy_activation:'策略激活',candidate_activation:'候选晋级',version_rollback:'版本回滚'}[key]||key);}

function strategyResultRows(strategies){return(strategies||[]).map(item=>{const metrics=item.holdout?.metrics||{},gate=metrics.risk_pass&&item.non_regression_pass;return`<div class="strategy-result-row"><strong>${safe(item.label||item.profile)}</strong><span>迭代 ${item.selected_iteration||'—'}</span><span>留出收益 ${metrics.total_return===undefined?'—':(Number(metrics.total_return)*100).toFixed(1)+'%'}</span><span>最大回撤 ${metrics.max_drawdown===undefined?'—':(Math.abs(Number(metrics.max_drawdown))*100).toFixed(1)+'%'}</span><b class="status-tag">${gate?'风控与非退化通过':(metrics.risk_pass?'基线对照未通过':'风控未通过')}</b></div>`;}).join('');}

function forecastPointMetricsHtml(forecast){
  const calibrated=(forecast.timeframes||[]).filter(row=>row.status==='AVAILABLE'&&Number(row.horizon_trading_days)>0).sort((a,b)=>Number(b.horizon_trading_days)-Number(a.horizon_trading_days))[0],requested=forecast.requested_forecast||{},curve=forecast.forecast_curve||[],endpoint=curve.at(-1)||{},base=Number(curve[0]?.p50||requested.last_price||0),endpointPrice=Number(endpoint.p50),endpointReturn=base>0&&Number.isFinite(endpointPrice)?endpointPrice/base-1:null,requestedUp=Number(requested.historical_up_ratio),effective=Number(requested.effective_sample_count||0),samples=Number(requested.sample_count||0),calibratedReturn=calibrated&&Number(calibrated.last_price)>0?Number(calibrated.p50_price)/Number(calibrated.last_price)-1:null,calibratedText=calibratedReturn===null?'暂无通过门禁的方向':`${calibrated.label} ${calibratedReturn>=0?'+':''}${(calibratedReturn*100).toFixed(1)}%`,endpointLabel=requested.reference_only?'期限基准中位':'期限模型中位',frequencyLabel=requested.reference_only?'历史上涨频率':'校准上涨概率';
  return `<div class="stock-forecast-points"><div><span>最长已校准判断</span><strong>${safe(calibratedText)}</strong><small>${calibrated?`${Number(calibrated.validation_count||0)} 个滚动检验段`:'不用于触发买卖'}</small></div><div><span>${endpointLabel}</span><strong>${endpointReturn===null?'—':`¥ ${money(endpointPrice,base<10?3:2)} · ${endpointReturn>=0?'+':''}${(endpointReturn*100).toFixed(1)}%`}</strong><small>${Number(forecast.requested_horizon_trading_days||0)} 个交易日终点</small></div><div><span>${frequencyLabel}</span><strong>${Number.isFinite(requestedUp)?(requestedUp*100).toFixed(1)+'%':'—'}</strong><small>${requested.reference_only?'来自历史同期限样本':'样本外校准后估计'}</small></div><div><span>历史支撑</span><strong>${samples} 个样本</strong><small>约 ${effective} 个不重叠周期</small></div></div>`;
}

function quantTimeframeForecastHtml(item){
  const forecast=item.timeframe_forecast||{},summary=forecast.summary||{},sell=item.sell_conclusion||summary.sell_review||{},actionClass=summary.action==='SELL_REVIEW'?'sell':summary.action==='REDUCE_REVIEW'?'reduce':summary.action==='BUY_WATCH'?'buy':'hold',sellHtml=['TRIGGERED','WATCH'].includes(sell.status)&&sell.label?`<div class="quant-sell-conclusion"><b>${safe(sell.label)}</b><small>${safe(sell.reason||'')}</small></div>`:'',plotted=Number(forecast.horizon_trading_days||0),validated=Number(forecast.validated_horizon_trading_days??plotted),requested=Number(forecast.requested_horizon_trading_days||0),hasBaseline=(forecast.timeframes||[]).some(row=>row.status==='BASELINE_REFERENCE'),edge=(forecast.timeframes||[]).some(row=>row.status==='AVAILABLE'&&row.predictive_edge_detected),validationLabel=forecast.horizon_status==='FULL'?`严格校准覆盖全部 ${validated} 个交易日`:hasBaseline?`${validated?`严格校准 ${validated} 日；`:''}历史基准情景延伸至 ${plotted} / ${requested} 日`:`只校准到 ${validated} / ${requested} 个交易日`,basisLabel=edge?'条件模型通过增益检验':hasBaseline?'基准情景未证明预测优势':'没有证据优于历史基准';
  const displayLabel=hasBaseline?(validated?`主图只显示已校准的前 ${validated} 日；长期基准见专业表格`:'没有通过检验的未来区间，主图不画预测带'):validationLabel;
  return `<article class="quant-forecast-card ${actionClass}"><header><div><strong>${safe(item.name)} <small>${safe(item.symbol)}</small></strong><span>${safe(item.action_label||summary.action_label||'继续观察')} · ${safe(displayLabel)} · ${safe(basisLabel)}</span></div>${sellHtml}</header>${forecastPointMetricsHtml(forecast)}${stockForecastCurveSvg(forecast)}<footer><span>绿色实线：最近90根日K</span><span>${validated?`黄色虚线：前 ${validated} 日校准中位数`:'未绘制未校准预测线'}</span><span>${hasBaseline?'长期历史基准仅在专业表格显示':'深浅色带：样本外检验后的概率区间'}</span></footer></article>`;
}

function stockForecastCurveSvg(forecast){
  const history=(forecast.history_curve||[]).filter(item=>Number.isFinite(Number(item.price))),source=(forecast.forecast_curve||[]).filter(item=>Number.isFinite(Number(item.trading_day))&&['p10','p25','p50','p75','p90'].every(key=>Number.isFinite(Number(item[key])))),validatedDays=Math.max(0,Number(forecast.validated_horizon_trading_days||0)),hasReferenceTail=source.some(item=>item.basis==='HISTORICAL_BASELINE_REFERENCE'&&Number(item.trading_day)>validatedDays),future=hasReferenceTail?source.filter(item=>Number(item.trading_day)<=validatedDays):source;
  if(history.length<2)return '<div class="empty-state">现有日K不足，无法进行滚动样本外校准</div>';
  const hasForecast=future.length>1,hasVisibleBaseline=future.some(item=>item.basis==='HISTORICAL_BASELINE_REFERENCE'),maxForecastDay=hasForecast?Math.max(...future.map(item=>Number(item.trading_day))):0,w=820,h=260,left=64,right=24,top=28,bottom=48,plotRight=w-right,split=hasForecast?left+(plotRight-left)*.43:plotRight;
  const values=[...history.map(item=>Number(item.price)),...future.flatMap(item=>[Number(item.p10),Number(item.p90)])],rawMin=Math.min(...values),rawMax=Math.max(...values),padding=Math.max((rawMax-rawMin)*.08,Math.abs(Number(history.at(-1).price))*.005,.01),min=rawMin-padding,max=rawMax+padding,span=Math.max(max-min,.0001);
  const xHistory=index=>left+index/(history.length-1)*(split-left),xFuture=day=>split+(maxForecastDay?day/maxForecastDay:0)*(plotRight-split),y=value=>top+(max-Number(value))/span*(h-top-bottom);
  const historyPoints=history.map((item,index)=>`${xHistory(index).toFixed(1)},${y(item.price).toFixed(1)}`).join(' '),futurePoints=key=>future.map(item=>`${xFuture(Number(item.trading_day)).toFixed(1)},${y(item[key]).toFixed(1)}`).join(' '),bandPoints=(upper,lower)=>`${futurePoints(upper)} ${[...future].reverse().map(item=>`${xFuture(Number(item.trading_day)).toFixed(1)},${y(item[lower]).toFixed(1)}`).join(' ')}`;
  const base=Number(history.at(-1).price),decimals=base<10?3:2,phaseLabel=hasVisibleBaseline?'历史基准概率情景':'样本外校准区间',forecastSvg=hasForecast?`<polygon class="forecast-band-wide" points="${bandPoints('p90','p10')}"/><polygon class="forecast-band-likely" points="${bandPoints('p75','p25')}"/><line class="forecast-start" x1="${split}" y1="${top}" x2="${split}" y2="${h-bottom}"/><line class="forecast-base" x1="${split}" y1="${y(base).toFixed(1)}" x2="${plotRight}" y2="${y(base).toFixed(1)}"/><polyline class="forecast-median ${hasVisibleBaseline?'baseline-reference':''}" points="${futurePoints('p50')}"/><text class="phase-label" x="${split+8}" y="${top+13}">${phaseLabel}</text>`:'';
  const horizonNote=hasReferenceTail?(validatedDays?`精度优先：主图只显示通过滚动检验的前 ${validatedDays} 个交易日`:'没有未来区间通过样本外检验，主图不绘制预测带'):forecast.horizon_status==='FULL'?`已覆盖请求的 ${Number(forecast.requested_horizon_trading_days||maxForecastDay)} 个交易日`:(forecast.horizon_reason||`当前情景展示到 ${maxForecastDay} 个交易日`),title=hasForecast?'历史价格与已校准概率路径':'历史价格与校准状态',description=hasForecast?'绿色实线为最近日K收盘价，黄色虚线为经滚动样本外检验的中位数，阴影为同一校准期内的概率区间。':'绿色实线为最近日K收盘价；没有通过样本外检验的未来区间时不绘制黄色预测带。';
  return `<div class="quant-chart-wrap stock-forecast-chart"><svg class="quant-equity-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="${safe(String(forecast.symbol||''))}${safe(title)}"><title>${safe(title)}</title><desc>${safe(description)}</desc><line class="chart-grid" x1="${left}" y1="${top}" x2="${plotRight}" y2="${top}"/><line class="chart-grid" x1="${left}" y1="${h-bottom}" x2="${plotRight}" y2="${h-bottom}"/>${forecastSvg}<polyline class="history-line" points="${historyPoints}"/><text x="${left}" y="${top+13}">${money(max,decimals)}</text><text x="${left}" y="${h-bottom-6}">${money(min,decimals)}</text><text x="${left}" y="${h-12}">${safe(history[0].date)}</text><text x="${split}" y="${h-12}" text-anchor="middle">${safe(history.at(-1).date)}</text>${hasForecast?`<text x="${plotRight}" y="${h-12}" text-anchor="end">已验证 ${maxForecastDay} 个交易日</text>`:''}</svg><div class="quant-chart-legend"><span class="legend-history">历史收盘走线</span>${hasForecast?`<span class="legend-median">校准中位数</span><span class="legend-likely">核心范围 P25—P75</span><span class="legend-wide">已检验范围 P10—P90</span>`:''}</div><small class="quant-chart-note">${safe(horizonNote)}。${hasReferenceTail?'更长期历史基准只保留在“专业数据与计算依据”表格中，不作为精确预测。':''} 概率路径不是确定价格或收益承诺。</small></div>`;
}

function portfolioFutureCurveSvg(forecast,targetReturnPct){
  const capital=Number(forecast?.capital||0),source=(forecast?.curve||[]).filter(item=>Number.isFinite(Number(item.trading_day))&&['p10','p25','p50','p75','p90'].every(key=>Number.isFinite(Number(item[key])))).sort((a,b)=>Number(a.trading_day)-Number(b.trading_day));
  if(forecast?.status!=='AVAILABLE'||capital<=0||source.length<2)return `<div class="empty-state">${safe(forecast?.reason||'当前没有可汇总的组合预测路径')}</div>`;
  const curve=Number(source[0].trading_day)>0?[{trading_day:0,p10:capital,p25:capital,p50:capital,p75:capital,p90:capital},...source]:source,maxDay=Math.max(...curve.map(item=>Number(item.trading_day))),targetAmount=capital*(1+Number(targetReturnPct||0)/100);
  const w=820,h=270,left=72,right=24,top=30,bottom=50,plotRight=w-right,values=[capital,targetAmount,...curve.flatMap(item=>[Number(item.p10),Number(item.p90)])],rawMin=Math.min(...values),rawMax=Math.max(...values),padding=Math.max((rawMax-rawMin)*.09,capital*.005,1),min=rawMin-padding,max=rawMax+padding,span=Math.max(max-min,1);
  const x=day=>left+(maxDay?Number(day)/maxDay:0)*(plotRight-left),y=value=>top+(max-Number(value))/span*(h-top-bottom),points=key=>curve.map(item=>`${x(item.trading_day).toFixed(1)},${y(item[key]).toFixed(1)}`).join(' '),band=(upper,lower)=>`${points(upper)} ${[...curve].reverse().map(item=>`${x(item.trading_day).toFixed(1)},${y(item[lower]).toFixed(1)}`).join(' ')}`;
  const correlation=forecast.correlation||{},excluded=forecast.excluded_allocations||[],baseline=forecast.baseline_allocations||[],hasBaseline=baseline.length>0,horizonNote=forecast.horizon_status==='FULL'?`已覆盖请求的 ${Number(forecast.requested_horizon_trading_days||maxDay)} 个交易日`:(forecast.horizon_reason||`当前情景展示到 ${maxDay} 个交易日`),methodNote=correlation.status==='AVAILABLE'?`使用 ${Number(correlation.common_daily_samples||0)} 个共同交易日和 ${Number(correlation.simulation_count||0)} 次经验 Copula 情景，保留股票同期相关性。`:'相关性样本不足，当前组合区间仅作边际汇总。',excludedNote=excluded.length?`${excluded.map(item=>item.name||item.symbol).join('、')}没有可用情景，其 ¥${money(excluded.reduce((sum,item)=>sum+Number(item.amount||0),0),0)} 在图中按零收益静态处理。`:'',basisNote=hasBaseline?`${baseline.map(item=>item.name||item.symbol).join('、')}包含未证明预测优势的历史基准情景。`:'逐股边际均通过相应期限的样本外门禁。';
  return `<div class="quant-chart-wrap portfolio-forecast-chart"><svg class="quant-equity-chart" viewBox="0 0 ${w} ${h}" role="img" aria-label="组合相关情景收益区间"><title>组合相关情景收益区间</title><desc>黄色虚线是逐股校准区间或历史基准情景合成的组合中位情景，深浅色带表示情景范围，红色虚线是用户填写的收益目标。</desc><line class="chart-grid" x1="${left}" y1="${top}" x2="${plotRight}" y2="${top}"/><line class="chart-grid" x1="${left}" y1="${h-bottom}" x2="${plotRight}" y2="${h-bottom}"/><polygon class="forecast-band-wide" points="${band('p90','p10')}"/><polygon class="forecast-band-likely" points="${band('p75','p25')}"/><line class="forecast-base" x1="${left}" y1="${y(capital).toFixed(1)}" x2="${plotRight}" y2="${y(capital).toFixed(1)}"/><line class="forecast-target" x1="${left}" y1="${y(targetAmount).toFixed(1)}" x2="${plotRight}" y2="${y(targetAmount).toFixed(1)}"/><polyline class="forecast-median ${hasBaseline?'baseline-reference':''}" points="${points('p50')}"/><text class="target-label" x="${plotRight}" y="${Math.max(top+12,y(targetAmount)-6).toFixed(1)}" text-anchor="end">目标 ¥${money(targetAmount,0)}</text><text x="${left}" y="${top+13}">¥${money(max,0)}</text><text x="${left}" y="${h-bottom-7}">¥${money(min,0)}</text><text x="${left}" y="${h-13}">当前 ¥${money(capital,0)}</text><text x="${plotRight}" y="${h-13}" text-anchor="end">情景范围 ${maxDay} 个交易日</text></svg><div class="quant-chart-legend"><span class="legend-median">组合中位情景</span><span class="legend-likely">核心情景 P25—P75</span><span class="legend-wide">较宽情景 P10—P90</span><span class="legend-target">你的收益目标</span></div><small class="quant-chart-note">${safe(horizonNote)}。${safe(methodNote)}${safe(excludedNote)}${safe(basisNote)}当前资金分配组合尚未单独回测，所有金额只用于研究情景。</small></div>`;
}

function quantTimeframeForecastDetailHtml(item){
  const forecast=item.timeframe_forecast||{},timeframes=forecast.timeframes||[],available=timeframes.filter(row=>['AVAILABLE','BASELINE_REFERENCE'].includes(row.status)),missing=timeframes.filter(row=>!['AVAILABLE','BASELINE_REFERENCE'].includes(row.status));
  const rows=available.map(row=>{const validation=row.validation||{},coverage=validation.wide_80_coverage,coverageCi=validation.wide_80_coverage_ci90||[],calibrated=row.status==='AVAILABLE'&&validation.gate_passed===true,reference=row.status==='BASELINE_REFERENCE',basis=reference?'历史基准情景，未通过模型门禁':calibrated?(row.forecast_basis==='CONDITIONAL'?'条件模型通过增益检验':'经校准的历史基准'):'盘中描述，未参与未来线',directionText=Number.isFinite(Number(validation.direction_accuracy))?`方向命中 ${(Number(validation.direction_accuracy)*100).toFixed(1)}% · 基准 ${(Number(validation.baseline_direction_accuracy)*100).toFixed(1)}%`:'',coverageText=Number.isFinite(Number(coverage))?`${(Number(coverage)*100).toFixed(1)}%${coverageCi.length===2?`<small>90%区间 ${(Number(coverageCi[0])*100).toFixed(1)}%—${(Number(coverageCi[1])*100).toFixed(1)}%${reference?' · 门禁未通过':''}</small>`:''}`:'—',sampleText=validation.walk_forward_points?`${Number(validation.walk_forward_points)} 个滚动样本外点 / ${Number(validation.evaluation_points||0)} 个最终检验点`:`${Number(row.sample_count||0)} 个历史样本 · 约 ${Number(row.effective_sample_count||0)} 个独立周期`;return`<tr><td><strong>${safe(row.label)}</strong><small>${safe(row.source_period)}</small></td><td>${money(row.last_price,Number(row.last_price)<10?3:2)}</td><td>${money(row.p10_price,Number(row.last_price)<10?3:2)} — ${money(row.p90_price,Number(row.last_price)<10?3:2)}</td><td>${money(row.p50_price,Number(row.last_price)<10?3:2)}</td><td><span class="quant-forecast-signal ${String(row.signal||'').toLowerCase()}"><strong>${safe(row.signal_label)}</strong></span><small>${safe(basis)}</small>${directionText?`<small>${safe(directionText)}</small>`:''}</td><td>${coverageText}</td><td>${safe(sampleText)}</td></tr>`;}).join('');
  const missingText=missing.length?`暂缺：${missing.map(row=>`${row.label}（${plainQuantText(row.reason)}）`).join('；')}`:'';
  return `<section class="quant-forecast-evidence"><div class="subsection-head"><span>${safe(item.name)} <small>${safe(item.symbol)}</small></span><small>${safe(forecast.validation_status||'未校准')} · 不使用未来数据</small></div><div class="table-wrap"><table class="quant-forecast-table"><thead><tr><th>周期 / 数据源</th><th>当前价</th><th>P10—P90</th><th>中位价</th><th>方向 / 增益</th><th>80%区间实测覆盖 / 90%区间</th><th>样本外检验</th></tr></thead><tbody>${rows||'<tr><td colspan="7">历史不足，当前无法生成概率情景</td></tr>'}</tbody></table></div>${missingText?`<p class="quant-forecast-missing">${safe(missingText)}</p>`:''}</section>`;
}

function quantRuleSnapshotHtml(quant,rec){
  const ranking=(rec.candidate_ranking||[]).slice(0,8),watchlist=rec.research_watchlist||[],allocations=rec.research_allocations||[],portfolioForecast=rec.portfolio_forecast||{},rules=rec.rules||{},version=quant.version||{},request=quant.request||{},historyWindow=quant.inference?.history_window||rec.credibility?.history_window||{},compressedHistory=historyWindow.mode==='COMPRESSED',historyNotice=compressedHistory?`共同历史 ${Number(historyWindow.common_trading_days||0)} 天，已压缩即时计算窗口；可信度低于 ${Number(historyWindow.standard_days||420)} 天标准窗口。`:'',passed=ranking.filter(item=>item.rule_pass).length,capital=Number(portfolioForecast.capital||request.capital||0),invested=Number(portfolioForecast.invested_amount||0),cash=Number(portfolioForecast.cash_amount??Math.max(0,capital-invested)),endpoint=portfolioForecast.endpoint||{},endpointReturns=portfolioForecast.endpoint_returns||{},endpointProbabilities=portfolioForecast.endpoint_probabilities||{},calibratedProbabilities=portfolioForecast.calibrated_horizon_probabilities||{},requestedDays=Number(portfolioForecast.requested_horizon_trading_days||Math.round(Number(request.horizon_months||0)*21)),scenarioDays=Number(portfolioForecast.horizon_trading_days||0),validatedDays=Number(portfolioForecast.validated_horizon_trading_days??scenarioDays),hasBaseline=(portfolioForecast.baseline_allocations||[]).length>0,coverageText=portfolioForecast.horizon_status==='FULL'?`严格校准覆盖全部 ${validatedDays} 个交易日`:hasBaseline?`情景覆盖 ${scenarioDays} / ${requestedDays} 日 · 共同严格校准 ${validatedDays} 日`:`请求 ${requestedDays} 个交易日，当前只通过 ${validatedDays} 个交易日`;
  const allocationCards=allocations.map(item=>`<article class="research-allocation-card"><header><div><strong>${safe(item.name)} <small>${safe(item.symbol)}</small></strong><span>${safe(item.action_label||'研究分配建议')}</span></div><b>¥ ${money(item.amount||0,2)}</b></header><div class="allocation-primary"><div><span>参考买入数量</span><strong>${Number(item.shares||0).toLocaleString('zh-CN')} 股</strong><small>按 ${Number(request.execution?.lot_size||100)} 股一手取整</small></div><div><span>实际占本金</span><strong>${(Number(item.weight||0)*100).toFixed(1)}%</strong><small>目标 ${(Number(item.target_weight||0)*100).toFixed(1)}%</small></div></div><dl><div><dt>最近收盘参考</dt><dd>¥ ${money(item.reference_price||0,Number(item.reference_price)<10?3:2)}</dd></div><div><dt>止损复核价</dt><dd>¥ ${money(item.stop_price||0,Number(item.reference_price)<10?3:2)}</dd></div><div><dt>浮动止盈起点</dt><dd>¥ ${money(item.take_profit_price||0,Number(item.reference_price)<10?3:2)}</dd></div><div><dt>触及止损计划亏损</dt><dd>¥ ${money(item.planned_loss_at_stop||0,2)}</dd></div></dl></article>`).join('');
  const forecasts=allocations.map(quantTimeframeForecastHtml).join('');
  const forecastDetails=allocations.map(quantTimeframeForecastDetailHtml).join('');
  const rows=ranking.map((item,index)=>{const blockers=(item.rejection_reasons||[]).map(plainQuantReason),status=item.rule_pass?'通过基础规则':item.fundamental_score==null?'财务待补':'观察';return`<tr><td>${index+1}</td><td><strong>${safe(item.name)}</strong><small>${safe(item.symbol)}</small></td><td>${item.reference_price==null?'—':money(item.reference_price,2)}</td><td>${item.momentum==null?'—':`${Number(item.momentum)>=0?'+':''}${(Number(item.momentum)*100).toFixed(1)}%`}</td><td>${item.trend==null?'—':`${Number(item.trend)>=0?'+':''}${(Number(item.trend)*100).toFixed(1)}%`}</td><td>${item.fundamental_score==null?'资料不足':Number(item.fundamental_score).toFixed(2)}</td><td>${item.annual_volatility==null?'—':(Number(item.annual_volatility)*100).toFixed(1)+'%'}</td><td>${item.composite_score==null?'—':Number(item.composite_score).toFixed(3)}</td><td><span class="status-tag">${safe(status)}</span><small>${safe(blockers.join('；')||(item.fundamental_score==null?'财务数据不足，当前仅按行情因子排序':'未触发基础阻断项'))}</small></td></tr>`;}).join('');
  const actionSummary=allocations.map(item=>`${item.name||item.symbol} ¥${money(item.amount||0,0)}（${Number(item.shares||0)}股）`).join('；');
  return `<section class="quant-result rule-snapshot-result"><div class="quant-result-head"><div><span class="status-tag">${safe(rec.profile_label||rec.profile||'中立')}</span><strong>即时多因子与K线预测</strong><small>数据截至 ${safe(rec.data_asof||'—')} · 使用本地缓存</small></div><b>${safe(quantVersionLabel(version.status))}</b></div>
    <div class="quant-decision-callout rule_snapshot"><span>按你的本金、期限与风险条件即时计算</span><strong>${actionSummary?safe(actionSummary):'当前不建议为候选股票分配资金，全部保留现金'}</strong>${historyNotice?`<small>${safe(historyNotice)}</small>`:''}<small>这是研究分配建议，不是你的真实持仓，也不会自动下单；实际成交前需人工复核价格与风险。</small></div>
    <div class="quant-plain-metrics allocation-metrics"><div><span>给定本金</span><strong>¥ ${money(capital,2)}</strong></div><div><span>建议投入</span><strong>¥ ${money(invested,2)}</strong></div><div><span>保留现金</span><strong>¥ ${money(cash,2)}</strong></div><div><span>建议股票</span><strong>${allocations.length} / ${Number(request.max_positions||allocations.length)}</strong></div></div>
    <section class="research-allocation-section"><div class="subsection-head"><span>建议资金分配（研究）</span><small>按因子、预测方向和波动分配，再按整手取整</small></div><div class="research-allocation-grid">${allocationCards||`<div class="empty-state">${safe(portfolioForecast.reason||'当前没有通过风险复核且能按整手买入的股票')}</div>`}</div></section>
    <section class="portfolio-forecast-section"><div class="subsection-head"><span>组合相关情景区间</span><small>以 ¥${money(capital,0)} 本金为起点 · ${safe(coverageText)}</small></div>${portfolioForecast.status==='AVAILABLE'?`<div class="portfolio-endpoint-grid extended"><div><span>情景终点 P10</span><strong>¥ ${money(endpoint.p10,0)}</strong><small>${Number(endpointReturns.p10)>=0?'+':''}${(Number(endpointReturns.p10||0)*100).toFixed(1)}%</small></div><div class="median"><span>中心判断 P50</span><strong>¥ ${money(endpoint.p50,0)}</strong><small>${Number(endpointReturns.p50)>=0?'+':''}${(Number(endpointReturns.p50||0)*100).toFixed(1)}%</small></div><div><span>情景终点 P90</span><strong>¥ ${money(endpoint.p90,0)}</strong><small>${Number(endpointReturns.p90)>=0?'+':''}${(Number(endpointReturns.p90||0)*100).toFixed(1)}%</small></div><div><span>${Number(calibratedProbabilities.trading_day||validatedDays)}日校准盈利概率</span><strong>${calibratedProbabilities.profit==null?'—':(Number(calibratedProbabilities.profit)*100).toFixed(1)+'%'}</strong><small>${Number(calibratedProbabilities.sample_count||0)} 次相关情景</small></div><div><span>达到 ${Number(request.target_return_pct||0).toFixed(1)}% 目标</span><strong>${endpointProbabilities.target==null?'样本不足':(Number(endpointProbabilities.target)*100).toFixed(1)+'%'}</strong><small>${endpointProbabilities.target==null?safe(endpointProbabilities.reason||'独立历史不足以估计'): '用户期限的同批情景'}</small></div></div>`:''}${portfolioFutureCurveSvg(portfolioForecast,request.target_return_pct)}</section>
    <details class="professional-disclosure quant-professional-disclosure stock-forecast-disclosure"><summary>展开查看每只股票校准区间 <span>${allocations.length} 只建议股票</span></summary><div class="professional-disclosure-body"><section class="quant-timeframe-forecasts">${forecasts||'<div class="empty-state">当前没有建议股票的单股校准区间</div>'}</section></div></details>
    <details class="professional-disclosure quant-professional-disclosure"><summary>专业数据与计算依据 <span>${watchlist.length} 只已预测候选 · ${ranking.length} 项因子排序</span></summary><div class="professional-disclosure-body">${forecastDetails}<section><div class="subsection-head"><span>缓存因子排序</span><small>综合走势、财务、成交活跃度和稳定性；概率因子暂不参与</small></div><div class="table-wrap"><table class="quant-ranking-table"><thead><tr><th>#</th><th>股票</th><th>最近收盘</th><th>阶段涨跌</th><th>均线趋势</th><th>财务评分</th><th>年化波动</th><th>综合分</th><th>结论 / 原因</th></tr></thead><tbody>${rows}</tbody></table></div></section></div></details>
    <div class="quant-grid quant-discipline-grid"><div><div class="subsection-head"><span>你设置的纪律</span><small>用于风险复核；成本止损与卖出数量需接入真实持仓后计算</small></div><div class="quant-rules"><span>成本下跌 ${Number(rules.stop_loss_pct||0).toFixed(1)}% 复核止损</span><span>${rules.take_profit_mode==='fixed'?'上涨':'开始浮动止盈'} ${Number(rules.take_profit_pct||0).toFixed(1)}%</span><span>盈利后回落 ${Number(rules.trailing_stop_pct||0).toFixed(1)}% 复核止盈</span><span>每 ${rules.rebalance_days||'—'} 个交易日重新评估</span></div></div></div>
    <p class="lab-warning">建议金额与股数只回答“在当前输入下如何做研究分配”，不代表你已经持有或已经成交。通过门禁的单股区间按时间顺序做滚动样本外检验；未通过门禁的期限仍提供明确标注的历史基准情景，但不参与方向判断。组合区间用同期历史相关性合成，尚未单独回测；系统只给人工复核建议，不自动下单。</p></section>`;
}

function quantResultHtml(result){
  const quant=result.quant_portfolio||{},rec=result.recommendation||quant.recommendation||{},exp=rec.expectation||{},metrics=rec.holdout_metrics||{},positions=rec.positions||[],rules=rec.rules||{},requestData=quant.request||{},maxPositions=Math.max(1,Number(requestData.max_positions||8)),trials=(rec.research_recommendations||[]).slice(0,maxPositions),inference=quant.inference||result.inference||{},ruleSnapshot=inference.kind==='CACHED_RULE_SNAPSHOT',snapshot=inference.kind==='LATEST_TRADING_DAY_SNAPSHOT';
  const credibility=rec.credibility||{},modelAudit=credibility.model||{},ranges=credibility.data_ranges||{},hitRates=modelAudit.top_k_hit_rates||{},walkForward=credibility.walk_forward_history||[],change=quant.change_summary||{};
  const formalSymbols=new Set(positions.map(item=>String(item.symbol||'')));
  const ranking=[...(rec.candidate_ranking||[])].sort((a,b)=>(formalSymbols.has(String(a.symbol||''))?0:a.research_recommended?1:a.eligible?2:3)-(formalSymbols.has(String(b.symbol||''))?0:b.research_recommended?1:b.eligible?2:3)||(Number(b.composite_score??-999)-Number(a.composite_score??-999))||String(a.symbol||'').localeCompare(String(b.symbol||''))).slice(0,8);
  if(!quant.run_key)return'';
  if(ruleSnapshot)return quantRuleSnapshotHtml(quant,rec);
  const targetReturn=Number(rec.target_return??(Number(requestData.target_return_pct||0)/100)),horizonMonths=Number(requestData.horizon_months||Math.round(Number(exp.horizon_trading_days||0)/21)||0),decisionStatus=rec.decision_status||(metrics.risk_pass?(Number(exp.p50||0)>=targetReturn?'TARGET_MET':'CLOSEST_FEASIBLE'):'NO_RISK_FEASIBLE'),rawDecisionLabel=rec.decision_label||({TARGET_MET:'历史结果达到目标，且历史最大跌幅符合要求',CLOSEST_FEASIBLE:'未达到目标，采用历史最大跌幅符合要求且收益最接近的方案',RESEARCH_TARGET_MET:'历史参考收益达到目标，但稳定性检查没有全部通过，仅供重点关注',RESEARCH_CLOSEST:'历史最大跌幅符合要求，但稳定性检查没有全部通过，仅供重点关注',NO_RISK_FEASIBLE:'没有方案同时满足历史最大跌幅限制，仅展示最接近的股票'}[decisionStatus]||'推荐结果已生成'),decisionLabel=plainQuantText(rawDecisionLabel),targetGap=Number(rec.target_gap??(Number(exp.p50||0)-targetReturn));
  const decisionHeading=({TARGET_MET:'历史结果达到目标',CLOSEST_FEASIBLE:'最接近目标',RESEARCH_TARGET_MET:'历史参考收益达标',RESEARCH_CLOSEST:'仅供重点关注',NO_RISK_FEASIBLE:'历史最大跌幅超出限制'}[decisionStatus]||'推荐结果');
  const formalAllocation=positions.map(item=>{const amount=Number(item.amount??(Number(item.shares||0)*Number(item.reference_price||0)));return`<div class="quant-allocation-row"><div><strong>${safe(item.name)} <small>${safe(item.symbol)}</small></strong><span>建议投入 ¥ ${money(amount,2)} · 参考数量 ${item.shares||0} 股 · ${snapshot?'当前数据估算上涨概率':'系统估算上涨概率'} ${item.probability_up===null||item.probability_up===undefined?'—':(Number(item.probability_up)*100).toFixed(1)+'%'}</span></div><div class="quant-weight"><div><i style="width:${Math.max(0,Math.min(100,Number(item.weight||0)*100))}%"></i></div><b>${(Number(item.weight||0)*100).toFixed(1)}%</b></div></div>`;}).join('');
  const trialAllocation=trials.map(item=>`<div class="quant-allocation-row trial"><div><strong>${safe(item.name)} <small>${safe(item.symbol)}</small></strong><span>参考数量 ${item.shares||0} 股 · 参考价 ${money(item.reference_price||0)} · 止损参考价 ${money(item.stop_price||0)}</span><small>已满足：${safe((item.passed_pillars||[]).map(plainQuantPillar).join('、')||'暂无')}；仍需确认：${safe((item.failed_pillars||[]).map(plainQuantPillar).join('、')||'无')}</small></div><div class="quant-weight"><div><i style="width:${Math.max(0,Math.min(100,Number(item.weight||0)*100))}%"></i></div><b>${(Number(item.weight||0)*100).toFixed(1)}%</b></div></div>`).join('');
  const allocation=positions.length?formalAllocation:(trials.length?`<div class="trial-note"><strong>可以重点关注 · 尚未达到正式推荐条件</strong><span>财务情况、近期走势和上涨概率这 3 项中至少满足 2 项；上涨概率最多比正式标准低 3 个百分点，并确认本金可以买整手股票。</span></div>${trialAllocation}`:'<div class="empty-state">当前没有达到重点关注最低条件的股票</div>');
  const rankingRows=ranking.map((item,index)=>{const isFormal=formalSymbols.has(String(item.symbol||'')),status=isFormal?(snapshot?'当前数据推荐，待盘后验证':'正式推荐'):item.research_recommended?'重点关注':item.eligible?'条件通过，未入选':'暂不推荐',statusClass=isFormal?'rank-pass':item.research_recommended?'rank-trial':item.eligible?'rank-backup':'rank-watch',rowClass=isFormal?'formal-selected':item.research_recommended?'research-selected':'',reasons=(item.rejection_reasons||[]).map(plainQuantReason),detail=item.research_recommended?`3 项条件中通过 ${item.research_factor_passes||0} 项；${reasons.join('；')||'等待满足全部正式推荐条件'}`:item.eligible?(rec.publish_gate_passed?'受到最多持有数量限制，本次没有入选':'单只股票条件通过，但整体方案尚未达到正式推荐要求'):(reasons.join('；')||'没有通过当前筛选条件'),profile=item.feature_profile||{},categories=(profile.categories||[]).map(category=>`<li><b>${safe(category.plain_label)}</b><span>${category.status==='AVAILABLE'?'有数据':'暂无数据'} · ${safe(category.source||'—')} · ${safe(category.asof||'日期未知')}</span></li>`).join('');return`<tr class="${rowClass}"><td>${index+1}</td><td><strong>${safe(item.name)}</strong><small>${safe(item.symbol)}</small></td><td>${item.probability_up===null||item.probability_up===undefined?'—':(Number(item.probability_up)*100).toFixed(1)+'%'}</td><td>${item.fundamental_score===null||item.fundamental_score===undefined?'—':Number(item.fundamental_score).toFixed(2)}</td><td>${(Number(item.fundamental_coverage||0)*100).toFixed(0)}%</td><td>${(Number(item.momentum||0)*100).toFixed(1)}%</td><td>${Number(item.composite_score??0).toFixed(3)}</td><td><span class="${statusClass}">${status}</span>${isFormal&&snapshot?'<small>排序已使用你的设置；逐股未来区间另行执行时间外校准，组合策略仍待盘后回测。</small>':isFormal?'':`<small>${safe(detail)}</small>`}${categories?`<details class="stock-profile"><summary>查看这只股票用了哪些资料</summary><ul>${categories}</ul><p>${safe(profile.missing_rule||'')}</p></details>`:''}</td></tr>`;}).join('');
  const plainRanking=ranking.slice(0,5).map(item=>{const isFormal=formalSymbols.has(String(item.symbol||'')),status=isFormal?'达到当前正式条件':item.research_recommended?'值得继续关注':item.eligible?'条件合格但本次未入选':'暂不推荐',reasons=(item.rejection_reasons||[]).map(plainQuantReason),reason=isFormal?'关键条件已通过，仍需人工复核':item.research_recommended?`${item.research_factor_passes||0}/3 项主要条件通过`:(reasons[0]||'当前资料或条件还不够');return `<article class="quant-plain-stock ${isFormal?'ready':item.research_recommended?'watch':'hold'}"><header><div><strong>${safe(item.name)}</strong><small>${safe(item.symbol)}</small></div><span>${safe(status)}</span></header><p>${safe(reason)}</p><footer><span>财务资料 ${(Number(item.fundamental_coverage||0)*100).toFixed(0)}%</span><span>近期涨跌 ${(Number(item.momentum||0)*100).toFixed(1)}%</span></footer></article>`;}).join('');
  const hitRateCards=[['1d','1个交易日后'],['5d','5个交易日后'],['20d','20个交易日后']].map(([key,label])=>{const row=hitRates[key]||{},interval=row.confidence_interval||{};return`<div><span>${label}上涨命中率</span><strong>${row.hit_rate===null||row.hit_rate===undefined?'—':(Number(row.hit_rate)*100).toFixed(1)+'%'}</strong><small>${row.sample_count||0} 个入选样本${interval.low===undefined?'':` · 95%区间 ${(Number(interval.low)*100).toFixed(1)}%–${(Number(interval.high)*100).toFixed(1)}%`}</small></div>`;}).join('');
  const stockAudit=(credibility.stocks||[]).map(item=>`<tr><td>${safe(item.name||item.symbol)}</td><td>${safe(item.symbol)}</td><td>${safe(item.included===false?'未纳入':'已纳入')}</td><td>${safe(item.reason||`${item.history?.rows||'—'}个交易日`)}</td></tr>`).join('');
  const currentCandidateAudit=(credibility.current_candidate_stocks||[]).map(item=>`<tr><td>${safe(item.name||item.symbol)}</td><td>${safe(item.symbol)}</td><td>等待盘后重跑</td><td>${safe(item.history?.rows?`${item.history.rows}个交易日，${item.history.data_start||'—'}至${item.history.data_end||'—'}`:'当前排序使用最近交易日数据')}</td></tr>`).join('');
  const validationWindows=(ranges.validation_windows||[]).map((item,index)=>`第${index+1}个半年 ${safe(item.start)} 至 ${safe(item.end)}`).join('；');
  const walkForwardRows=walkForward.map(item=>{const m=item.oos_metrics||{},final=item.role==='FINAL_UNTOUCHED_HOLDOUT';return`<tr><td>${item.sequence||'—'}</td><td>${final?'最终独立检查':'半年样本外检验'}</td><td>${safe(item.formation_start||'—')} 至 ${safe(item.formation_end||'—')}</td><td>${safe(item.evaluation_start||'—')} 至 ${safe(item.evaluation_end||'—')}</td><td>第 ${item.selected_iteration||'—'} 套${item.parameter_updated?' · 本期已换参数':' · 沿用/初始参数'}</td><td>${m.total_return===undefined?'—':`${Number(m.total_return)>=0?'+':''}${(Number(m.total_return)*100).toFixed(1)}%`}</td><td>${m.max_drawdown===undefined?'—':(Math.abs(Number(m.max_drawdown))*100).toFixed(1)+'%'}</td><td>${m.risk_pass?'通过':'未通过'}</td></tr>`;}).join('');
  const changeRows=(change.ranking_changes||[]).slice(0,5).map(item=>`<li>${safe(item.name||item.symbol)}：第 ${item.from} 位 → 第 ${item.to} 位</li>`).join('');
  const accuracyBreakdown=[...(modelAudit.per_sector||[]).map(item=>({...item,type:'板块'})),...(modelAudit.per_market_regime||[]).map(item=>({...item,type:'行情阶段'})),...(modelAudit.per_stock||[]).map(item=>({...item,type:'股票'}))].map(item=>`<tr><td>${safe(item.type)}</td><td>${safe(item.name||item.symbol)}</td><td>${item.directional_accuracy===null||item.directional_accuracy===undefined?'—':(Number(item.directional_accuracy)*100).toFixed(1)+'%'}</td><td>${item.sample_count||0}</td></tr>`).join('');
  const forecastCandidates=(rec.forecast_allocations||[]).length?rec.forecast_allocations:(positions.length?positions:(trials.length?trials:(rec.research_watchlist||[]))),forecastAllocations=forecastCandidates.slice(0,maxPositions),portfolioForecast=rec.portfolio_forecast||{},capital=Number(portfolioForecast.capital||requestData.capital||0),endpoint=portfolioForecast.endpoint||{},endpointReturns=portfolioForecast.endpoint_returns||{},hasPortfolioForecast=portfolioForecast.status==='AVAILABLE',requestedForecastDays=Number(portfolioForecast.requested_horizon_trading_days||Math.round(horizonMonths*21)),scenarioForecastDays=Number(portfolioForecast.horizon_trading_days||0),validatedForecastDays=Number(portfolioForecast.validated_horizon_trading_days??scenarioForecastDays),hasBaselineForecast=(portfolioForecast.baseline_allocations||[]).length>0,portfolioCoverageText=portfolioForecast.horizon_status==='FULL'?`严格校准覆盖全部 ${validatedForecastDays} 个交易日`:hasBaselineForecast?`情景覆盖 ${scenarioForecastDays} / ${requestedForecastDays} 日 · 共同严格校准 ${validatedForecastDays} 日`:`请求 ${requestedForecastDays} 个交易日，当前只通过 ${validatedForecastDays} 个交易日`,individualForecasts=forecastAllocations.map(quantTimeframeForecastHtml).join(''),forecastDetails=forecastAllocations.filter(item=>item.timeframe_forecast).map(quantTimeframeForecastDetailHtml).join('');
  const portfolioPrediction=`<section class="portfolio-forecast-section"><div class="subsection-head"><span>${hasPortfolioForecast?'组合相关情景区间':'组合校准证据不足'}</span><small>以 ¥${money(capital,0)} 本金为起点 · ${hasPortfolioForecast?safe(portfolioCoverageText):'不回退到旧的长期模板线'}</small></div>${hasPortfolioForecast?`<div class="portfolio-endpoint-grid"><div><span>情景终点 P10</span><strong>¥ ${money(endpoint.p10,0)}</strong><small>${Number(endpointReturns.p10)>=0?'+':''}${(Number(endpointReturns.p10||0)*100).toFixed(1)}%</small></div><div class="median"><span>情景终点 P50</span><strong>¥ ${money(endpoint.p50,0)}</strong><small>${Number(endpointReturns.p50)>=0?'+':''}${(Number(endpointReturns.p50||0)*100).toFixed(1)}%</small></div><div><span>情景终点 P90</span><strong>¥ ${money(endpoint.p90,0)}</strong><small>${Number(endpointReturns.p90)>=0?'+':''}${(Number(endpointReturns.p90||0)*100).toFixed(1)}%</small></div></div>`:''}${portfolioFutureCurveSvg(portfolioForecast,requestData.target_return_pct)}</section>`;
  const stockPredictions=`<details class="professional-disclosure quant-professional-disclosure stock-forecast-disclosure"><summary>展开查看每只股票校准区间 <span>${forecastAllocations.length} 只股票</span></summary><div class="professional-disclosure-body"><section class="quant-timeframe-forecasts">${individualForecasts||'<div class="empty-state">当前结果还没有逐股校准区间</div>'}</section></div></details>`;
  const forecastEvidence=`<details class="professional-disclosure quant-professional-disclosure"><summary>专业数据与计算依据 <span>${forecastAllocations.length} 只股票</span></summary><div class="professional-disclosure-body">${forecastDetails||'<div class="empty-state">当前结果还没有逐股周期统计明细</div>'}</div></details>`;
  return `<section class="quant-result"><div class="quant-result-head"><div><span class="status-tag"><span class="beginner-only">${safe(plainQuantText(rec.profile_label||rec.profile||'自动选择'))}</span><span class="professional-only">${safe(rec.profile_label||rec.profile||'auto')}</span></span><strong>${snapshot?'最近交易日推荐结果':'当前推荐组合'}</strong><small>数据截至 ${safe(rec.data_asof||'—')}<span class="professional-only"> · 评估编号 ${safe(rec.model_version||'—')}</span></small></div><b>${safe(quantVersionLabel(quant.version?.status||result.version?.status))}</b></div>
    <div class="quant-decision-callout ${safe(decisionStatus.toLowerCase())}"><span>${safe(decisionHeading)}</span><strong><span class="beginner-only">${safe(decisionLabel)}</span><span class="professional-only">${safe(rawDecisionLabel)}</span></strong><small>目标 ${(targetReturn*100).toFixed(1)}% · 中位参考收益 ${(Number(exp.p50||0)*100).toFixed(1)}% · 与目标相差 ${targetGap>=0?'+':''}${(targetGap*100).toFixed(1)}%</small></div>
    <div class="quant-plain-metrics beginner-only"><div><span>历史中位参考</span><strong>${(Number(exp.p50||0)*100).toFixed(1)}%</strong></div><div><span>模拟出现亏损</span><strong>${(Number(exp.probability_loss||0)*100).toFixed(1)}%</strong></div><div><span>历史最大跌幅</span><strong>${(Math.abs(Number(metrics.max_drawdown||0))*100).toFixed(1)}%</strong></div></div>
    <div class="quant-metrics professional-only"><div><span>未来 ${horizonMonths||'—'} 个月模拟收益范围（较差 / 中位 / 较好）</span><strong>${(Number(exp.p10||0)*100).toFixed(1)}% / ${(Number(exp.p50||0)*100).toFixed(1)}% / ${(Number(exp.p90||0)*100).toFixed(1)}%</strong></div><div><span>模拟中达到目标的比例</span><strong>${(Number(exp.probability_target||0)*100).toFixed(1)}%</strong></div><div><span>模拟中出现亏损的比例</span><strong>${(Number(exp.probability_loss||0)*100).toFixed(1)}%</strong></div><div><span>历史最大跌幅${snapshot?'（相近参考策略）':''}</span><strong>${(Math.abs(Number(metrics.max_drawdown||0))*100).toFixed(1)}%</strong></div></div>
    <div class="quant-grid quant-discipline-grid"><div><div class="subsection-head"><span>${positions.length?(snapshot?'当前数据推荐股票（待盘后验证）':'正式推荐股票'):trials.length?'重点关注股票':'推荐股票'}</span><small>按参考比例计算的剩余现金 ${(Number(positions.length?rec.cash_weight:rec.research_cash_weight||1)*100).toFixed(1)}%</small></div><div class="quant-allocation">${allocation}</div><div class="quant-rules"><span>下跌 ${Number(rules.stop_loss_pct||0).toFixed(1)}% 止损</span><span>${rules.take_profit_mode==='fixed'?'上涨':'开始浮动止盈'} ${Number(rules.take_profit_pct||0).toFixed(1)}%</span><span>盈利后回落 ${Number(rules.trailing_stop_pct||0).toFixed(1)}% 止盈</span><span>每 ${rules.rebalance_days||'—'} 个交易日重新评估</span></div></div></div>
    ${portfolioPrediction}${stockPredictions}${forecastEvidence}
    <section class="quant-plain-ranking beginner-only"><div class="subsection-head"><span>股票结论</span><small>只展示主要原因，不把缺失资料算成 0 分</small></div>${plainRanking||'<div class="empty-state">当前没有可比较的股票</div>'}</section>
    <section class="professional-only"><div class="subsection-head"><span>股票筛选结果</span><small>推荐股票置顶；综合分已使用你保存的五类关注重点</small></div><div class="table-wrap"><table class="quant-ranking-table"><thead><tr><th>#</th><th>股票</th><th>估算上涨概率</th><th>财务评分</th><th>数据完整度</th><th>近期涨跌</th><th>综合分</th><th>是否推荐 / 原因</th></tr></thead><tbody>${rankingRows}</tbody></table></div></section>
    <section class="credibility-panel professional-only"><div class="subsection-head"><span>这套推荐历史上准不准</span><small>${safe(credibility.validation_status||'等待盘后验证')}</small></div><div class="quant-metrics credibility-metrics"><div><span>1日方向准确率</span><strong>${modelAudit.directional_accuracy===null||modelAudit.directional_accuracy===undefined?'—':(Number(modelAudit.directional_accuracy)*100).toFixed(1)+'%'}</strong><small>${modelAudit.sample_count||0} 个预测样本</small></div><div><span>AUC 排序能力</span><strong>${modelAudit.auc===null||modelAudit.auc===undefined?'—':Number(modelAudit.auc).toFixed(3)}</strong><small>0.5约等于随机，越高越好</small></div><div><span>Brier 概率误差</span><strong>${modelAudit.brier_score===null||modelAudit.brier_score===undefined?'—':Number(modelAudit.brier_score).toFixed(3)}</strong><small>越低越好</small></div><div><span>策略胜率</span><strong>${metrics.win_rate===null||metrics.win_rate===undefined?'—':(Number(metrics.win_rate)*100).toFixed(1)+'%'}</strong><small>${metrics.closed_trades||0} 笔已结束模拟交易</small></div><div><span>相对等权股票池多赚/少赚</span><strong>${metrics.excess_return===null||metrics.excess_return===undefined?'—':`${Number(metrics.excess_return)>=0?'+':''}${(Number(metrics.excess_return)*100).toFixed(1)}%`}</strong><small>策略净收益减去同股票池等权买入持有</small></div><div><span>平均赚亏比</span><strong>${metrics.profit_loss_ratio===null||metrics.profit_loss_ratio===undefined?'—':Number(metrics.profit_loss_ratio).toFixed(2)}</strong><small>平均盈利金额 ÷ 平均亏损金额</small></div>${hitRateCards}</div>${accuracyBreakdown?`<details class="audit-details"><summary>按股票、板块和行情阶段看准确率</summary><div class="table-wrap"><table><thead><tr><th>分类</th><th>名称</th><th>方向准确率</th><th>样本数</th></tr></thead><tbody>${accuracyBreakdown}</tbody></table></div></details>`:''}
    <details class="audit-details" open><summary>回测用了哪些股票、哪些日期</summary><div class="audit-range"><p><b>${snapshot?'参考策略可用数据':'可用数据'}：</b>${safe(ranges.start||ranges.model_training_start||'—')} 至 ${safe(ranges.end||ranges.model_training_end||'—')}，${ranges.rows||modelAudit.sample_count||'—'} 个交易日/预测样本</p><p><b>最初形成参数：</b>${safe(ranges.training_range?.start||'—')} 至 ${safe(ranges.training_range?.end||'—')}</p><p><b>半年递推检验：</b>${validationWindows||'当前是旧参考策略快照，等待本股票池按半年递推重跑'}</p><p><b>最终独立检查：</b>${safe(ranges.final_holdout?.start||ranges.reference_holdout?.start||'—')} 至 ${safe(ranges.final_holdout?.end||ranges.reference_holdout?.end||'—')}</p><p><b>规则：</b>每半年开始前只使用当时已经结束的数据选参数；本期结果只能用于下一期更新，不能倒回去修改本期成绩。</p><p><b>成交假设：</b>已计佣金、最低佣金、印花税、滑点、整手、成交量限制、涨跌停近似和 T+1；历史结果不代表未来。</p></div>${walkForwardRows?`<div class="subsection-head"><span>每半年怎样递推</span><small>最后一期在选完参数后才打开</small></div><div class="table-wrap"><table><thead><tr><th>期数</th><th>用途</th><th>选参数时可见的数据</th><th>本期检验数据</th><th>采用参数</th><th>收益</th><th>最大跌幅</th><th>风控</th></tr></thead><tbody>${walkForwardRows}</tbody></table></div>`:''}<div class="table-wrap"><table><thead><tr><th>${snapshot?'参考策略实际回测股票':'股票'}</th><th>代码</th><th>是否纳入</th><th>原因 / 历史覆盖</th></tr></thead><tbody>${stockAudit||'<tr><td colspan="4">旧参考版本没有保存逐股清单，等待盘后新版本补齐</td></tr>'}</tbody></table></div>${snapshot&&currentCandidateAudit?`<div class="subsection-head"><span>当前排序股票</span><small>不冒充参考回测样本</small></div><div class="table-wrap"><table><thead><tr><th>股票</th><th>代码</th><th>验证状态</th><th>当前历史覆盖</th></tr></thead><tbody>${currentCandidateAudit}</tbody></table></div>`:''}</details>
    <details class="audit-details"><summary>和上一个交易日相比为什么变了</summary><p>${safe(change.plain_reason||'首次参数版本暂无上一日可比较结果；盘后正式版本产生后会显示持仓、排名和参数变化。')}</p>${changeRows?`<ul>${changeRows}</ul>`:'<p>暂无排名变化记录。</p>'}</details></section>
    <p class="lab-warning">${snapshot?'股票排序使用最近交易日已完成评估的数据；逐股未来期限只保留通过滚动样本外门禁的部分，资金分配组合仍待独立回测。':'未来概率区间使用未参与调参的历史数据，按连续 5 个交易日分组反复抽样计算。'}这些结果不代表未来收益；系统不连接券商、不自动下单。</p></section>`;
}

function harnessResultHtml(detail){
  const result=detail?.run?.result||{};
  if(!result.summary)return '<div class="empty-state">运行完成后在这里显示结构化结果</div>';
  const facts=(result.facts||[]).map(item=>`<div class="agent-finding"><span>${safe(item.label)}</span><p>${safe(item.text)}</p></div>`).join('');
  const opinion=result.opinion?`<div class="agent-finding opinion"><span>${safe(result.opinion.label)}</span><p>${safe(result.opinion.text)}</p></div>`:'';
  const hypotheses=(result.hypotheses||[]).map(item=>`<div class="agent-finding hypothesis"><span>${safe(item.label)}</span><p>${safe(item.text)}</p></div>`).join('');
  const stocks=detail.run.input?.stocks||[];
  const compareLink=result.artifacts?.comparison&&stocks.length?`<a class="secondary-button agent-result-link" href="/?view=compare&stocks=${encodeURIComponent(stocks.join(','))}&profile=${encodeURIComponent(detail.run.input.profile||'balanced')}">打开对比决策台</a>`:'';
  const strategies=result.strategies?.length?`<div class="strategy-result-table">${strategyResultRows(result.strategies)}</div>`:'';
  const quant=quantResultHtml(result);
  return `<div class="agent-result-head"><span class="status-tag">结构化结果</span><strong>${safe(result.summary)}</strong>${compareLink}</div>${facts}${opinion}${hypotheses}${quant}${strategies}`;
}

function harnessRunDetailHtml(detail){
  if(!detail)return '<section class="panel agent-run-detail"><div class="empty-state">选择一次运行查看上下文、工具和事件</div></section>';
  const run=detail.run,progress=`${Math.min(run.current_step,run.plan.length)}/${run.plan.length}`;
  const events=detail.events.map(item=>`<div class="agent-event ${safe(item.level.toLowerCase())}"><i></i><div><strong>${safe(item.message)}</strong><small>${safe(localTimestamp(item.created_at))} · ${safe(item.event_type)}</small></div></div>`).join('');
  const tools=detail.tool_calls.map(item=>`<div class="agent-tool-row"><div><span>${safe(item.risk_level)}</span><strong>${safe(item.tool_name)}</strong></div><small>尝试 ${item.attempts} 次</small><b class="status-tag">${safe(harnessStatus(item.status))}</b></div>`).join('');
  const pending=detail.waiting_approval;
  const approval=pending?`<div class="agent-approval"><div><span>CONSEQUENTIAL_WRITE</span><strong>${safe(pending.summary)}</strong><small>${safe(JSON.stringify(pending.request.arguments||{}))}</small></div><div class="agent-approval-actions"><button class="secondary-button harness-approval-decision" data-key="${safe(pending.approval_key)}" data-approved="false">拒绝</button><button class="primary-button harness-approval-decision" data-key="${safe(pending.approval_key)}" data-approved="true">批准并继续</button></div></div>`:'';
  const resume=detail.can_resume?'<button id="harnessResumeRun" class="secondary-button">从检查点续跑</button>':'';
  const cancel=!['COMPLETED','CANCELLED'].includes(run.status)?'<button id="harnessCancelRun" class="secondary-button danger-button">取消运行</button>':'';
  return `<section class="panel agent-run-detail"><div class="panel-head"><div><p class="eyebrow">${safe(detail.thread.thread_key)}</p><h2>${safe(run.display_title||detail.thread.title)}</h2></div><div class="agent-run-actions"><span class="status-tag">${safe(harnessStatus(run.status))} · ${progress}</span>${resume}${cancel}</div></div>
    <div class="agent-context-strip"><span>配置 <b>${safe(run.context.active_config_version||'baseline')}</b></span><span>规划器 <b>${safe(run.context.reasoning_engine?.provider||'domain')}</b></span><span>工具 <b>${run.context.available_tools?.length||0} 个受限调用</b></span><span>交易 <b>禁用</b></span></div>${approval}
    <div class="agent-inspector"><div><div class="subsection-head"><span>执行事件</span><small>${detail.events.length} 条</small></div><div class="agent-timeline">${events||'<div class="empty-state">尚无事件</div>'}</div></div><div><div class="subsection-head"><span>工具调用</span><small>白名单</small></div><div class="agent-tools">${tools||'<div class="empty-state">尚未调用工具</div>'}</div></div></div>
    <div class="subsection-head result-head"><span>运行结果</span><small>事实 / 模型观点 / 推翻条件</small></div><div class="agent-result">${harnessResultHtml(detail)}</div></section>`;
}

function quantDecisionInput(){
  const input={...state.quantDraft,take_profit_mode:'trailing'};
  const maxDrawdown=Math.max(1,Math.min(80,Number(input.max_drawdown_pct)||15));
  const adjusted=[];
  input.max_drawdown_pct=maxDrawdown;
  [['stop_loss_pct','止损'],['trailing_stop_pct','移动止盈回撤']].forEach(([key,label])=>{
    const value=Math.max(1,Number(input[key])||1),bounded=Math.min(value,maxDrawdown);
    if(bounded!==Number(state.quantDraft[key]))adjusted.push(label);
    input[key]=bounded;
  });
  if(maxDrawdown!==Number(state.quantDraft.max_drawdown_pct)||adjusted.length){
    Object.assign(state.quantDraft,{max_drawdown_pct:maxDrawdown,stop_loss_pct:input.stop_loss_pct,trailing_stop_pct:input.trailing_stop_pct});
    persistQuantDraft();
    if(adjusted.length)showToast(`已将${adjusted.join('和')}收紧到最大回撤 ${maxDrawdown}% 以内`);
  }
  return input;
}

function quantDecisionOutputHtml(decision){
  if(!decision||decision.status==='UNREGISTERED')return '<div class="decision-empty"><strong>还没有保存投资条件</strong><span>保存后会显示最近一个交易日计算出的推荐结果。</span></div>';
  if(decision.status==='PENDING_FIRST_POST_CLOSE')return `<div class="decision-empty"><strong>当前数据还不够，无法即时计算</strong><span>${safe(plainQuantText(decision.snapshot_error||'当前缓存缺少足够的候选股票日线；补齐数据后可重新点击查看。'))}</span></div>`;
  if(decision.status!=='AVAILABLE'||!decision.result)return `<div class="decision-empty"><strong>暂时没有可展示的推荐结果</strong><span>${safe(plainQuantText(decision.summary||'等待盘后更新完成'))}</span></div>`;
  const version=decision.version||{},ruleSnapshot=version.status==='RULE_SNAPSHOT',snapshot=version.status==='SNAPSHOT',status=ruleSnapshot?'即时预测结果':decision.update_pending?(snapshot?'后台正在重新计算，先显示最近交易日结果':'后台正在更新，先显示上一版结果'):(snapshot?'最近交易日结果':'最新正式结果');
  return `<div class="decision-summary"><span class="status-tag">${safe(status)}</span><strong><span class="beginner-only">${safe(plainQuantText(decision.summary))}</span><span class="professional-only">${safe(decision.summary)}</span></strong><small>数据截至 ${safe(decision.data_asof||'—')}<span class="professional-only"> · 结果编号 ${safe(version.version_key||'—')} · 生成于 ${safe(localTimestamp(version.activated_at||version.created_at))}</span></small></div>${quantResultHtml({quant_portfolio:decision.result})}`;
}

function quantDecisionPanelHtml(_detail,learning,_quantVersions,sectorCache){
  const decision=state.quantDecision,d=state.quantDraft,cycle=learning?.last_cycle,metrics=cycle?.metrics||{},version=decision?.version;
  const dataState=decision?.data_asof?`评估数据更新至 ${safe(decision.data_asof)}`:'等待第一次盘后更新';
  const ruleSnapshot=version?.status==='RULE_SNAPSHOT',snapshot=version?.status==='SNAPSHOT',freshness=ruleSnapshot?'当前显示即时K线预测，正式模型与回测待验证':decision?.update_pending?(snapshot?'后台正在重新计算，当前先用最近交易日结果':'后台正在计算新结果，当前先用上一版'):snapshot?'今天不开盘或尚未完成更新：使用最近一个交易日的评估结果':cycle&&metrics.market_date_complete===false?'盘后数据还没到齐，稍后自动重试':decision?.status==='AVAILABLE'?'当前显示最近一次正式结果':'等待每日盘后更新';
  const cache=sectorCache||{},cacheTotal=Number(cache.total_symbols||0),cacheMarket=Number(cache.market_cached||0),cacheModel=Number(cache.model_ready||0),cacheFundamental=Number(cache.fundamentals_cached||0),cacheProgress=cacheTotal?Math.max(0,Math.min(100,Number(cache.market_percent||0))):0;
  return `<section class="panel decision-entry"><div class="panel-head"><div><p class="eyebrow">投资条件</p><h2>填写条件，查看股票推荐</h2></div><div class="daily-decision-state"><span>${dataState}</span><strong>${safe(freshness)}</strong><small>${version?`结果编号 ${safe(version.version_key)}`:'还没有可用结果'}</small></div></div>
    <div class="sector-cache-strip professional-only"><div><strong>四个板块的历史数据</strong><span>${safe(harnessStatus(cache.status||'IDLE'))} · 数据截至 ${safe(cache.target_asof||'—')}</span></div><div class="sector-cache-counts"><span>股票总数 <b>${cacheTotal}</b></span><span>日线数据已保存 <b>${cacheMarket}</b></span><span>可用于评估 <b>${cacheModel}</b></span><span>财务数据已保存 <b>${cacheFundamental}</b></span></div><div class="sector-cache-track"><i style="width:${cacheProgress}%"></i></div><small>行情会尽可能缓存并至少保留最近 1 年；当前选择近 ${Number(d.backtest_window_years||3)} 年回测，需要至少 ${Math.max(420,Number(d.backtest_window_years||3)*252)} 个交易日且覆盖最近交易日。每天收盘后加入最新一天，并移除窗口外最旧一天。</small></div>
    <div class="decision-form">
      <div class="risk-profile-control"><span>风险偏好</span><div class="risk-profile-segments" role="group" aria-label="风险偏好">
        <button type="button" data-quant-profile="aggressive" class="${d.risk_profile==='aggressive'?'active':''}">激进</button>
        <button type="button" data-quant-profile="balanced" class="${d.risk_profile==='balanced'?'active':''}">中立</button>
        <button type="button" data-quant-profile="conservative" class="${d.risk_profile==='conservative'?'active':''}">保守</button>
      </div></div>
      <label>本金（元）<input data-quant-draft="capital" id="quickCapital" type="number" min="1000" value="${Number(d.capital)}"></label>
      <label>期限（月）<input data-quant-draft="horizon_months" id="quickHorizon" type="number" min="1" max="120" value="${Number(d.horizon_months)}"></label>
      <label>希望达到的收益（%）<input data-quant-draft="target_return_pct" id="quickTarget" type="number" min="0" max="300" value="${Number(d.target_return_pct)}"></label>
      <label>可接受的历史最大跌幅（%）<input data-quant-draft="max_drawdown_pct" id="quickDrawdown" type="number" min="1" max="80" value="${Number(d.max_drawdown_pct)}"></label>
      <label><span class="decision-field-label"><span>止损（%）</span><small>上限为最大回撤</small></span><input data-quant-draft="stop_loss_pct" id="quickStopLoss" type="number" min="1" max="${Number(d.max_drawdown_pct)}" value="${Number(d.stop_loss_pct)}"></label>
      <label>开始浮动止盈的涨幅（%）<input data-quant-draft="take_profit_pct" id="quickTakeProfit" type="number" min="1" max="300" value="${Number(d.take_profit_pct)}"></label>
      <label class="decision-sector-field">关注板块<input data-quant-draft="sectors" id="quickSectors" value="${safe(d.sectors)}" placeholder="例如 白酒、新能源"></label>
      <label>最多持有（只）<input data-quant-draft="max_positions" id="quickPositions" type="number" min="1" max="8" value="${Number(d.max_positions)}"></label>
      <label class="professional-only">最多比较多少套参数<input data-quant-draft="max_iterations" id="quickIterations" type="number" min="1" max="20" value="${Number(d.max_iterations)}"><small>每个风险档位最多比较的方案数；越多，盘后计算越久</small></label>
      <button id="quickQuantRun" class="primary-button">按这些条件查看推荐</button>
    </div>
    <details class="decision-options professional-disclosure"><summary>专业参数与更多选项</summary><div>
      <label>指定股票（可留空）<input data-quant-draft="stocks" id="quickStocks" value="${safe(d.stocks)}" placeholder="留空就按板块自动筛选"></label>
      <label>最多筛选股票数<input data-quant-draft="max_candidates" id="quickCandidates" type="number" min="2" max="30" value="${Number(d.max_candidates)}"></label>
      <label>盈利后回落多少时止盈（%）<input data-quant-draft="trailing_stop_pct" id="quickTrailingStop" type="number" min="1" max="${Number(d.max_drawdown_pct)}" value="${Number(d.trailing_stop_pct)}"><small>不能高于最大回撤；旧设置冲突时会自动收紧</small></label>
      <label>每天回看多长历史<select data-quant-draft="backtest_window_years"><option value="1" ${Number(d.backtest_window_years)===1?'selected':''}>近1年</option><option value="3" ${Number(d.backtest_window_years)===3?'selected':''}>近3年（默认）</option><option value="5" ${Number(d.backtest_window_years)===5?'selected':''}>近5年</option></select></label>
      <label>细分选股方法<select data-quant-draft="strategy_style"><option value="auto" ${d.strategy_style==='auto'?'selected':''}>自动比较</option><option value="trend_following" ${d.strategy_style==='trend_following'?'selected':''}>趋势跟随</option><option value="quality_growth" ${d.strategy_style==='quality_growth'?'selected':''}>优质成长</option><option value="garp" ${d.strategy_style==='garp'?'selected':''}>合理价格成长</option><option value="low_volatility" ${d.strategy_style==='low_volatility'?'selected':''}>低波动</option><option value="cashflow_value" ${d.strategy_style==='cashflow_value'?'selected':''}>现金流与估值</option><option value="catalyst_momentum" ${d.strategy_style==='catalyst_momentum'?'selected':''}>催化与动量</option></select></label>
    </div></details></section>
    <section class="panel decision-output"><div class="panel-head"><div><p class="eyebrow">推荐结果</p><h2>本次结论</h2></div><span class="count">研究用途 · 不下单</span></div>${quantDecisionOutputHtml(decision)}</section>`;
}

function learningProgressHtml(cycle){
  if(!cycle)return '<section class="panel learning-progress-panel"><div class="empty-state">尚无后台自进化轮次</div></section>';
  const progress=cycle.progress||{},stages=progress.stages||[],percent=Math.max(0,Math.min(100,Number(progress.percent||0))),heartbeat=progress.heartbeat_age_seconds;
  const rows=stages.map(item=>`<div class="learning-stage ${safe(String(item.status||'PENDING').toLowerCase())}"><i></i><div><strong>${safe(item.label||item.key)}</strong><small>${safe(item.detail||harnessStatus(item.status))}</small></div><span>${safe(harnessStatus(item.status))}</span></div>`).join('');
  const heartbeatText=cycle.status==='RUNNING'?(progress.stalled?'心跳中断':(heartbeat===null||heartbeat===undefined?'等待心跳':`${Number(heartbeat).toFixed(0)} 秒前心跳`)):`完成于 ${safe(localTimestamp(cycle.finished_at||cycle.started_at))}`;
  return `<section class="panel learning-progress-panel"><div class="panel-head"><div><p class="eyebrow">DURABLE BACKGROUND PROGRESS</p><h2>${safe(cycle.phase)} · ${safe(cycle.cycle_date)}</h2></div><div class="learning-progress-state"><strong>${percent.toFixed(0)}%</strong><span class="status-tag">${safe(harnessStatus(progress.status||cycle.status))}</span></div></div><div class="learning-progress-meta"><span>${safe(progress.current_label||'等待轮次')}</span><small>${progress.completed||0} / ${progress.total||0} 阶段 · ${heartbeatText}</small></div><div class="learning-progress-track"><i style="width:${percent}%"></i></div><div class="learning-stage-grid">${rows||'<div class="empty-state">历史轮次没有阶段级进度</div>'}</div>${cycle.errors?.length?`<p class="lab-warning">本轮记录 ${cycle.errors.length} 条警告或错误，生产版本只会在门禁通过后晋级。</p>`:''}</section>`;
}

function renderHarness(data,detail){
  const openStockProfiles=new Set(Array.from(document.querySelectorAll('#harnessView details.stock-profile[open]')).map(details=>details.closest('tr')?.querySelector('td:nth-child(2) small')?.textContent).filter(Boolean));
  const runtime=data.agent_runtime||{},rs=runtime.summary||{},runs=data.agent_runtime?.runs||[],s=data.summary||{},cases=data.bad_cases||[],candidates=data.candidates||[],versions=data.versions||[],latest=(data.evaluations||[])[0],autonomy=data.autonomous_run,strategyState=data.strategy_evolution||{},experiments=strategyState.experiments||[],strategyVersions=strategyState.versions||[],quantState=data.quant_portfolio||{},quantRuns=quantState.runs||[],quantVersions=quantState.versions||[],learning=data.continuous_learning||{},predictionStats=learning.predictions||{},learningEval=learning.last_evaluation||{},candidateMetrics=learningEval.candidate_metrics||{},intraday=data.intraday_evolution||{},intradayActive=intraday.active_version||{},intradayHoldout=intradayActive.metrics?.holdout||intraday.last_run?.metrics?.holdout||{},deep=data.deep_learning||{},deepLast=deep.last_version||{},deepHoldout=deepLast.metrics?.holdout||{},sentimentCoverage=data.sentiment_coverage||{},sentimentSources=sentimentCoverage.sources||[],codeEvolution=data.code_evolution||{},codeGate=codeEvolution.last_evaluation?.gate||{};
  const caseRows=cases.map(item=>`<div class="harness-row"><div><span class="badge">${safe(item.case_type)} · ${safe(item.severity)}</span><strong>#${item.id} ${safe(JSON.stringify(item.input))}</strong><p>期望：${safe(JSON.stringify(item.expected))} · 实际：${safe(JSON.stringify(item.observed))}</p><small>${safe(item.source)} · 出现 ${item.occurrences} 次</small></div><div class="harness-row-actions"><span class="status-tag">${safe(harnessStatus(item.status))}</span>${item.status==='READY'?`<button class="secondary-button harness-generate" data-id="${item.id}">生成候选</button>`:''}</div></div>`).join('');
  const candidateRows=candidates.map(item=>`<div class="harness-row"><div><span class="badge">${safe(item.candidate_type)} · ${safe(item.baseline_version)}</span><strong>#${item.id} ${safe(item.title)}</strong><p>${safe(item.rationale)}</p><small>${safe(JSON.stringify(item.config))}</small></div><div class="harness-row-actions"><span class="status-tag">${safe(harnessStatus(item.status))}</span>${item.status==='DRAFT'?`<button class="secondary-button harness-evaluate" data-id="${item.id}">回归评测</button>`:''}${item.status==='EVALUATED'&&item.activatable?`<button class="primary-button harness-approve" data-id="${item.id}">批准晋级</button>`:''}</div></div>`).join('');
  const versionRows=versions.map(item=>`<div class="harness-row"><div><span class="badge">${safe(item.status)}</span><strong>${safe(item.version_key)}</strong><p>${safe(item.reason)}</p></div><div class="harness-row-actions">${item.status!=='ACTIVE'?`<button class="secondary-button harness-rollback" data-version="${safe(item.version_key)}">回滚至此</button>`:'<span class="status-tag">当前版本</span>'}</div></div>`).join('');
  const autonomyFindings=autonomy&&autonomy.findings&&autonomy.findings.length?autonomy.findings.map(item=>`<div class="harness-row"><div><span class="badge">${safe(item.category)} · ${safe(item.severity)}</span><strong>${safe(item.title)}</strong><small>${safe(JSON.stringify(item.observed))}</small></div><span class="status-tag">已归因</span></div>`).join(''):'<div class="empty-state">尚未发现预测坏案例</div>';
  const runRows=runs.map(item=>`<button class="agent-run-row harness-run-open ${item.run_key===state.harnessRunKey?'active':''}" data-key="${safe(item.run_key)}"><span>${safe(harnessWorkflow(item.workflow))}</span><strong>${safe(item.display_title||item.thread_title)}</strong><small>${safe(harnessStatus(item.status))} · ${item.current_step}/${item.plan.length} · ${safe(localTimestamp(item.updated_at))}</small></button>`).join('');
  const workflows=(runtime.workflows||[]).map(item=>`<option value="${safe(item.key)}" ${item.key===state.harnessWorkflow?'selected':''}>${safe(item.label)}</option>`).join('');
  const rollbackOptions=versions.map(item=>`<option value="${safe(item.version_key)}">${safe(item.version_key)} · ${safe(item.status)}</option>`).join('');
  const experimentOptions=experiments.filter(item=>item.status==='SUCCESS'&&item.activation_eligible).map(item=>`<option value="${safe(item.experiment_key)}">${safe(item.name)} · 可激活 · ${safe(localTimestamp(item.finished_at))}</option>`).join('')||'<option value="">暂无通过全部门禁的实验</option>';
  const experimentRows=experiments.map(item=>`<div class="harness-row"><div><span class="badge">${safe(item.status)} · ${item.iteration_count||0} 次候选测试</span><strong>${safe(item.name)} · ${safe(item.experiment_key)}</strong><p>${(item.universe||[]).map(stock=>safe(stock.name||stock.symbol)).join('、')}</p><small>数据截至 ${safe(item.data_snapshot_at||'—')} · ${item.activation_eligible?'全部门禁已通过':'不可激活'}</small></div><div class="harness-row-actions"><span class="status-tag">${item.activation_eligible?'待人工激活':(item.status==='SUCCESS'?'门禁未通过':safe(harnessStatus(item.status)))}</span></div></div>`).join('');
  const strategyVersionRows=strategyVersions.map(item=>`<div class="harness-row"><div><span class="badge">${safe(item.status)}</span><strong>${safe(item.version_key)}</strong><p>${safe(item.mandate?.name||'策略版本')} · ${item.strategies?.length||0} 档策略</p><small>${safe(item.approved_by||'—')} · ${safe(localTimestamp(item.activated_at||item.created_at))}</small></div><span class="status-tag">${item.status==='ACTIVE'?'当前策略版本':safe(item.status)}</span></div>`).join('');
  const sentimentRows=sentimentSources.map(item=>`<div class="harness-row"><div><span class="badge">${safe(item.source_kind)}</span><strong>${safe(item.source_code)}</strong><p>文档 ${item.document_count||0} · ${Number(item.latency_ms||0).toFixed(0)} ms</p><small>${safe(item.error||item.last_success_at||'尚无成功记录')}</small></div><span class="status-tag">${safe(item.status)}</span></div>`).join('');
  const codeCheckRows=(codeEvolution.last_evaluation?.checks||[]).map(item=>`<div class="harness-row"><div><strong>${safe(item.name||'门禁检查')}</strong><small>${Number(item.duration_seconds||0).toFixed(1)} 秒</small></div><span class="status-tag">${Number(item.returncode)===0?'通过':'失败'}</span></div>`).join('');
  $('#harnessView').innerHTML=`${quantDecisionPanelHtml(detail,learning,quantVersions,data.sector_cache)}<details class="advanced-harness professional-disclosure"><summary>专业运行记录与高级设置</summary><div class="advanced-harness-body"><section class="research-summary"><article class="metric-card"><span>代理运行</span><strong>${rs.runs||0}</strong><small>${rs.running||0} 正在执行</small></article><article class="metric-card"><span>等待批准</span><strong>${rs.waiting_approval||0}</strong><small>高影响操作门禁</small></article><article class="metric-card"><span>可恢复</span><strong>${(rs.failed||0)+(rs.interrupted||0)}</strong><small>保留步骤检查点</small></article><article class="metric-card accent"><span>允许工具</span><strong>${runtime.tools?.length||0}</strong><small>无 Shell · 不下单</small></article></section>
  <section class="panel agent-launcher"><div class="panel-head"><div><p class="eyebrow">BOUNDED AGENT LOOP</p><h2>启动领域工作流</h2></div><button id="harnessRefreshButton" class="secondary-button">刷新状态</button></div><div class="agent-launch-form"><label>工作流<select id="harnessWorkflow">${workflows}</select></label><div class="agent-workflow-fields" data-workflow="stock_analysis"><label>股票<input id="harnessAgentStocks" value="600519,000858" placeholder="2-8 只股票"></label><label>投资态度<select id="harnessAgentProfile"><option value="aggressive">激进派</option><option value="balanced" selected>中间派</option><option value="conservative">保守派</option></select></label><label>研究主题<input id="harnessResearchQuery" placeholder="可选"></label></div><div class="agent-workflow-fields" data-workflow="quality_audit"><label>巡检股票数<input id="harnessAuditLimit" type="number" min="1" max="100" value="20"></label></div><div class="agent-workflow-fields quant-portfolio-fields" data-workflow="quant_portfolio"><label>组合名称<input id="quantName" value="A股量化组合"></label><label>本金（元）<input id="quantCapital" type="number" min="1000" value="100000"></label><label>期限（月）<input id="quantHorizon" type="number" min="1" max="120" value="12"></label><label>目标收益（%）<input id="quantTarget" type="number" min="0" max="300" value="20"></label><label>最大回撤（%）<input id="quantDrawdown" type="number" min="1" max="80" value="15"></label><label>止损（%）<input id="quantStopLoss" type="number" min="1" max="80" value="8"></label><label>止盈目标（%）<input id="quantTakeProfitPct" type="number" min="1" max="300" value="20"></label><label>移动止盈回撤（%）<input id="quantTrailingStop" type="number" min="1" max="80" value="8"></label><label>关注板块<input id="quantSectors" value="消费,新能源" placeholder="例如 白酒、新能源、银行"></label><label>候选股票（可选）<input id="quantStocks" placeholder="留空则按板块自动筛选"></label><label>候选池数量<input id="quantCandidates" type="number" min="2" max="30" value="12"></label><label>同时持仓<input id="quantPositions" type="number" min="1" max="8" value="2"></label><label>风险档位<select id="quantRiskProfile"><option value="auto" selected>自动择优</option><option value="aggressive">激进</option><option value="balanced">平衡</option><option value="conservative">保守</option></select></label><label>止盈方式<select id="quantTakeProfitMode"><option value="trailing" selected>浮动止盈</option><option value="fixed">固定止盈</option></select></label><label>迭代次数<input id="quantIterations" type="number" min="1" max="20" value="10"></label></div><div class="agent-workflow-fields strategy-evolution-fields" data-workflow="strategy_evolution"><label>实验名称<input id="strategyMandateName" value="三档组合策略实验"></label><label>本金（元）<input id="strategyCapital" type="number" min="1000" value="100000"></label><label>期限（月）<input id="strategyHorizon" type="number" min="3" max="120" value="12"></label><label>目标收益（%）<input id="strategyTarget" type="number" min="0" max="300" value="20"></label><label>最大回撤（%）<input id="strategyDrawdown" type="number" min="1" max="80" value="20"></label><label>股票池<input id="strategyStocks" value="600519,000858,300750" placeholder="2-8 只股票"></label><label>关注板块<input id="strategySectors" value="消费,新能源" placeholder="可选，逗号分隔"></label><label>同时持仓<input id="strategyPositions" type="number" min="1" max="8" value="2"></label><label>迭代次数<input id="strategyIterations" type="number" min="1" max="20" value="10"></label><label>止盈方式<select id="strategyTakeProfit"><option value="trailing" selected>浮动止盈</option><option value="fixed">固定止盈</option></select></label></div><div class="agent-workflow-fields" data-workflow="continuous_learning"><label>运行阶段<select id="learningPhase"><option value="BACKFILL" selected>立即回填评估</option><option value="PRE_OPEN">盘前预测</option><option value="POST_CLOSE">盘后完整进化</option></select></label><label>股票数<input id="learningStockLimit" type="number" min="3" max="100" value="20"></label><label>社交平台股票数<input id="learningSocialLimit" type="number" min="0" max="10" value="3"></label><label>Qwen3 适配轮数<input id="learningDeepEpochs" type="number" min="4" max="100" value="32"></label><label>回撤门禁（%）<input id="learningDrawdown" type="number" min="1" max="80" value="15"></label><label><input id="learningDeep" type="checkbox" checked> Qwen3 时序模型</label><label><input id="learningIntraday" type="checkbox" checked> 5 分钟策略</label><label><input id="learningCode" type="checkbox" checked> 源码候选门禁</label></div><div class="agent-workflow-fields" data-workflow="strategy_activation"><label>策略实验<select id="strategyExperimentKey">${experimentOptions}</select></label></div><div class="agent-workflow-fields" data-workflow="candidate_activation"><label>候选 ID<input id="harnessCandidateId" type="number" min="1" placeholder="例如 3"></label></div><div class="agent-workflow-fields" data-workflow="version_rollback"><label>目标版本<select id="harnessRollbackVersion">${rollbackOptions}</select></label></div><label class="agent-intent">任务目标<input id="harnessAgentIntent" placeholder="本次运行的验收目标"></label><label class="agent-thread-toggle"><input id="harnessContinueThread" type="checkbox" ${detail?'':'disabled'}>沿用当前线程</label><button id="harnessStartRun" class="primary-button">开始运行</button></div><p class="lab-warning">每日盘后按同一数据快照更新行情、舆情、模型、分钟策略与受控源码版本；不连接券商、不执行真实交易。</p></section>
  <section class="agent-workspace"><aside class="panel agent-run-list"><div class="subsection-head"><span>运行队列</span><small>${runs.length} 条</small></div>${runRows||'<div class="empty-state">尚无代理运行</div>'}</aside>${harnessRunDetailHtml(detail)}</section>
  <div class="harness-section-title"><p class="eyebrow">CONTINUOUS LEARNING LOOP</p><h2>A 股持续学习闭环</h2></div>${learningProgressHtml(learning.last_cycle)}<section class="research-summary"><article class="metric-card"><span>当前预测模型</span><strong>${learning.active_model?'ONLINE · ACTIVE':'未初始化'}</strong><small>${safe(learning.active_model?.training_end||'等待首轮回测')}</small></article><article class="metric-card"><span>已评分预测</span><strong>${predictionStats.scored||0}</strong><small>待评分 ${predictionStats.pending||0} 条</small></article><article class="metric-card"><span>真实方向准确率</span><strong>${predictionStats.accuracy===null||predictionStats.accuracy===undefined?'—':(Number(predictionStats.accuracy)*100).toFixed(1)+'%'}</strong><small>Brier ${predictionStats.brier===null||predictionStats.brier===undefined?'—':Number(predictionStats.brier).toFixed(4)}</small></article><article class="metric-card accent"><span>六个月候选评估</span><strong>${candidateMetrics.directional_accuracy===undefined?'—':(Number(candidateMetrics.directional_accuracy)*100).toFixed(1)+'%'}</strong><small>${safe(learningEval.status||'尚未运行')} · 回撤 ${candidateMetrics.max_drawdown===undefined?'—':(Number(candidateMetrics.max_drawdown)*100).toFixed(1)+'%'}</small></article></section>
  <div class="harness-section-title"><p class="eyebrow">FULL-STACK EVOLUTION</p><h2>模型、分钟策略、舆情与源码</h2></div><section class="research-summary"><article class="metric-card"><span>Qwen3 时序模型</span><strong>${deep.active_model?'Qwen3 · ACTIVE':safe(deepLast.status||(deep.base_model_ready?'待训练':'权重缺失'))}</strong><small>${safe(deepHoldout.date_end||deep.base_model_id||'—')} · 准确率 ${deepHoldout.accuracy===undefined?'—':(Number(deepHoldout.accuracy)*100).toFixed(1)+'%'}</small></article><article class="metric-card"><span>5 分钟策略</span><strong>${safe(intradayActive.version_key||intraday.last_run?.metrics?.version_status||'未回测')}</strong><small>留出收益 ${intradayHoldout.total_return===undefined?'—':(Number(intradayHoldout.total_return)*100).toFixed(1)+'%'} · 回撤 ${intradayHoldout.max_drawdown===undefined?'—':(Number(intradayHoldout.max_drawdown)*100).toFixed(1)+'%'}</small></article><article class="metric-card"><span>舆情来源</span><strong>${sentimentCoverage.healthy||0} / ${sentimentSources.length||0}</strong><small>健康 / 已登记 · X ${safe(sentimentSources.find(item=>item.source_code==='x_official')?.status||'未检查')}</small></article><article class="metric-card accent"><span>源码进化</span><strong>${safe(codeEvolution.last_candidate?.status||'未运行')}</strong><small>${codeGate.passed?'门禁通过':(codeEvolution.last_evaluation?'门禁未通过':'等待候选')} · ${codeEvolution.active_version?'可回滚版本已激活':'无激活版本'}</small></article></section>
  <section class="harness-grid"><article class="panel"><div class="panel-head"><div><p class="eyebrow">SOURCE COVERAGE</p><h2>舆情来源健康</h2></div><span class="count">${sentimentCoverage.healthy||0} 健康</span></div><div class="harness-list">${sentimentRows||'<div class="empty-state">尚未采集多源舆情</div>'}</div></article><article class="panel"><div class="panel-head"><div><p class="eyebrow">ISOLATED CODE GATE</p><h2>源码候选与回滚</h2></div><div class="harness-row-actions"><button id="codeEvolutionRun" class="secondary-button">立即评测</button>${codeEvolution.active_version?'<button id="codeEvolutionRollback" class="secondary-button">回滚源码</button>':''}</div></div><div class="harness-list">${codeCheckRows||'<div class="empty-state">尚未运行隔离源码门禁</div>'}</div></article></section>
  <div class="harness-section-title"><p class="eyebrow">DAILY QUANT PORTFOLIO VERSIONS</p><h2>每日量化组合更新</h2></div><section class="research-summary"><article class="metric-card"><span>组合运行</span><strong>${quantRuns.length}</strong><small>含手动与盘后更新</small></article><article class="metric-card"><span>当前研究版本</span><strong>${safe(quantVersions.find(item=>item.status==='ACTIVE')?.version_key||'尚未建立')}</strong><small>退化版本不会覆盖当前版本</small></article><article class="metric-card"><span>最近数据截点</span><strong>${safe(quantRuns[0]?.summary?.data_asof||'—')}</strong><small>${safe(quantRuns[0]?.name||'等待首次量化运行')}</small></article><article class="metric-card accent"><span>源码门禁</span><strong>${codeEvolution.automatic_code_changes?'启用':'禁用'}</strong><small>隔离副本 · 全量测试 · 样本外 · 可回滚</small></article></section>
  <div class="harness-section-title"><p class="eyebrow">PORTFOLIO STRATEGY EVOLUTION</p><h2>策略实验与版本</h2></div><section class="harness-grid"><article class="panel"><div class="panel-head"><div><p class="eyebrow">滚动验证 + 最终留出</p><h2>策略实验</h2></div><span class="count">${experiments.length} 次</span></div><div class="harness-list">${experimentRows||'<div class="empty-state">尚无策略实验</div>'}</div></article><article class="panel"><div class="panel-head"><div><p class="eyebrow">人工审批</p><h2>已激活策略版本</h2></div><span class="count">${strategyVersions.length} 个</span></div><div class="harness-list">${strategyVersionRows||'<div class="empty-state">尚无已激活策略版本</div>'}</div></article></section>
  <div class="harness-section-title"><p class="eyebrow">FEEDBACK AND EVOLUTION</p><h2>反馈、回归与配置演化</h2></div><section class="research-summary"><article class="metric-card"><span>坏案例</span><strong>${s.bad_cases||0}</strong><small>自动去重累计</small></article><article class="metric-card"><span>待处理</span><strong>${s.open_cases||0}</strong><small>等待期望或候选</small></article><article class="metric-card"><span>候选改进</span><strong>${s.candidates||0}</strong><small>默认不生效</small></article><article class="metric-card accent"><span>最近通过率</span><strong>${s.latest_pass_rate===null||s.latest_pass_rate===undefined?'—':(Number(s.latest_pass_rate)*100).toFixed(0)+'%'}</strong><small>${latest?safe(harnessStatus(latest.status)):'尚未评测'}</small></article></section>
  <section class="panel harness-autonomy"><div class="panel-head"><div><p class="eyebrow">主动预测与修正</p><h2>自主巡检摘要</h2></div><button id="harnessAutonomyButton" class="primary-button">立即自主巡检</button></div><p class="autonomy-summary">${autonomy?safe(autonomy.summary):'服务启动后自动运行，检查股票解析、搜索可达性、日线覆盖、策略契约和 Skill 说明。'}</p><div class="harness-list">${autonomyFindings}</div><p class="lab-warning">巡检可以记录案例、生成候选和运行回归，但不会自动切换当前配置；晋级和回滚必须明确批准。</p></section>
  <section class="panel harness-submit"><div class="panel-head"><div><p class="eyebrow">坏案例采集</p><h2>提交坏案例</h2></div><button id="harnessBaselineButton" class="secondary-button">运行全量回归</button></div><div class="harness-form"><select id="harnessCaseType"><option value="search_no_result">搜索无结果</option><option value="stock_resolution">股票识别错误</option><option value="ranking_mismatch">排序不符合期望</option><option value="data_gap">数据缺失</option><option value="interaction_failure">页面交互失败</option><option value="comparison_error">股票对比异常</option></select><select id="harnessSeverity"><option value="MEDIUM">中等</option><option value="HIGH">严重</option><option value="CRITICAL">关键</option><option value="LOW">轻微</option></select><textarea id="harnessInput" placeholder='输入JSON，例如 {"query":"红太杨"}'></textarea><textarea id="harnessExpected" placeholder='期望JSON，例如 {"canonical_query":"000525","symbol":"000525"}'></textarea><textarea id="harnessObserved" placeholder="实际结果JSON，可留空"></textarea><input id="harnessNotes" placeholder="补充说明"><button id="harnessSubmitButton" class="primary-button">记录坏案例</button></div><p class="lab-warning">人工反馈与自主巡检共享同一回归集；不把未经验证的猜测直接写入当前策略。</p></section>
  <section class="harness-grid"><article class="panel"><div class="panel-head"><div><p class="eyebrow">回归案例集</p><h2>坏案例回归集</h2></div><span class="count">${cases.length} 条</span></div><div class="harness-list">${caseRows||'<div class="empty-state">尚无坏案例</div>'}</div></article><article class="panel"><div class="panel-head"><div><p class="eyebrow">候选门禁</p><h2>候选与晋级门禁</h2></div><span class="count">人工批准</span></div><div class="harness-list">${candidateRows||'<div class="empty-state">尚无候选改进</div>'}</div></article></section>
  <section class="panel"><div class="panel-head"><div><p class="eyebrow">版本控制</p><h2>配置版本与回滚</h2></div><span class="count">当前 ${safe(data.active_version.version_key)}</span></div><div class="harness-list">${versionRows}</div></section></div></details>`;
  $('#quantRiskProfile option[value="auto"]')?.remove();
  if($('#quantRiskProfile'))$('#quantRiskProfile').value='balanced';
  document.querySelectorAll('#harnessView details.stock-profile').forEach(details=>{const symbol=details.closest('tr')?.querySelector('td:nth-child(2) small')?.textContent;details.open=Boolean(symbol&&openStockProfiles.has(symbol));});
  bindHarness(data,detail);
}

function updateHarnessWorkflowFields(){document.querySelectorAll('.agent-workflow-fields').forEach(item=>item.classList.toggle('active',item.dataset.workflow===state.harnessWorkflow));}
function scheduleHarnessPoll(detail,learningCycle,sectorCache){window.clearTimeout(state.harnessPollTimer);const runActive=detail&&['QUEUED','RUNNING'].includes(detail.run.status),learningActive=learningCycle&&learningCycle.status==='RUNNING',cacheActive=sectorCache&&['QUEUED','RUNNING_MARKET','RUNNING_FUNDAMENTALS'].includes(sectorCache.status);if((runActive||learningActive||cacheActive)&&document.querySelector('#harnessView.active'))state.harnessPollTimer=window.setTimeout(loadHarness,HARNESS_ACTIVE_POLL_MS);}
async function loadHarness(){const view=$('#harnessView');if(!view)return;if(!view.children.length)view.innerHTML='<div class="search-loading">正在读取股票推荐…</div>';try{const [data,decision]=await Promise.all([request('/api/harness'),request('/api/quant/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input:quantDecisionInput()})})]);state.quantDecision=decision;const runtime=data.agent_runtime||{},runs=runtime.runs||[];if(!state.harnessRunKey){const preferred=runtime.latest_quant_run||runs.find(item=>item.workflow==='quant_portfolio')||runs[0];if(preferred)state.harnessRunKey=preferred.run_key;}let detail=null;if(state.harnessRunKey){try{detail=await request(`/api/harness/runs/${encodeURIComponent(state.harnessRunKey)}`);}catch{state.harnessRunKey=null;}}renderHarness(data,detail);updateHarnessWorkflowFields();scheduleHarnessPoll(detail,data.continuous_learning?.last_cycle,data.sector_cache);}catch(error){view.innerHTML=`<div class="empty-state">${safe(error.message)}</div>`;}}

function bindHarness(data,detail){
  document.querySelectorAll('[data-quant-draft]').forEach(element=>{const save=()=>{state.quantDraft[element.dataset.quantDraft]=element.type==='number'?Number(element.value):element.value;persistQuantDraft();};element.oninput=save;element.onchange=save;});
  document.querySelectorAll('[data-quant-profile]').forEach(button=>button.onclick=()=>{state.quantDraft.risk_profile=button.dataset.quantProfile;persistQuantDraft();document.querySelectorAll('[data-quant-profile]').forEach(item=>item.classList.toggle('active',item===button));});
  $('#quickQuantRun').onclick=async()=>{const button=$('#quickQuantRun');button.disabled=true;button.textContent='正在读取最近数据…';try{const registered=await request('/api/quant/mandates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({input:quantDecisionInput()})});state.quantDecision=registered.decision;const versionStatus=registered.decision.version?.status;showToast(versionStatus==='RULE_SNAPSHOT'?'已生成样本外校准区间和研究复核':versionStatus==='SNAPSHOT'?'已根据最近交易日数据生成推荐':registered.decision.status==='AVAILABLE'?'已读取最近一次正式结果':`现在还无法计算：${plainQuantText(registered.decision.snapshot_error||'当前缓存数据不足')}`);await loadHarness();}catch(error){showToast(`保存失败：${error.message}`);button.disabled=false;button.textContent='按这些条件查看推荐';}};
  $('#harnessWorkflow').onchange=event=>{state.harnessWorkflow=event.target.value;updateHarnessWorkflowFields();};
  $('#harnessRefreshButton').onclick=loadHarness;
  $('#harnessStartRun').onclick=async()=>{const button=$('#harnessStartRun');button.disabled=true;const workflow=state.harnessWorkflow;let input={};if(workflow==='stock_analysis')input={stocks:$('#harnessAgentStocks').value,profile:$('#harnessAgentProfile').value,research_query:$('#harnessResearchQuery').value};if(workflow==='quality_audit')input={stock_limit:Number($('#harnessAuditLimit').value)};if(workflow==='quant_portfolio')input={name:$('#quantName').value,capital:Number($('#quantCapital').value),horizon_months:Number($('#quantHorizon').value),target_return_pct:Number($('#quantTarget').value),max_drawdown_pct:Number($('#quantDrawdown').value),stop_loss_pct:Number($('#quantStopLoss').value),take_profit_pct:Number($('#quantTakeProfitPct').value),trailing_stop_pct:Number($('#quantTrailingStop').value),sectors:$('#quantSectors').value,stocks:$('#quantStocks').value,max_candidates:Number($('#quantCandidates').value),max_positions:Number($('#quantPositions').value),risk_profile:$('#quantRiskProfile').value,take_profit_mode:$('#quantTakeProfitMode').value,max_iterations:Number($('#quantIterations').value),refresh_data:true,collect_sentiment:true};if(workflow==='strategy_evolution')input={name:$('#strategyMandateName').value,capital:Number($('#strategyCapital').value),horizon_months:Number($('#strategyHorizon').value),target_return_pct:Number($('#strategyTarget').value),max_drawdown_pct:Number($('#strategyDrawdown').value),stocks:$('#strategyStocks').value,sectors:$('#strategySectors').value,max_positions:Number($('#strategyPositions').value),max_iterations:Number($('#strategyIterations').value),take_profit_mode:$('#strategyTakeProfit').value};if(workflow==='continuous_learning')input={phase:$('#learningPhase').value,stock_limit:Number($('#learningStockLimit').value),max_social_symbols:Number($('#learningSocialLimit').value),deep_epochs:Number($('#learningDeepEpochs').value),max_drawdown:Number($('#learningDrawdown').value)/100,refresh_data:$('#learningPhase').value!=='BACKFILL',collect_sentiment:true,train_deep_model:$('#learningDeep').checked,evolve_intraday:$('#learningIntraday').checked,evolve_source_code:$('#learningCode').checked,auto_promote:true,auto_promote_code:true};if(workflow==='strategy_activation')input={experiment_key:$('#strategyExperimentKey').value};if(workflow==='candidate_activation')input={candidate_id:Number($('#harnessCandidateId').value)};if(workflow==='version_rollback')input={version_key:$('#harnessRollbackVersion').value};try{const created=await request('/api/harness/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workflow,input,intent:$('#harnessAgentIntent').value,thread_key:$('#harnessContinueThread').checked?detail?.thread?.thread_key:null})});state.harnessRunKey=created.run.run_key;showToast('Harness 运行已创建');await loadHarness();}catch(error){showToast(`创建失败：${error.message}`);button.disabled=false;}};
  document.querySelectorAll('.harness-run-open').forEach(button=>button.onclick=()=>{state.harnessRunKey=button.dataset.key;loadHarness();});
  if($('#harnessResumeRun'))$('#harnessResumeRun').onclick=async()=>{try{await request(`/api/harness/runs/${encodeURIComponent(detail.run.run_key)}/resume`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});showToast('已从检查点续跑');await loadHarness();}catch(error){showToast(`续跑失败：${error.message}`);}};
  if($('#harnessCancelRun'))$('#harnessCancelRun').onclick=async()=>{if(!window.confirm('确认取消本次运行？'))return;try{await request(`/api/harness/runs/${encodeURIComponent(detail.run.run_key)}/cancel`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});showToast('运行已取消');await loadHarness();}catch(error){showToast(`取消失败：${error.message}`);}};
  document.querySelectorAll('.harness-approval-decision').forEach(button=>button.onclick=async()=>{const approved=button.dataset.approved==='true',actor=window.prompt('请输入操作人名称');if(!actor||!window.confirm(approved?'确认批准并继续执行？':'确认拒绝并终止运行？'))return;try{await request(`/api/harness/approvals/${encodeURIComponent(button.dataset.key)}/resolve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approved,resolved_by:actor,confirmed:true})});showToast(approved?'已批准，运行继续':'已拒绝，运行停止');await loadHarness();}catch(error){showToast(`审批失败：${error.message}`);}});
  $('#harnessAutonomyButton').onclick=async()=>{const button=$('#harnessAutonomyButton');button.disabled=true;button.textContent='巡检与回归中…';try{const result=await request('/api/harness/autonomous/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auto_apply:false,stock_limit:20})});showToast(result.summary);await loadHarness();}catch(error){showToast(`自主巡检失败：${error.message}`);button.disabled=false;button.textContent='立即自主巡检';}};
  if($('#codeEvolutionRun'))$('#codeEvolutionRun').onclick=async()=>{if(!window.confirm('将运行隔离数据库快照、全量测试和样本外回测，确认继续？'))return;const button=$('#codeEvolutionRun');button.disabled=true;button.textContent='隔离评测中…';try{const result=await request('/api/harness/code-evolution/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stock_limit:20,max_drawdown:0.15,auto_promote:true})});showToast(`源码候选：${result.status}`);await loadHarness();}catch(error){showToast(`源码评测失败：${error.message}`);button.disabled=false;button.textContent='立即评测';}};
  if($('#codeEvolutionRollback'))$('#codeEvolutionRollback').onclick=async()=>{const actor=window.prompt('请输入回滚操作人名称');if(!actor||!window.confirm('确认从备份恢复上一版策略源码？'))return;try{const result=await request('/api/harness/code-evolution/rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({approved_by:actor,confirmed:true,reason:'UI rollback'})});showToast(`源码${result.status}`);await loadHarness();}catch(error){showToast(`源码回滚失败：${error.message}`);}};
  $('#harnessSubmitButton').onclick=async()=>{const button=$('#harnessSubmitButton');button.disabled=true;button.textContent='记录中…';try{await request('/api/harness/bad-cases',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({case_type:$('#harnessCaseType').value,severity:$('#harnessSeverity').value,input:harnessJson($('#harnessInput').value),expected:harnessJson($('#harnessExpected').value,'title_contains'),observed:harnessJson($('#harnessObserved').value,'value'),notes:$('#harnessNotes').value,source:'manual'})});showToast('坏案例已进入回归集');await loadHarness();}catch(error){showToast(`记录失败：${error.message}`);}};
  $('#harnessBaselineButton').onclick=async()=>{const button=$('#harnessBaselineButton');button.disabled=true;button.textContent='评测中…';try{const result=await request('/api/harness/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});showToast(`回归完成：通过 ${result.passed_cases}/${result.total_cases}`);await loadHarness();}catch(error){showToast(`评测失败：${error.message}`);}};
  document.querySelectorAll('.harness-generate').forEach(button=>button.onclick=async()=>{button.disabled=true;try{await request('/api/harness/candidates/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bad_case_id:Number(button.dataset.id)})});showToast('候选改进已生成');await loadHarness();}catch(error){showToast(`生成失败：${error.message}`);}});
  document.querySelectorAll('.harness-evaluate').forEach(button=>button.onclick=async()=>{button.disabled=true;try{const result=await request('/api/harness/evaluate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({candidate_id:Number(button.dataset.id)})});showToast(`候选评测：通过 ${result.passed_cases}/${result.total_cases}`);await loadHarness();}catch(error){showToast(`评测失败：${error.message}`);}});
  document.querySelectorAll('.harness-approve').forEach(button=>button.onclick=async()=>{const approvedBy=window.prompt('请输入批准人名称');if(!approvedBy||!window.confirm('确认将该候选晋级为当前配置？'))return;try{await request('/api/harness/candidates/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({candidate_id:Number(button.dataset.id),approved_by:approvedBy,confirmed:true})});showToast('候选已晋级');await loadHarness();}catch(error){showToast(`晋级失败：${error.message}`);}});
  document.querySelectorAll('.harness-rollback').forEach(button=>button.onclick=async()=>{const approvedBy=window.prompt('请输入回滚操作人名称');if(!approvedBy||!window.confirm(`确认回滚至 ${button.dataset.version}？`))return;try{await request('/api/harness/versions/rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version_key:button.dataset.version,approved_by:approvedBy,confirmed:true})});showToast('配置版本已回滚');await loadHarness();}catch(error){showToast(`回滚失败：${error.message}`);}});
}

const signalActionLabel = value => ({BUY:'买入复核',SELL:'卖出复核',REBALANCE:'调仓复核',HOLD:'继续观察',WATCH:'观察'}[value]||value);
const signalStatusLabel = value => ({NEW:'待复核',ACKNOWLEDGED:'已确认',DISMISSED:'已忽略',SUPERSEDED:'已被新版本替代'}[value]||value);

function renderSignalCenter(data, models){
  const feed=data.feed||{},signals=feed.signals||[],subscriptionData=data.subscriptions||{};
  const subscriptions=subscriptionData.subscriptions||[],counts=data.outbox?.counts||{};
  const queued=Number(counts.PENDING||0)+Number(counts.RETRY||0);
  $('#signalUnread').textContent=feed.unread_actionable||0;
  $('#signalQueued').textContent=queued;
  $('#signalSubscriptions').textContent=subscriptions.filter(item=>item.enabled).length;
  $('#notificationBadge').textContent=feed.unread_actionable||0;
  $('#notificationBadge').hidden=!feed.unread_actionable;
  $('#signalFeed').innerHTML=signals.length?signals.map(item=>{
    const summary=typeof item.rationale?.summary==='string'?item.rationale.summary:signalActionLabel(item.action);
    const canReview=item.status!=='SUPERSEDED';
    return `<article class="signal-row action-${safe(item.action.toLowerCase())}">
      <div class="signal-action"><span>${safe(signalActionLabel(item.action))}</span><strong>${safe(item.name)}</strong><small>${safe(item.symbol)} · ${safe(item.data_asof)}</small></div>
      <div class="signal-reference"><span>参考价<strong>${item.reference_price==null?'—':money(item.reference_price,2)}</strong></span><span>参考数量<strong>${item.shares==null?'—':money(item.shares,0)+' 股'}</strong></span><span>组合权重<strong>${item.weight==null?'—':(Number(item.weight)*100).toFixed(1)+'%'}</strong></span><span>置信度<strong>${item.confidence==null?'—':(Number(item.confidence)*100).toFixed(0)+'%'}</strong></span></div>
      <div class="signal-rationale"><strong>${safe(summary)}</strong><span>${safe(item.invalidation)}</span><small class="beginner-only">数据截至 ${safe(item.data_asof)} · ${safe(signalStatusLabel(item.status))}</small><small class="professional-only">版本 ${safe(item.version_key)} · ${safe(item.validation_status)} · ${safe(signalStatusLabel(item.status))}</small></div>
      <div class="signal-actions">${canReview?`<button type="button" class="secondary-button" data-signal-review="acknowledge" data-id="${item.id}">确认</button><button type="button" class="icon-button signal-dismiss" data-signal-review="dismiss" data-id="${item.id}" title="忽略该信号" aria-label="忽略该信号">×</button>`:''}</div>
    </article>`;
  }).join(''):'<div class="signal-empty"><strong>暂无盘后信号</strong><span>登记投资约束后，最近交易日快照会进入观察；正式版本切换后生成买入、卖出或调仓复核项。</span></div>';
  $('#subscriptionList').innerHTML=subscriptions.length?subscriptions.map(item=>`<div class="subscription-row">
    <div><strong>${safe(String(item.name).replaceAll('Argus','Rooftop').replaceAll('ARGUS','ROOFTOP'))}</strong><span>${safe(item.target||subscriptionData.smtp?.default_target||'服务器默认邮箱')} · ${(item.event_kinds||[]).map(signalActionLabel).join(' / ')}</span></div>
    <span class="status-pill ${item.enabled?'positive':'muted'}">${item.enabled?'已启用':'已停用'}</span>
    <div class="subscription-actions"><button type="button" class="icon-button" data-subscription-test="${item.id}" title="发送测试邮件" aria-label="发送测试邮件">↗</button>
    <button type="button" class="secondary-button" data-subscription-toggle="${item.id}" data-enabled="${item.enabled}">${item.enabled?'停用':'启用'}</button></div>
  </div>`).join(''):`<div class="signal-empty compact"><strong>尚未订阅</strong><span>SMTP ${subscriptionData.smtp?.configured?'已配置':'未配置'}</span></div>`;
  renderModelRegistry(models);
  bindSignalRowControls();
}

function renderModelRegistry(payload){
  const definitions=payload?.definitions||[],assignments=payload?.assignments||[];
  const activeByModel=new Map(assignments.filter(item=>item.status==='ACTIVE').map(item=>[Number(item.model_id),item]));
  $('#activationModelId').innerHTML=definitions.map(item=>`<option value="${item.id}">${safe(item.name)} · ${safe(item.version)}</option>`).join('');
  $('#modelRegistry').innerHTML=definitions.length?`<div class="table-wrap"><table><thead><tr><th>模型</th><th>类型</th><th>版本</th><th>档位</th><th>状态</th><th>当前范围</th></tr></thead><tbody>${definitions.map(item=>{
    const assignment=activeByModel.get(Number(item.id));
    return `<tr><td><strong>${safe(item.name)}</strong><small>${safe(item.model_key)}</small></td><td>${safe(item.model_kind)}</td><td>${safe(item.version)}</td><td>${safe(item.profile||'通用')}</td><td>${safe(item.status)}</td><td>${assignment?safe(`${assignment.scope_type}:${assignment.scope_value}`):'未激活'}</td></tr>`;
  }).join('')}</tbody></table></div>`:'<div class="signal-empty compact"><strong>没有模型版本</strong></div>';
}

function bindSignalRowControls(){
  document.querySelectorAll('[data-signal-review]').forEach(button=>button.onclick=async()=>{
    button.disabled=true;
    try{await request(`/api/signals/${button.dataset.id}/${button.dataset.signalReview}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await loadSignals();}
    catch(error){showToast(`信号更新失败：${error.message}`);button.disabled=false;}
  });
  document.querySelectorAll('[data-subscription-toggle]').forEach(button=>button.onclick=async()=>{
    button.disabled=true;const action=button.dataset.enabled==='true'?'disable':'enable';
    try{await request(`/api/notification-subscriptions/${button.dataset.subscriptionToggle}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await loadSignals();}
    catch(error){showToast(`订阅更新失败：${error.message}`);button.disabled=false;}
  });
  document.querySelectorAll('[data-subscription-test]').forEach(button=>button.onclick=async()=>{
    button.disabled=true;
    try{const result=await request('/api/notifications/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({subscription_id:Number(button.dataset.subscriptionTest)})});showToast(result.delivery?.sent?'测试邮件已发送':`邮件已入队：${result.delivery?.status||'等待发送'}`);await loadSignals();}
    catch(error){showToast(`邮件测试失败：${error.message}`);button.disabled=false;}
  });
}

async function loadSignals(){
  const view=$('#signalFeed');if(!view)return;
  try{const [data,models]=await Promise.all([request('/api/notifications'),request('/api/models')]);renderSignalCenter(data,models);}
  catch(error){view.innerHTML=`<div class="signal-empty"><strong>信号中心载入失败</strong><span>${safe(error.message)}</span></div>`;}
}

function bindSignalForms(){
  $('#refreshSignals').onclick=loadSignals;
  $('#notificationButton').onclick=()=>document.querySelector('.nav-item[data-view="signals"]')?.click();
  $('#subscriptionForm').onsubmit=async event=>{
    event.preventDefault();const button=event.submitter;button.disabled=true;
    const eventKinds=[...document.querySelectorAll('input[name="signalEvent"]:checked')].map(item=>item.value);
    try{await request('/api/notification-subscriptions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('#subscriptionName').value,target:$('#subscriptionTarget').value,event_kinds:eventKinds,minimum_confidence:Number($('#subscriptionConfidence').value)/100})});showToast('邮件订阅已保存');await loadSignals();}
    catch(error){showToast(`订阅保存失败：${error.message}`);}finally{button.disabled=false;}
  };
  $('#modelForm').onsubmit=async event=>{
    event.preventDefault();const button=event.submitter;button.disabled=true;
    try{const specification=JSON.parse($('#modelSpecification').value);await request('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model_key:$('#modelKey').value,model_kind:$('#modelKind').value,name:$('#modelName').value,version:$('#modelVersion').value,profile:$('#modelProfile').value||null,created_by:$('#modelCreatedBy').value,specification})});showToast('模型草稿版本已创建');await loadSignals();}
    catch(error){showToast(`模型创建失败：${error.message}`);}finally{button.disabled=false;}
  };
  $('#modelActivationForm').onsubmit=async event=>{
    event.preventDefault();const button=event.submitter;if(!window.confirm('确认激活该模型版本并替换相同范围的当前模型？'))return;button.disabled=true;
    const id=Number($('#activationModelId').value),scope=$('#activationScope').value;
    try{await request(`/api/models/${id}/activate`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({scope_type:scope,scope_value:scope==='DEFAULT'?'*':$('#activationScopeValue').value,profile:$('#activationProfile').value||null,approved_by:$('#activationApprovedBy').value,confirmed:true})});showToast('模型版本已激活');await loadSignals();}
    catch(error){showToast(`模型激活失败：${error.message}`);}finally{button.disabled=false;}
  };
}

function bindNavigation() {
  const titles = {signals:'信号中心',compare:'股票对比',overview:'市场总览',library:'资料搜索',logic:'推荐怎么算',harness:'股票推荐'};
  document.querySelectorAll('.nav-item').forEach(button => button.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach(item => item.classList.remove('active'));
    document.querySelectorAll('.view').forEach(view => view.classList.remove('active'));
    button.classList.add('active');
    $(`#${button.dataset.view}View`).classList.add('active');
    $('#pageTitle').textContent = titles[button.dataset.view];
    if(button.dataset.view==='signals')loadSignals();
    if(button.dataset.view==='library'&&!$('#libraryView').children.length&&state.dashboard)renderLibraryView(state.dashboard);
    if(button.dataset.view==='logic')loadLogic();
    if(button.dataset.view==='harness')loadHarness();
  }));
  const rawRequested=new URLSearchParams(location.search).get('view'),requested=({research:'library',strategy:'library',reports:'library'}[rawRequested]||rawRequested);
  const requestedButton=document.querySelector(`.nav-item[data-view="${requested}"]`);
  if(requestedButton) requestedButton.click();
}

async function initialize() {
  try {
    const pageParams=new URLSearchParams(location.search),requestedAsset=pageParams.get('asset'),requestedPeriod=pageParams.get('period'),linkedQuery=pageParams.get('query');
    if(['1d','5m','15m','30m','60m','5d'].includes(requestedPeriod))state.period=requestedPeriod;
    const selectedSymbol = state.selected && state.selected.symbol;
    const data = normalizeDashboard(await request('/api/dashboard'));
    state.dashboard = data;
    $('#warning').textContent = `重要：${data.meta.warning}`;
    $('#asOf').textContent = data.meta.as_of;
    renderMarketSyncState(data.meta);
    renderSummary(data.portfolio, data.meta.evidence_coverage);
    renderPositions(data.portfolio);
    renderHypotheses(data.hypotheses);
    if(!$('#libraryView').children.length)renderLibraryView(data);
    if(linkedQuery&&$('#librarySearchInput')){$('#librarySearchInput').value=linkedQuery;$('#librarySearchButton')?.click();}
    const fallbackAssets=[...data.portfolio.positions,...data.markets.filter(m=>!data.portfolio.positions.some(p=>p.symbol===m.symbol))];
    const assets=data.analysis_assets.length?data.analysis_assets:fallbackAssets;
    state.overviewAssets=Object.fromEntries(assets.map(item=>[item.symbol,{symbol:item.symbol,name:item.name}]));
    if(/^\d{6}(?:\.(?:SH|SZ))?$/i.test(requestedAsset||'')&&!state.overviewAssets[requestedAsset])state.overviewAssets[requestedAsset]={symbol:requestedAsset,name:requestedAsset};
    $('#assetSuggestions').innerHTML = assets.map(p => `<option value="${safe(p.name)} ${safe(p.symbol)}"></option>`).join('');
    if (!assets.length) throw new Error('本地数据库中没有可展示的持仓或行情标的');
    const nextSymbol = /^\d{6}(?:\.(?:SH|SZ))?$/i.test(requestedAsset||'')?requestedAsset:(assets.some(p => p.symbol === selectedSymbol) ? selectedSymbol : assets[0].symbol);
    await selectAsset(nextSymbol);
  } catch (error) {
    $('#warning').textContent = `载入失败：${error.message}`;
    $('#warning').classList.add('negative');
  }
}

window.addEventListener('argus:comparison-completed',async event=>{
  try{
    const data=normalizeDashboard(await request('/api/dashboard'));
    state.dashboard=data;
    const assets=data.analysis_assets;
    if(!assets.length)return;
    const current=state.selected?.symbol;
    const compared=event.detail?.ranking?.[0]?.symbol;
    const nextSymbol=assets.some(item=>item.symbol===current)?current:(assets.some(item=>item.symbol===compared)?compared:assets[0].symbol);
    state.overviewAssets=Object.fromEntries(assets.map(item=>[item.symbol,{symbol:item.symbol,name:item.name}]));
    $('#assetSuggestions').innerHTML=assets.map(item=>`<option value="${safe(item.name)} ${safe(item.symbol)}"></option>`).join('');
    if($('#libraryStrategiesPane'))renderStrategyLab(data.strategy_lab);
    await selectAsset(nextSymbol);
  }catch(error){console.warn('comparison asset refresh failed',error);}
});

$('#assetLookupForm').addEventListener('submit',async event=>{event.preventDefault();const input=$('#assetInput'),button=$('#assetLookupButton'),raw=input.value.trim(),embedded=raw.match(/(?:^|\s)(\d{6})(?:\s|$)/),query=embedded?embedded[1]:raw;if(!query)return;button.disabled=true;button.textContent='读取中';try{const data=await request('/api/assets/resolve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query})}),stock=data.stock;state.overviewAssets[stock.symbol]=stock;const options=Object.values(state.overviewAssets);$('#assetSuggestions').innerHTML=options.map(item=>`<option value="${safe(item.name)} ${safe(item.symbol)}"></option>`).join('');await selectAsset(stock.symbol);if(data.cache?.errors?.length)showToast(`${stock.name}已显示本地数据，最新抓取有 ${data.cache.errors.length} 个来源错误`);else showToast(`${stock.name}行情已载入并保存到本地`);}catch(error){showToast(`无法读取：${error.message}`);}finally{button.disabled=false;button.textContent='查看';}});
$('#colorConvention').value=state.colorConvention;
$('#colorConvention').addEventListener('change',event=>{state.colorConvention=event.target.value;localStorage.setItem('argus-color-convention',state.colorConvention);if(state.chart){renderChart(state.chart);const change=state.chart.quote?state.chart.quote.change_pct:state.selected?.pnl_pct;$('#assetPnl').style.color=chartTrendColor(change||0);}});
$('#recheckButton').addEventListener('click', async () => {
  const p = state.selected;
  if (!p) return;
  const result = await request('/api/risk/evaluate', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});
  showToast(`风险检查完成：${actionLabel(result.discipline.action)}`);
});
bindPortfolioImport();
const stockEvidenceObserver=new MutationObserver(records=>records.forEach(record=>record.addedNodes.forEach(node=>{if(node.nodeType===Node.ELEMENT_NODE)enhanceStockProfileLinks(node);})));
stockEvidenceObserver.observe(document.body,{childList:true,subtree:true});
bindNavigation();
bindSignalForms();
loadSignals();
initialize();
window.setInterval(refreshRealtimeOverview, 60_000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshRealtimeOverview();});
window.setInterval(()=>{if(document.querySelector('#libraryView.active')&&state.libraryTab==='reports')loadReportLibrary();},60_000);
