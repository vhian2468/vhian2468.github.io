(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const profile = MANUAL_PROFILE;
  const defaultUrl = typeof MANUAL_DEFAULT_GAS_URL === 'string' ? MANUAL_DEFAULT_GAS_URL : '';
  const urlKey = 'tiktok-gas-url:'+profile+':'+defaultUrl;
  const fields = ['repost_date','repost_caption'];
  const empty = () => ({records:{},outbox:[],history:[],drafts:{},cursor:0,lastDate:''});
  let db, state = empty(), videos = new Map(), baseline = new Map(), ready = false, busy = false;
  let activeId = null, opening = {}, dateTouched = false, saveBusy = false, draftTimer, gasUrl = '', token = '';
  let bridge, peer, peerOrigin, channel, bridgeReady, connectReject, bridgeTimer, lastError = '';
  const requests = new Map();
  const uid = () => crypto.randomUUID();
  const textValues = value => Object.fromEntries(fields.map(k => [k,String(value[k] ?? '')]));
  const status = document.createElement('div'); status.id = 'manualSyncStatus'; status.setAttribute('role','status');
  const group = document.createElement('div'); group.className = 'nav-group';
  group.innerHTML = '<button id="manualEditToggle" aria-pressed="false" disabled>✏️ Edit</button><button id="manualSyncNow">Đồng bộ</button><button id="manualConfigure">⚙️ Lưu/Sync</button>';
  document.querySelector('.controls .nav-bar:last-child').appendChild(group);
  document.querySelector('.controls').appendChild(status);
  function showStatus() {
    const pending = state.outbox.length, conflicts = state.outbox.filter(v => v.status === 'conflict').length;
    status.textContent = !ready ? 'Chỉnh sửa chưa sẵn sàng' : (busy ? 'Đang đồng bộ GAS… ' : '') +
      (pending ? `Đã lưu trên thiết bị · ${pending} thay đổi chờ đồng bộ` : (gasUrl && token ? 'Không có thay đổi local đang chờ' : 'Lưu local · chưa kết nối GAS')) +
      (conflicts ? ` · ${conflicts} xung đột: mở ✏️ để xử lý` : '') + (lastError ? ' · ' + lastError : '');
  }
  function openDB() {
    return new Promise((resolve,reject) => {
      const request = indexedDB.open('tiktok-manual-edits-v1',1);
      request.onupgradeneeded = () => request.result.createObjectStore('profiles');
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result);
    });
  }
  function readState() {
    return new Promise((resolve,reject) => {
      const tx = db.transaction('profiles','readonly');
      const request = tx.objectStore('profiles').get(profile);
      request.onsuccess = () => resolve(request.result || empty());
      request.onerror = () => reject(request.error);
    });
  }
  // One read-modify-write IDB transaction also serializes edits from other tabs.
  function mutate(change) {
    return new Promise((resolve,reject) => {
      const tx = db.transaction('profiles','readwrite'), store = tx.objectStore('profiles');
      let next;
      const request = store.get(profile);
      request.onsuccess = () => {
        try { next = request.result || empty(); change(next); store.put(next,profile); }
        catch(error) { tx.abort(); reject(error); }
      };
      tx.oncomplete = () => { state = next; applyLocal(); showStatus(); resolve(); };
      tx.onabort = tx.onerror = () => reject(tx.error || new Error('Không lưu được IndexedDB'));
    });
  }
  function effective(id) {
    const result = {...(state.records[id]?.fields || {})};
    state.outbox.filter(op => op.video_id === id).forEach(op => Object.assign(result,op.patch));
    return result;
  }
  function applyLocal() {
    videos.forEach((video,id) => Object.assign(video,baseline.get(id),effective(id)));
    // Replace only cards already on screen; never re-sort/filter on a save.
    const top = scrollArea.scrollTop;
    document.querySelectorAll('.card[id^="card-"]').forEach(card => {
      const video = videos.get(card.id.slice(5));
      if(video) card.replaceWith(createCard(video));
    });
    scrollArea.scrollTop = top;
    if(typeof renderSidebar === 'function') renderSidebar();
  }
  async function init(data) {
    videos = new Map(data.map(v => [String(v.id),v]));
    baseline = new Map(data.map(v => [String(v.id),textValues(v)]));
    try {
      db = await openDB(); state = await readState(); ready = true;
      gasUrl = localStorage.getItem(urlKey) || defaultUrl || localStorage.getItem('tiktok-gas-url') || '';
      token = sessionStorage.getItem('tiktok-gas-token') || '';
      $('manualGasUrl').value = gasUrl; $('manualGasToken').value = token;
      $('manualEditToggle').disabled = false; applyLocal();
    } catch(error) { lastError = 'Không mở được lưu trữ: '+error.message; }
    showStatus();
    if(ready && gasUrl && token) sync();
  }
  function addButton(card,video) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'manual-edit-button';
    button.textContent = '✏️'; button.setAttribute('aria-label','Sửa repost '+video.id);
    button.onclick = event => { event.preventDefault(); event.stopPropagation(); openEditor(String(video.id)); };
    card.appendChild(button); // Sibling of the thumbnail link, never nested in <a>.
  }
  function openEditor(id) {
    if(!ready || !videos.has(id)) return;
    activeId = id; opening = textValues(videos.get(id));
    const draft = state.drafts[id];
    dateTouched = !!draft?.dateTouched;
    $('manualIdentity').textContent = '@'+profile+' · ID '+id+' · '+(videos.get(id).nickname || '');
    $('manualDate').value = draft?.repost_date ?? opening.repost_date;
    $('manualCaption').value = draft?.repost_caption ?? opening.repost_caption;
    $('manualFormMessage').textContent = draft ? 'Đã khôi phục bản nháp trên thiết bị.' : '';
    const conflict = state.outbox.find(op => op.video_id === id && op.status === 'conflict');
    $('manualConflictActions').hidden = !conflict;
    $('manualConflict').textContent = conflict ? 'Server đã có bản khác:\n'+JSON.stringify(conflict.current?.fields || {},null,2)+'\nChọn bản muốn giữ; không có bản nào tự ghi đè.' : '';
    if(!$('manualDialog').open) $('manualDialog').showModal();
  }
  function captureDraft() {
    clearTimeout(draftTimer);
    const id = activeId, draft = {repost_date:dateTouched ? $('manualDate').value : opening.repost_date,repost_caption:$('manualCaption').value,dateTouched};
    draftTimer = setTimeout(() => mutate(s => { s.drafts[id] = draft; }).catch(reportError),200);
  }
  async function closeEditor() {
    clearTimeout(draftTimer);
    if(activeId && ready) {
      const id = activeId, draft = {repost_date:dateTouched ? $('manualDate').value : opening.repost_date,repost_caption:$('manualCaption').value,dateTouched};
      try { await mutate(s => { s.drafts[id] = draft; }); }
      catch(error) { $('manualFormMessage').textContent = 'Không giữ được nháp: '+error.message; return; }
    }
    $('manualDialog').close();
  }
  function reportError(error) { lastError = error.message || String(error); showStatus(); }
  async function saveEditor(next=false) {
    if(saveBusy) return; saveBusy = true;
    clearTimeout(draftTimer);
    try {
      if(!$('manualDate').checkValidity()) throw new Error('Ngày không hợp lệ');
      const id = activeId, values = {repost_date:dateTouched ? $('manualDate').value : opening.repost_date,repost_caption:$('manualCaption').value};
      const patch = Object.fromEntries(fields.filter(k => values[k] !== opening[k]).map(k => [k,values[k]]));
      if(values.repost_caption.length > 5000) throw new Error('Caption tối đa 5000 ký tự');
      await mutate(s => {
        if(s.outbox.some(op => op.video_id === id && op.status === 'conflict')) throw new Error('Hãy xử lý xung đột trước khi lưu tiếp.');
        if(Object.keys(patch).length) {
          const prior = s.outbox.filter(op => op.video_id === id).at(-1);
          const op = {op_id:uid(),profile,video_id:id,patch,base_values:{...opening},
                      base_revision:s.records[id]?.revision || 0,after_op:prior?.op_id || '',created_at:new Date().toISOString(),status:'pending'};
          s.outbox.push(op); s.history.push({...op,event:'local_save'});
        }
        delete s.drafts[id]; if(values.repost_date) s.lastDate = values.repost_date;
      });
      $('manualDialog').close(); lastError = ''; showStatus();
      if(next) { const index = displayData.findIndex(v => String(v.id) === id); if(index >= 0 && index+1 < displayData.length) openEditor(String(displayData[index+1].id)); }
      sync();
    } catch(error) { $('manualFormMessage').textContent = 'Chưa lưu: '+error.message; }
    finally { saveBusy = false; }
  }
  function shiftDay(amount) {
    const value = $('manualDate').value; if(!value) return;
    const date = new Date(value+'T12:00:00Z'); date.setUTCDate(date.getUTCDate()+amount);
    $('manualDate').value = date.toISOString().slice(0,10); dateTouched = true; captureDraft();
  }
  async function resolveConflict(keepLocal) {
    const id = activeId;
    await mutate(s => {
      const ops = s.outbox.filter(op => op.video_id === id);
      const conflict = ops.find(op => op.status === 'conflict'); if(!conflict) return;
      const patch = Object.assign({},...ops.map(op => op.patch));
      s.outbox = s.outbox.filter(op => op.video_id !== id);
      s.history.push({event:keepLocal?'resolve_local':'resolve_server',video_id:id,operations:ops,at:new Date().toISOString()});
      s.records[id] = conflict.current || {fields:{},revision:0};
      if(keepLocal) s.outbox.push({op_id:uid(),profile,video_id:id,patch,base_revision:s.records[id].revision,after_op:'',base_values:s.records[id].fields,status:'pending',created_at:new Date().toISOString()});
      delete s.drafts[id];
    });
    openEditor(id); sync();
  }
  function disconnect() {
    clearTimeout(bridgeTimer); connectReject?.(new Error('Đã ngắt kết nối')); connectReject = null;
    token = ''; sessionStorage.removeItem('tiktok-gas-token'); peer = null; bridgeReady = null;
    if(bridge) bridge.remove(); bridge = null;
    requests.forEach(p => { clearTimeout(p.timer); p.reject(new Error('Đã ngắt kết nối')); }); requests.clear();
    showStatus();
  }
  function connectBridge() {
    if(bridgeReady) return bridgeReady;
    bridgeReady = new Promise((resolve,reject) => {
      const url = new URL(gasUrl);
      if(location.origin === 'null') { reject(new Error('GAS cần mở trang qua HTTPS hoặc localhost; file offline vẫn sửa/lưu local được.')); return; }
      if(url.protocol !== 'https:' || url.hostname !== 'script.google.com' || !url.pathname.endsWith('/exec')) { reject(new Error('URL GAS không hợp lệ')); return; }
      channel = uid(); url.searchParams.set('bridge','1'); url.searchParams.set('channel',channel); url.searchParams.set('parent',location.origin);
      bridge = document.createElement('iframe'); bridge.hidden = true; bridge.title = 'GAS sync bridge'; bridge.src = url.href;
      const ownedFrame = bridge;
      bridgeTimer = setTimeout(() => { bridgeReady = null; ownedFrame.remove(); peer = null; connectReject = null; reject(new Error('GAS bridge chưa sẵn sàng; kiểm tra deployment và ALLOWED_ORIGINS')); },30000);
      connectReject = reject;
      bridge._ready = () => { clearTimeout(bridgeTimer); connectReject = null; resolve(); };
      document.body.appendChild(bridge);
    });
    return bridgeReady;
  }
  window.addEventListener('message', event => {
    const message = event.data;
    if(!message || message.channel !== channel) return;
    const host = (()=>{ try{return new URL(event.origin).hostname;}catch{return '';} })();
    if(event.origin !== 'https://script.google.com' && !(host === 'script.googleusercontent.com' || host.endsWith('-script.googleusercontent.com'))) return;
    if(message.type === 'manual-bridge-ready' && !peer) { peer = event.source; peerOrigin = event.origin; bridge?._ready(); return; }
    if(event.source !== peer || event.origin !== peerOrigin || message.type !== 'manual-rpc-response') return;
    const request = requests.get(message.id); if(!request) return;
    clearTimeout(request.timer); requests.delete(message.id);
    if(message.error) request.reject(new Error(message.error)); else request.resolve(message.result);
  });
  async function rpc(payload) {
    await connectBridge();
    return new Promise((resolve,reject) => {
      const id = uid(), timer = setTimeout(() => { requests.delete(id); reject(new Error('GAS chậm/chưa phản hồi; thay đổi vẫn còn local')); },45000);
      requests.set(id,{resolve,reject,timer}); peer.postMessage({type:'manual-rpc',channel,id,payload:{...payload,token}},peerOrigin);
    });
  }
  async function sync() {
    if(!ready || busy || !token || !gasUrl || !navigator.onLine) { showStatus(); return; }
    busy = true; showStatus();
    try {
      state = await readState();
      const blocked = new Set(state.outbox.filter(op => op.status === 'conflict').map(op => op.video_id));
      const ops = state.outbox.filter(op => !blocked.has(op.video_id)).slice(0,20);
      const response = await rpc({action:'sync',profile,cursor:state.cursor,ops});
      if(!response?.ok) throw new Error(response?.error || 'Phản hồi GAS không hợp lệ');
      await mutate(s => {
        for(const item of response.changes || []) if((s.records[item.video_id]?.revision || 0) <= item.revision) s.records[item.video_id] = item;
        for(const ack of response.acks || []) {
          const op = s.outbox.find(v => v.op_id === ack.op_id); if(!op) continue;
          if(ack.status === 'ok') {
            if(!ack.state || ack.state.video_id !== op.video_id) throw new Error('GAS chưa trả bản đã lưu; giữ lại hàng đợi');
            if((s.records[op.video_id]?.revision || 0) <= ack.state.revision) s.records[op.video_id] = ack.state;
            s.history.push({event:'server_ack',op_id:op.op_id,revision:ack.revision}); s.outbox = s.outbox.filter(v => v.op_id !== op.op_id);
          }
          else { op.status = 'conflict'; op.current = ack.current; }
        }
        s.cursor = response.cursor;
      });
      lastError = '';
      if(response.has_more || state.outbox.some(op => !blocked.has(op.video_id) && op.status !== 'conflict')) setTimeout(sync,500);
    } catch(error) { lastError = error.message; }
    finally { busy = false; showStatus(); }
  }
  async function exportBackup() {
    if(!ready) return;
    state = await readState();
    const ids = new Set([...Object.keys(state.records),...state.outbox.map(op => op.video_id)]);
    const changes = [...ids].map(id => ({video_id:id,patch:effective(id),base_values:state.records[id]?.base_values || baseline.get(id) || {},revision:state.records[id]?.revision || 0,
      pending_fields:[...new Set(state.outbox.filter(op=>op.video_id===id).flatMap(op=>Object.keys(op.patch)))]}));
    const document = {format:'tiktok-manual-edits-v1',profile,exported_at:new Date().toISOString(),changes,local_state:state};
    const blob = new Blob([JSON.stringify(document,null,2)],{type:'application/json'}), url = URL.createObjectURL(blob);
    const link = window.document.createElement('a'); link.href = url; link.download = 'manual-edits-'+profile+'-'+Date.now()+'.json'; link.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  $('manualEditToggle').onclick = () => { const active = document.body.classList.toggle('manual-edit-mode'); $('manualEditToggle').setAttribute('aria-pressed',String(active)); };
  $('manualConfigure').onclick = () => $('manualSettings').showModal();
  $('manualSyncNow').onclick = () => { if(!token || !gasUrl) $('manualSettings').showModal(); else sync(); };
  $('manualForm').onsubmit = event => { event.preventDefault(); saveEditor(); };
  $('manualSaveNext').onclick = () => saveEditor(true);
  $('manualDate').oninput = () => { dateTouched = true; captureDraft(); }; $('manualCaption').oninput = captureDraft;
  $('manualPreviousDay').onclick = () => shiftDay(-1); $('manualNextDay').onclick = () => shiftDay(1);
  $('manualLastDate').onclick = () => { if(state.lastDate) { $('manualDate').value = state.lastDate; dateTouched = true; captureDraft(); } };
  $('manualKeepLocal').onclick = () => resolveConflict(true).catch(reportError);
  $('manualUseServer').onclick = () => resolveConflict(false).catch(reportError);
  document.querySelectorAll('[data-close]').forEach(button => button.onclick = () => button.dataset.close === 'manualDialog' ? closeEditor() : $(button.dataset.close).close());
  $('manualDialog').addEventListener('cancel',event => { event.preventDefault(); closeEditor(); });
  $('manualDisconnect').onclick = disconnect; $('manualExport').onclick = () => exportBackup().catch(reportError);
  function saveUrl() {
    const value = $('manualGasUrl').value.trim();
    if(value && !/^https:\/\/script\.google\.com\/macros\/s\/[A-Za-z0-9_-]+\/exec$/.test(value)) throw new Error('Cần URL GAS /exec, không kèm token hay tham số.');
    localStorage.setItem(urlKey,value);
    if(value !== gasUrl) disconnect();
    gasUrl = value;
    showStatus();
  }
  $('manualSaveUrl').onclick = () => {
    try { saveUrl(); $('manualSettingsMessage').textContent = 'Đã nhớ URL API trên thiết bị. Các lần mở sau không cần dán lại. Token vẫn nhập riêng khi kết nối.'; }
    catch(error) { $('manualSettingsMessage').textContent = 'Chưa lưu URL: '+error.message; }
  };
  $('manualSettingsForm').onsubmit = event => {
    event.preventDefault();
    try { saveUrl(); disconnect(); token = $('manualGasToken').value.trim(); sessionStorage.setItem('tiktok-gas-token',token); }
    catch(error) { $('manualSettingsMessage').textContent = 'Chưa kết nối: '+error.message; return; }
    $('manualSettingsMessage').textContent = 'Đang thử kết nối; xem trạng thái trên thanh điều hướng.'; sync();
  };
  window.addEventListener('online',sync);
  window.addEventListener('focus',async () => { if(ready) { state = await readState(); applyLocal(); showStatus(); sync(); } });
  window.addEventListener('beforeunload',event => { if(state.outbox.length) { event.preventDefault(); event.returnValue = ''; } });
  setInterval(() => { if(!document.hidden) sync(); },30000);
  window.tiktokManualEditor = {init,addButton};
  showStatus();
})();
