(() => {
  const colors = ['#31d6a0', '#f5bc60', '#68a9ff', '#ff6b76', '#b493ff', '#78e3e5', '#f08c46', '#d7e36d'];
  const $ = selector => document.querySelector(selector);
  const safe = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const number = (value, digits = 1) => value == null ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits: digits, minimumFractionDigits: digits});
  const pct = value => value == null ? '—' : `${number(Number(value) * 100, 1)}%`;
  const metricPct = value => value == null ? '—' : `${number(value, 1)}%`;
  let lastResult = null;

  function selectedProfile() {
    return document.querySelector('input[name="profile"]:checked')?.value || 'balanced';
  }

  function setStatus(message, tone = '') {
    const status = $('#compareStatus');
    status.className = `compare-status ${tone}`.trim();
    status.textContent = message;
  }

  function legend(items) {
    return items.map((item, index) => `<span><i style="--legend:${colors[index % colors.length]}"></i>${safe(item.name)}</span>`).join('');
  }

  function formatCap(value) {
    if (value == null) return '—';
    if (value >= 1e12) return `${number(value / 1e12, 2)} 万亿`;
    return `${number(value / 1e8, 0)} 亿`;
  }

  function renderVerdict(data) {
    const verdict = data.verdict;
    $('#compareVerdict').innerHTML = `<div class="verdict-rank"><span>01</span><small>${safe(data.profile.label)}排序</small></div>
      <div class="verdict-copy"><p class="eyebrow">COMPARISON VERDICT</p><h2>${safe(verdict.headline)}</h2><p>${safe(verdict.reason)}</p><small>${safe(verdict.qualification)}</small></div>
      <div class="verdict-score"><span>领先评分</span><strong>${number(data.ranking[0].score, 1)}</strong><small>置信度 ${data.ranking[0].confidence}%</small></div>`;
  }

  function renderTable(data) {
    const rows = data.ranking.map((item, index) => {
      const metrics = item.metrics, fundamental = item.fundamentals, backtest = item.backtest;
      const quotePrice = item.quote?.price ?? metrics.last_close;
      return `<tr><td><span class="rank-index">${String(index + 1).padStart(2, '0')}</span></td>
        <td><strong>${safe(item.name)}</strong><small>${safe(item.symbol)} · ${safe(fundamental.industry)}</small></td>
        <td><strong class="score-number">${number(item.score, 1)}</strong><small>${safe(item.stance)}</small></td>
        <td>${number(quotePrice, 2)}<small>${safe(item.quote?.source || '日线收盘')}</small></td>
        <td>${pct(metrics.momentum_60d)}<small>20日 ${pct(metrics.momentum_20d)}</small></td>
        <td>${pct(metrics.annualized_volatility)}<small>回撤 ${pct(metrics.max_drawdown)}</small></td>
        <td>${number(fundamental.pe_ttm ?? fundamental.pe_dynamic, 1)}<small>PB ${number(fundamental.pb, 2)}</small></td>
        <td class="${backtest.total_return_pct >= 0 ? 'positive' : 'negative'}">${metricPct(backtest.total_return_pct)}<small>回撤 ${metricPct(backtest.max_drawdown_pct)}</small></td></tr>`;
    }).join('');
    $('#compareTable').innerHTML = `<div class="compare-section-head"><div><p class="eyebrow">RANKING MATRIX</p><h3>横向对比矩阵</h3></div><span>${safe(data.as_of)} · ${data.ranking.length} 只股票</span></div>
      <div class="compare-table-wrap"><table><thead><tr><th>排名</th><th>股票</th><th>综合评分</th><th>价格</th><th>动量</th><th>风险</th><th>估值</th><th>策略回放</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function renderCards(data) {
    $('#compareCards').innerHTML = data.ranking.map((item, index) => {
      const fundamental = item.fundamentals;
      const bars = item.dimensions.map(axis => `<div class="dimension-row"><span>${safe(axis.label)}<small>${axis.weight}%</small></span><div><i style="width:${Math.max(2, axis.score)}%;--bar:${colors[index % colors.length]}"></i></div><strong>${number(axis.score, 0)}</strong></div>`).join('');
      return `<article class="compare-stock-card" style="--stock-color:${colors[index % colors.length]}">
        <header><div><span class="stock-rank">#${item.rank}</span><h3>${safe(item.name)}</h3><p>${safe(item.symbol)} · ${safe(fundamental.industry)}</p></div><div class="card-score"><strong>${number(item.score, 1)}</strong><span>${safe(item.stance)}</span></div></header>
        <div class="stock-facts"><span>总市值<strong>${formatCap(fundamental.market_cap)}</strong></span><span>PE<strong>${number(fundamental.pe_ttm ?? fundamental.pe_dynamic, 1)}</strong></span><span>PB<strong>${number(fundamental.pb, 2)}</strong></span><span>ROE<strong>${pct(fundamental.roe)}</strong></span></div>
        <div class="dimension-list">${bars}</div>
        <div class="stock-risk"><strong>关键风险</strong><ul>${item.red_flags.map(flag => `<li>${safe(flag)}</li>`).join('')}</ul></div>
        <div class="stock-invalidation"><strong>什么会推翻逻辑</strong><p>${safe(item.invalidation)}</p></div>
        <footer><span>数据覆盖置信度 <b>${item.confidence}%</b></span><span>${safe(item.metrics.data_start)} → ${safe(item.metrics.data_end)}</span></footer>
      </article>`;
    }).join('');
  }

  function renderMethod(data) {
    const weights = data.methodology.weights.map(item => `<span>${safe(item.label)} <b>${item.weight}%</b></span>`).join('');
    const gaps = [...new Set(data.ranking.flatMap(item => item.fundamentals.data_gaps || []))];
    $('#compareMethod').innerHTML = `<div><p class="eyebrow">METHODOLOGY</p><h3>${safe(data.profile.label)} · ${safe(data.methodology.horizon)}</h3><div class="method-weights">${weights}</div></div>
      <div class="method-audit"><strong>回测口径</strong><p>${safe(data.methodology.backtest)}</p><strong>当前数据缺口</strong><p>${safe(gaps.join('、') || '无')}</p></div>`;
  }

  function setupCanvas(canvas, height = 360) {
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(320, canvas.clientWidth || 640);
    canvas.width = width * ratio;
    canvas.height = height * ratio;
    canvas.style.height = `${height}px`;
    const context = canvas.getContext('2d');
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    return {context, width, height};
  }

  function drawRadar(data) {
    const canvas = $('#radarChart');
    const {context: ctx, width, height} = setupCanvas(canvas, 360);
    const axes = data.ranking[0].dimensions;
    const cx = width / 2, cy = height / 2 + 8, radius = Math.min(width * 0.29, height * 0.34);
    ctx.clearRect(0, 0, width, height);
    ctx.font = '14px Inter, system-ui, sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    for (let level = 1; level <= 5; level++) {
      ctx.beginPath();
      axes.forEach((_, index) => {
        const angle = -Math.PI / 2 + index * Math.PI * 2 / axes.length;
        const x = cx + Math.cos(angle) * radius * level / 5;
        const y = cy + Math.sin(angle) * radius * level / 5;
        index ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.closePath(); ctx.strokeStyle = '#1e3342'; ctx.stroke();
    }
    axes.forEach((axis, index) => {
      const angle = -Math.PI / 2 + index * Math.PI * 2 / axes.length;
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx + Math.cos(angle) * radius, cy + Math.sin(angle) * radius);
      ctx.strokeStyle = '#1e3342'; ctx.stroke();
      const labelRadius = radius + 34;
      ctx.fillStyle = '#91a8b4'; ctx.fillText(axis.label, cx + Math.cos(angle) * labelRadius, cy + Math.sin(angle) * labelRadius);
    });
    data.ranking.forEach((item, stockIndex) => {
      ctx.beginPath();
      item.dimensions.forEach((axis, index) => {
        const angle = -Math.PI / 2 + index * Math.PI * 2 / axes.length;
        const distance = radius * axis.score / 100;
        const x = cx + Math.cos(angle) * distance, y = cy + Math.sin(angle) * distance;
        index ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.closePath(); ctx.strokeStyle = colors[stockIndex % colors.length]; ctx.lineWidth = stockIndex === 0 ? 2.5 : 1.5;
      ctx.fillStyle = `${colors[stockIndex % colors.length]}18`; ctx.fill(); ctx.stroke();
    });
  }

  function drawCurves(data) {
    const canvas = $('#backtestChart');
    const {context: ctx, width, height} = setupCanvas(canvas, 360);
    const padding = {left: 54, right: 18, top: 18, bottom: 34};
    const all = data.ranking.flatMap(item => item.backtest.curve.map(point => point.strategy));
    const min = Math.min(...all) * 0.96, max = Math.max(...all) * 1.04;
    const plotWidth = width - padding.left - padding.right, plotHeight = height - padding.top - padding.bottom;
    ctx.clearRect(0, 0, width, height); ctx.font = '13px Inter, system-ui, sans-serif';
    for (let line = 0; line <= 4; line++) {
      const y = padding.top + plotHeight * line / 4;
      ctx.beginPath(); ctx.moveTo(padding.left, y); ctx.lineTo(width - padding.right, y);
      ctx.strokeStyle = '#1e3342'; ctx.stroke();
      const value = max - (max - min) * line / 4;
      ctx.fillStyle = '#7f98a6'; ctx.textAlign = 'right'; ctx.fillText(number(value / 1000, 0) + 'k', padding.left - 8, y + 4);
    }
    data.ranking.forEach((item, stockIndex) => {
      const points = item.backtest.curve;
      ctx.beginPath();
      points.forEach((point, index) => {
        const x = padding.left + plotWidth * index / Math.max(1, points.length - 1);
        const y = padding.top + (max - point.strategy) / Math.max(1, max - min) * plotHeight;
        index ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.strokeStyle = colors[stockIndex % colors.length]; ctx.lineWidth = stockIndex === 0 ? 2.5 : 1.5; ctx.stroke();
    });
    const first = data.ranking[0].backtest.curve;
    ctx.fillStyle = '#7f98a6'; ctx.textAlign = 'left'; ctx.fillText(first[0].date, padding.left, height - 10);
    ctx.textAlign = 'right'; ctx.fillText(first[first.length - 1].date, width - padding.right, height - 10);
  }

  function render(data) {
    lastResult = data;
    $('#compareResults').hidden = false;
    $('#compareAsOf').textContent = `数据截至 ${data.as_of}`;
    $('#radarLegend').innerHTML = legend(data.ranking);
    $('#curveLegend').innerHTML = legend(data.ranking);
    renderVerdict(data); renderTable(data); renderCards(data); renderMethod(data);
    drawRadar(data); drawCurves(data);
    const historyState = data.refresh?.history_refresh?.status === 'STARTED' ? ' · 分钟历史后台回填中' : '';
    setStatus(`${data.profile.label}对比完成 · ${data.ranking.length} 只股票${historyState} · 仅用于研究`, 'success');
  }

  async function runComparison(forceRefresh = false) {
    const stocks = $('#compareStocks').value.trim();
    const profile = selectedProfile();
    if (!stocks) { setStatus('请输入至少两只股票', 'error'); return; }
    const button = $('#compareSubmit');
    button.disabled = true; button.innerHTML = '<span class="compare-spinner"></span> 正在分析';
    setStatus('正在解析股票、刷新公开行情并执行策略回放…', 'loading');
    try {
      const data = await window.argusRequest('/api/stock-comparison', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({stocks, profile, refresh:forceRefresh})});
      const url = new URL(location.href); url.searchParams.set('view', 'compare'); url.searchParams.set('stocks', stocks); url.searchParams.set('profile', profile);
      history.replaceState(null, '', url);
      render(data);
      window.dispatchEvent(new CustomEvent('argus:comparison-completed', {detail:{ranking:data.ranking}}));
    } catch (error) {
      $('#compareResults').hidden = true;
      setStatus(`对比失败：${error.message}`, 'error');
    } finally {
      button.disabled = false; button.innerHTML = '<span aria-hidden="true">↗</span> 开始对比';
    }
  }

  $('#compareForm').addEventListener('submit', event => { event.preventDefault(); runComparison(false); });
  window.addEventListener('resize', () => { if (lastResult) { drawRadar(lastResult); drawCurves(lastResult); } });
  const params = new URLSearchParams(location.search);
  const profile = params.get('profile');
  if (profile && document.querySelector(`input[name="profile"][value="${CSS.escape(profile)}"]`)) document.querySelector(`input[name="profile"][value="${CSS.escape(profile)}"]`).checked = true;
  const stocks = params.get('stocks');
  if (stocks) { $('#compareStocks').value = stocks; runComparison(false); }
})();
