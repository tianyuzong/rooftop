(() => {
  const el = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const post = (url, value) => window.argusRequest(url, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});
  let subscriptions = [], current = null, initialized = false, smtpInitialized = false;
  const kindLabel = kind => ({MARKET:'大盘总结',COMPANIES:'关注公司',BUY:'买入',SELL:'卖出',REBALANCE:'调仓'}[kind] || kind);
  const message = text => {el('mailActionMessage').textContent=text;};
  const selected = name => [...document.querySelectorAll(`input[name="${name}"]:checked`)].map(e=>e.value);
  function draft() {
    return {id:el('subscriptionId').value?Number(el('subscriptionId').value):undefined,
      name:el('subscriptionName').value,target:el('subscriptionTarget').value,
      digest_kinds:selected('digestKind'),watch_stocks:el('subscriptionStocks').value,
      event_kinds:el('includeSignals').checked?selected('signalEvent'):[],
      minimum_confidence:Number(el('subscriptionConfidence').value)/100,
      send_time:el('subscriptionTime').value,report_date:el('digestDate').value};
  }
  function toggleFields() {
    el('watchStocksField').hidden=!selected('digestKind').includes('COMPANIES');
    el('signalOptions').hidden=!el('includeSignals').checked;
  }
  function editSubscription(item) {
    current=item;el('subscriptionId').value=item?.id||'';
    el('subscriptionName').value=item?.name||'我的投资日报';
    el('subscriptionTarget').value=item?.target||subscriptions[0]?.target||'';
    el('subscriptionStocks').value=(item?.watch_symbols||[]).join('，');
    el('subscriptionTime').value=item?.send_time||'08:30';
    el('subscriptionConfidence').value=(item?.minimum_confidence||0)*100;
    const kinds=item?.digest_kinds||['MARKET'];
    document.querySelectorAll('[name="digestKind"]').forEach(e=>e.checked=kinds.includes(e.value));
    el('includeSignals').checked=Boolean(item?.event_kinds?.length);
    document.querySelectorAll('[name="signalEvent"]').forEach(e=>e.checked=(item?.event_kinds||['BUY','SELL','REBALANCE']).includes(e.value));
    toggleFields();
  }
  window.renderMailCenter = data => {
    subscriptions=data.subscriptions?.subscriptions||[];
    const smtp=data.subscriptions?.smtp||{}, rows=data.outbox?.rows||[];
    el('mailDeliveryStatus').textContent=!smtp.configured?'尚未配置发件服务：订阅已保存也不会发出邮件。':!smtp.send_enabled?'发件服务已配置，发送开关尚未打开。':'邮件发送已启用。请查看发送记录，并确认收件箱实际收到。';
    el('mailDeliveryStatus').classList.toggle('ready',smtp.configured&&smtp.send_enabled);
    if(!initialized){editSubscription(subscriptions.find(s=>s.enabled)||null);initialized=true;}
    if(!smtpInitialized){
      el('smtpUser').value=smtp.user||subscriptions[0]?.target||'';
      el('smtpHost').value=smtp.host||'smtp.163.com';el('smtpPort').value=smtp.port||465;
      el('smtpSecurity').value=smtp.security||'ssl';el('smtpEnabled').checked=smtp.configured?Boolean(smtp.send_enabled):true;
      smtpInitialized=true;
    }
    el('subscriptionList').innerHTML=subscriptions.map(item=>`<div class="mail-subscription"><strong>${esc(item.name)}</strong><span>${esc(item.target)} · ${item.enabled?'已启用':'已停用'}</span><span>${[...(item.digest_kinds||[]),...(item.event_kinds||[])].map(kindLabel).map(esc).join(' / ')} · 每日 ${esc(item.send_time)} 北京时间</span>${item.watch_symbols?.length?`<small>关注：${esc(item.watch_symbols.join('、'))}</small>`:''}<div class="mail-actions"><button type="button" class="secondary-button" data-mail-edit="${item.id}">编辑</button><button type="button" class="secondary-button" data-subscription-toggle="${item.id}" data-enabled="${item.enabled}">${item.enabled?'停用':'启用'}</button><button type="button" class="secondary-button" data-mail-test="${item.id}">测试收件</button></div></div>`).join('')+ '<button id="mailNewSubscription" class="secondary-button" type="button">新增订阅</button>';
    document.querySelectorAll('[data-mail-edit]').forEach(b=>b.onclick=()=>{editSubscription(subscriptions.find(s=>s.id===Number(b.dataset.mailEdit)));el('subscriptionName').focus();});
    el('mailNewSubscription').onclick=()=>{editSubscription(null);el('subscriptionName').focus();};
    document.querySelectorAll('[data-mail-test]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const r=await post('/api/notifications/test',{subscription_id:Number(b.dataset.mailTest)});message(r.delivery?.sent?'发信服务器已接受测试邮件；请检查收件箱和垃圾邮件。':'测试未发送成功，请查看发送记录。');await loadSignals();}catch(e){message(e.message);}finally{b.disabled=false;}});
    const label=row=>row.received_at?'已确认收到':({PENDING:'等待发送',RETRY:'等待重试',FAILED:'发送失败',SENT:'服务器已接受，待确认收到',CANCELLED:'已取消'}[row.status]||row.status);
    el('mailHistory').innerHTML=rows.length?rows.slice(0,12).map(row=>`<article class="mail-log"><div><strong>${esc(row.subject)}</strong><span>${esc(row.target||'')} · ${esc(label(row))}</span><small>${esc(row.sent_at?localTimestamp(row.sent_at):localTimestamp(row.created_at))}</small>${row.error?`<p>${esc(row.error)}</p>`:''}</div>${row.status==='SENT'&&!row.received_at?`<button type="button" class="secondary-button" data-mail-received="${row.id}">我已收到</button>`:''}${['FAILED','RETRY'].includes(row.status)?`<button type="button" class="secondary-button" data-mail-retry="${row.id}">重试</button>`:''}</article>`).join(''):'<p class="mail-muted">尚未发送邮件。</p>';
    document.querySelectorAll('[data-mail-received]').forEach(b=>b.onclick=async()=>{await post(`/api/notifications/${b.dataset.mailReceived}/received`,{});await loadSignals();});
    document.querySelectorAll('[data-mail-retry]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{await post(`/api/notifications/${b.dataset.mailRetry}/retry`,{});await loadSignals();}catch(e){message(e.message);}finally{b.disabled=false;}});
  };
  window.bindMailForms = () => {
    const today=new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
    const yesterday=new Date(`${today}T12:00:00Z`);yesterday.setUTCDate(yesterday.getUTCDate()-1);
    el('digestDate').value=yesterday.toISOString().slice(0,10);el('digestDate').max=today;
    document.querySelectorAll('[name="digestKind"],#includeSignals').forEach(e=>e.onchange=toggleFields);
    el('subscriptionForm').onsubmit=async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;try{const result=await post('/api/notification-subscriptions',draft());el('subscriptionId').value=result.id;el('subscriptionStocks').value=(result.watch_symbols||[]).join('，');message('订阅已保存，发件服务和投递状态请见下方。');await loadSignals();}catch(error){message(error.message);}finally{b.disabled=false;}};
    el('digestPreviewButton').onclick=async e=>{const b=e.currentTarget;b.disabled=true;try{const result=await post('/api/digests/preview',draft());el('digestPreviewText').textContent=result.body;el('digestPreview').hidden=false;el('digestPreview').scrollIntoView({behavior:'smooth',block:'start'});message('已生成预览，尚未发送。');}catch(error){message(error.message);}finally{b.disabled=false;}};
    el('digestSendButton').onclick=async e=>{const b=e.currentTarget;b.disabled=true;try{
      const input=draft();const saved=subscriptions.find(s=>s.id===input.id);
      if(!saved)throw new Error('请先保存订阅');
      if(input.target!==saved.target||input.watch_stocks!==(saved.watch_symbols||[]).join('，')||JSON.stringify(input.digest_kinds.slice().sort())!==JSON.stringify((saved.digest_kinds||[]).slice().sort()))throw new Error('内容已修改，请先保存订阅，再发送');
      const result=await post('/api/digests/send',{subscription_id:saved.id,report_date:input.report_date});
      message(result.delivery?.sent?'发信服务器已接受日报，请在收件后点击“我已收到”。':result.message?.status==='SENT'?'这一天的日报已发送，未重复发送。':'请查看发送记录了解投递状态。');await loadSignals();
    }catch(error){message(error.message);}finally{b.disabled=false;}};
    el('smtpSecurity').onchange=()=>{el('smtpPort').value=el('smtpSecurity').value==='ssl'?465:587;};
    el('smtpForm').onsubmit=async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;el('smtpMessage').textContent='正在保存并验证…';try{
      await post('/api/email-settings',{user:el('smtpUser').value,host:el('smtpHost').value,port:Number(el('smtpPort').value),security:el('smtpSecurity').value,password:el('smtpPassword').value,enabled:el('smtpEnabled').checked,default_target:el('subscriptionTarget').value});
      el('smtpPassword').value='';const result=await post('/api/email-settings/verify',{});el('smtpMessage').textContent=result.detail;await loadSignals();
    }catch(error){el('smtpMessage').textContent=error.message;}finally{b.disabled=false;}};
  };
})();
