(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const profile = MANUAL_PROFILE;
  const defaultUrl = typeof MANUAL_DEFAULT_GAS_URL === 'string' ? MANUAL_DEFAULT_GAS_URL : '';
  const urlKey = 'tiktok-gas-url:'+profile+':'+defaultUrl;
  const tokenKey = () => 'tiktok-gas-token:'+profile+':'+gasUrl;
  const rememberKey = 'tiktok-remember-token:'+profile;
  const legacyFields = ['repost_date','repost_caption'];
  const publicFields = [...legacyFields,'repost_time','tags'];
  const privateFields = ['notes'];
  const fields = [...publicFields,...privateFields];
  const empty = () => ({records:{},outbox:[],history:[],drafts:{},cursor:0,lastDate:''});
  let db, state = empty(), videos = new Map(), baseline = new Map(), appliedOverlay = new Map(), ready = false, busy = false;
  let activeId = null, opening = {}, dateTouched = false, timeTouched = false, noteWriteAllowed = false, saveBusy = false, draftTimer, gasUrl = '', token = '';
  let bridge, peer, peerOrigin, channel, bridgeReady, connectReject, bridgeTimer, lastError = '';
  let bridgeCapabilities = null, privateUnlocked = false;
  const requests = new Map();
  const uid = () => crypto.randomUUID();
  const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  const canonical = value => JSON.stringify(value === undefined ? null : value);
  const defaults = () => ({repost_date:'',repost_time:null,repost_caption:'',tags:[],notes:''});
  // Public JS may contain the converged current time/labels selected by the
  // exporter. Notes, revision heads, conflicts and history never enter it.
  const publicBaseline = value => ({...defaults(),repost_date:String(value.repost_date ?? ''),
    repost_time:clone(value.repost_time ?? null),repost_caption:String(value.repost_caption ?? ''),
    tags:Array.isArray(value.tags) ? value.tags.map(String) : []});
  const fieldValues = value => {
    const result = defaults();
    for(const key of fields) if(value && Object.prototype.hasOwnProperty.call(value,key)) result[key] = clone(value[key]);
    result.repost_date = String(result.repost_date ?? ''); result.repost_caption = String(result.repost_caption ?? '');
    result.tags = Array.isArray(result.tags) ? result.tags.map(String) : [];
    result.notes = String(result.notes ?? ''); result.repost_time = result.repost_time ?? null;
    return result;
  };
  const withPublicBaseline = (id,value) => fieldValues({
    ...clone(baseline.get(String(id)) || defaults()),...clone(value || {})
  });
  const hasStoredNote = id => Object.prototype.hasOwnProperty.call(state.records[String(id)]?.fields || {},'notes') ||
    state.outbox.some(op => String(op.video_id) === String(id) && Object.prototype.hasOwnProperty.call(op.patch || {},'notes')) ||
    Object.prototype.hasOwnProperty.call(state.drafts[String(id)] || {},'notes');
  const status = document.createElement('div'); status.id = 'manualSyncStatus'; status.setAttribute('role','status');
  const group = document.createElement('div'); group.className = 'nav-group';
  group.innerHTML = '<button id="manualEditToggle" aria-pressed="false" disabled>✏️ Edit</button><button id="manualSyncNow">Đồng bộ</button><button id="manualConfigure">⚙️ Lưu/Sync</button>';
  document.querySelector('.controls .nav-bar:last-child').appendChild(group);
  document.querySelector('.controls').appendChild(status);
  function showStatus() {
    const pending = state.outbox.length, conflicts = state.outbox.filter(v => v.status === 'conflict').length;
    const blockedPending = bridgeCapabilities ? state.outbox.filter(op => Object.keys(op.patch || {}).some(key => !bridgeCapabilities.fields.includes(key))).length : 0;
    const bridgeLabel = bridgeCapabilities?.manual_field_schema === 1 ?
      (privateUnlocked ? 'GAS đã xác thực · ghi chú riêng đã mở' : 'bridge mới · ghi chú server đang khóa') :
      (bridgeCapabilities ? 'bridge cũ: ngày/caption' : 'chưa kiểm tra bridge');
    status.textContent = !ready ? 'Chỉnh sửa chưa sẵn sàng' : (busy ? 'Đang đồng bộ GAS… ' : '') +
      (pending ? `Đã lưu trên thiết bị · ${pending} thay đổi chờ đồng bộ` : (gasUrl && token ? 'Không có thay đổi local đang chờ' : 'Lưu local · chưa kết nối GAS')) +
      (gasUrl && token ? ' · '+bridgeLabel : '') +
      (blockedPending ? ` · ${blockedPending} bản có trường chưa được server xác nhận, vẫn giữ local` : '') +
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
    const server = clone(state.records[id]?.fields || {});
    if(!privateUnlocked) delete server.notes;
    const result = withPublicBaseline(id,server);
    state.outbox.filter(op => op.video_id === id).forEach(op => {
      const patch = clone(op.patch || {}); if(!privateUnlocked) delete patch.notes;
      Object.assign(result,patch);
    });
    return result;
  }
  function effectiveMap() {
    const result = new Map();
    Object.entries(state.records || {}).forEach(([id,record]) => {
      if(record?.fields) {
        const server = clone(record.fields); if(!privateUnlocked) delete server.notes;
        result.set(String(id),withPublicBaseline(id,server));
      }
    });
    for(const op of state.outbox || []) {
      const id = String(op.video_id);
      const patch = clone(op.patch || {}); if(!privateUnlocked) delete patch.notes;
      result.set(id,Object.assign(result.get(id) || withPublicBaseline(id,{}),patch));
    }
    return result;
  }
  function applyLocal() {
    const nextOverlay = effectiveMap();
    const ids = new Set([...appliedOverlay.keys(),...nextOverlay.keys()]);
    const changed = new Set(); let sidebarDirty = false;
    for(const id of ids) {
      const before = appliedOverlay.get(id) || {};
      const after = nextOverlay.get(id) || {};
      if(fields.every(key => canonical(before[key]) === canonical(after[key]))) continue;
      const video = videos.get(id);
      if(!video) continue;
      if(String(before.repost_date ?? '') !== String(after.repost_date ?? '')) sidebarDirty = true;
      Object.assign(video,clone(baseline.get(id)),clone(after)); changed.add(id);
    }
    appliedOverlay = nextOverlay;
    // Replace only changed cards already on screen; never re-sort/filter on a save.
    const top = scrollArea.scrollTop;
    document.querySelectorAll('.card[id^="card-"]').forEach(card => {
      const id = card.id.slice(5), video = changed.has(id) ? videos.get(id) : null;
      if(video) card.replaceWith(createCard(video));
    });
    scrollArea.scrollTop = top;
    if(typeof refreshSearchIndexForIds === 'function') refreshSearchIndexForIds(changed);
    if(sidebarDirty && typeof renderSidebar === 'function' && displayData.length) renderSidebar();
  }
  async function init(data) {
    videos = new Map(data.map(v => [String(v.id),v]));
    baseline = new Map(data.map(v => [String(v.id),publicBaseline(v)]));
    try {
      db = await openDB(); state = await readState(); ready = true;
      gasUrl = localStorage.getItem(urlKey) || defaultUrl || localStorage.getItem('tiktok-gas-url') || '';
      token = localStorage.getItem(tokenKey()) || sessionStorage.getItem(tokenKey()) || '';
      $('manualRememberToken').checked = localStorage.getItem(rememberKey) !== 'false';
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
    activeId = id; opening = fieldValues(effective(id));
    const draft = state.drafts[id];
    dateTouched = !!draft?.dateTouched;
    timeTouched = !!draft?.timeTouched;
    $('manualIdentity').textContent = '@'+profile+' · ID '+id+' · '+(videos.get(id).nickname || '');
    $('manualDate').value = draft?.repost_date ?? opening.repost_date;
    $('manualCaption').value = draft?.repost_caption ?? opening.repost_caption;
    $('manualTags').value = draft?.tagsText ?? opening.tags.join('\n');
    noteWriteAllowed = privateUnlocked || !hasStoredNote(id);
    $('manualNotes').disabled = !noteWriteAllowed;
    $('manualNotes').placeholder = privateUnlocked ? '' : (noteWriteAllowed ? 'Ghi chú mới sẽ được giữ local và khóa sau khi đóng' : 'Kết nối và xác thực GAS để mở ghi chú riêng');
    $('manualNotes').value = privateUnlocked ? (draft?.notes ?? opening.notes) : '';
    $('manualTime').value = draft?.timeInput ?? timeInput(opening.repost_time);
    showTimeSummary(draft?.repost_time ?? opening.repost_time);
    $('manualFormMessage').textContent = draft ? 'Đã khôi phục bản nháp trên thiết bị.' : '';
    const conflict = state.outbox.find(op => op.video_id === id && op.status === 'conflict');
    $('manualConflictActions').hidden = !conflict;
    $('manualConflict').textContent = conflict ? 'Server đã có bản khác:\n'+JSON.stringify(conflict.current?.fields || {},null,2)+'\nChọn bản muốn giữ; không có bản nào tự ghi đè.' : '';
    if(!$('manualDialog').open) $('manualDialog').showModal();
  }
  function timeInput(value) {
    if(!value || value.kind !== 'exact' || !value.at) return '';
    const date = new Date(value.at); if(!Number.isFinite(date.getTime())) return '';
    const part = number => String(number).padStart(2,'0');
    return `${date.getFullYear()}-${part(date.getMonth()+1)}-${part(date.getDate())}T${part(date.getHours())}:${part(date.getMinutes())}:${part(date.getSeconds())}`;
  }
  function exactTime(value) {
    if(!value) return null;
    if(!/^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?$/.test(value)) throw new Error('Giờ repost không hợp lệ');
    const date = new Date(value), offset = -date.getTimezoneOffset();
    if(!Number.isFinite(date.getTime())) throw new Error('Giờ repost không hợp lệ');
    const sign = offset >= 0 ? '+' : '-', absolute = Math.abs(offset), part = number => String(number).padStart(2,'0');
    const seconds = value.length === 16 ? ':00' : '';
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Bangkok';
    return {kind:'exact',zone,at:value+seconds+sign+part(Math.floor(absolute/60))+':'+part(absolute%60)};
  }
  function showTimeSummary(value) {
    if(!value) { $('manualTimeSummary').textContent = timeTouched ? 'Đã chọn xóa giờ' : 'Chưa có giờ'; return; }
    if(value.kind === 'exact') $('manualTimeSummary').textContent = 'Chính xác · '+value.at+' · '+value.zone;
    else if(value.kind === 'range') $('manualTimeSummary').textContent = 'Khoảng · '+value.lower+' → '+value.upper+' · '+value.zone+' (nhập giờ để thay bằng mốc chính xác)';
    else $('manualTimeSummary').textContent = 'Dữ liệu giờ cũ đang được giữ nguyên';
  }
  function tagsValue(text) {
    const tags = text.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    const unique = [...new Set(tags)];
    if(unique.length > 30) throw new Error('Tối đa 30 nhãn');
    if(unique.some(value => value.length > 60)) throw new Error('Mỗi nhãn tối đa 60 ký tự');
    return unique;
  }
  function draftValues() {
    const savedDraft = state.drafts[activeId] || {};
    return {repost_date:dateTouched ? $('manualDate').value : opening.repost_date,
      repost_caption:$('manualCaption').value,tagsText:$('manualTags').value,
      notes:noteWriteAllowed ? $('manualNotes').value : (savedDraft.notes ?? ''),
      timeInput:$('manualTime').value,repost_time:timeTouched ? exactTime($('manualTime').value) : clone(opening.repost_time),
      dateTouched,timeTouched};
  }
  function editorValues() {
    const draft = draftValues();
    return {repost_date:draft.repost_date,repost_time:draft.repost_time,repost_caption:draft.repost_caption,
      tags:tagsValue(draft.tagsText),notes:draft.notes};
  }
  function captureDraft() {
    clearTimeout(draftTimer);
    const id = activeId; let draft;
    try { draft = draftValues(); } catch(error) { $('manualFormMessage').textContent = error.message; return; }
    draftTimer = setTimeout(() => mutate(s => { s.drafts[id] = draft; }).catch(reportError),200);
  }
  async function closeEditor() {
    clearTimeout(draftTimer);
    if(activeId && ready) {
      const id = activeId; let draft;
      try { draft = draftValues(); } catch(error) { $('manualFormMessage').textContent = 'Không giữ được nháp: '+error.message; return; }
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
      if(!$('manualTime').checkValidity()) throw new Error('Giờ không hợp lệ');
      const id = activeId, values = editorValues();
      const editableFields = noteWriteAllowed ? fields : publicFields;
      const patch = Object.fromEntries(editableFields.filter(k => canonical(values[k]) !== canonical(opening[k])).map(k => [k,clone(values[k])]));
      if(values.repost_caption.length > 5000) throw new Error('Caption tối đa 5000 ký tự');
      if(values.notes.length > 20000) throw new Error('Ghi chú tối đa 20000 ký tự');
      await mutate(s => {
        if(s.outbox.some(op => op.video_id === id && op.status === 'conflict')) throw new Error('Hãy xử lý xung đột trước khi lưu tiếp.');
        if(Object.keys(patch).length) {
          const prior = s.outbox.filter(op => op.video_id === id).at(-1);
          const op = {op_id:uid(),legacy_op_id:uid(),profile,video_id:id,patch,base_values:clone(opening),
                      base_revision:s.records[id]?.revision || 0,after_op:prior?.op_id || '',
                      legacy_after_op:prior?.legacy_op_id || '',created_at:new Date().toISOString(),status:'pending'};
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
      if(keepLocal) s.outbox.push({op_id:uid(),legacy_op_id:uid(),profile,video_id:id,patch,base_revision:s.records[id].revision,
        after_op:'',legacy_after_op:'',base_values:clone(s.records[id].fields),status:'pending',created_at:new Date().toISOString()});
      delete s.drafts[id];
    });
    openEditor(id); sync();
  }
  function disconnect() {
    clearTimeout(bridgeTimer); connectReject?.(new Error('Đã ngắt kết nối')); connectReject = null;
    token = ''; peer = null; bridgeReady = null; bridgeCapabilities = null; privateUnlocked = false;
    if(bridge) bridge.remove(); bridge = null;
    requests.forEach(p => { clearTimeout(p.timer); p.reject(new Error('Đã ngắt kết nối')); }); requests.clear();
    applyLocal();
    if(activeId && $('manualDialog').open) openEditor(activeId);
    if(ready) applyLocal();
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
  async function negotiateCapabilities() {
    if(bridgeCapabilities) return bridgeCapabilities;
    const response = await rpc({action:'capabilities',profile,workspace:'main',client_manual_field_schema:1,client_private_read_schema:1});
    if(response?.ok) {
      const privateRead = response.authenticated === true && response.private_read_schema === 1 &&
        Array.isArray(response.private_fields) && privateFields.every(field => response.private_fields.includes(field));
      // An older five-field bridge did authenticate the token, but did not
      // state that private reads were supported. Fail closed and use only the
      // legacy two-field transport until that deployment advertises the gate.
      const schema = response.manual_field_schema === 1 && privateRead ? 1 : 0;
      const advertised = Array.isArray(response.fields) ? response.fields : legacyFields;
      if(schema === 1 && !fields.every(field => advertised.includes(field))) throw new Error('Bridge báo schema 5 trường nhưng thiếu capability; giữ toàn bộ local');
      bridgeCapabilities = {manual_field_schema:schema,private_read_schema:schema,fields:schema === 1 ? fields : legacyFields};
      privateUnlocked = schema === 1;
      applyLocal();
      if(activeId && $('manualDialog').open) openEditor(activeId);
      showStatus(); return bridgeCapabilities;
    }
    const message = String(response?.error || '');
    if(/Action not allowed|Unsupported|không hỗ trợ/i.test(message)) {
      // Deployed v1 bridge predates capability discovery. It is safe only for
      // date/caption; extended fields remain in IndexedDB and are never sent.
      bridgeCapabilities = {manual_field_schema:0,private_read_schema:0,fields:legacyFields};
      privateUnlocked = false;
      applyLocal();
      if(activeId && $('manualDialog').open) openEditor(activeId);
      showStatus(); return bridgeCapabilities;
    }
    throw new Error(message || 'Không xác minh được capability của bridge; giữ thay đổi local');
  }
  function mergeRecord(oldRecord,item) {
    if(!item || typeof item !== 'object') return oldRecord;
    const old = oldRecord || {fields:{},revision:0};
    return {...old,...clone(item),fields:{...(old.fields || {}),...(clone(item.fields) || {})}};
  }
  function sendable(op, capability) {
    return Object.keys(op.patch || {}).some(field => capability.fields.includes(field));
  }
  function wireOperation(op, capability) {
    const full = capability.manual_field_schema === 1;
    const patch = Object.fromEntries(Object.entries(op.patch || {}).filter(([field]) => capability.fields.includes(field)).map(([field,value]) => [field,clone(value)]));
    if(!Object.keys(patch).length) return null;
    const baseValues = Object.fromEntries(Object.entries(op.base_values || {}).filter(([field]) => field in patch).map(([field,value]) => [field,clone(value)]));
    return {op_id:full ? op.op_id : op.legacy_op_id,profile,video_id:op.video_id,patch,base_values:baseValues,
      base_revision:op.base_revision || 0,after_op:full ? (op.after_op || '') : (op.legacy_after_op || ''),created_at:op.created_at};
  }
  async function sync() {
    if(!ready || busy || !token || !gasUrl || !navigator.onLine) { showStatus(); return; }
    busy = true; showStatus();
    try {
      state = await readState();
      const capability = await negotiateCapabilities();
      if(capability.manual_field_schema !== 1 && state.outbox.some(op => !op.legacy_op_id)) {
        // Persist transport identities before the request. Lost ACK/retry must
        // reuse the same ID while leaving the original five-field op intact.
        await mutate(s => {
          const previous = new Map();
          for(const op of s.outbox) {
            if(!op.legacy_op_id) op.legacy_op_id = uid();
            if(op.legacy_after_op === undefined) op.legacy_after_op = previous.get(op.video_id) || '';
            previous.set(op.video_id,op.legacy_op_id);
          }
        });
        state = await readState();
      }
      const blocked = new Set(state.outbox.filter(op => op.status === 'conflict').map(op => op.video_id));
      const ops = [], sent = new Map(); let payloadSize = 2048;
      for(const op of state.outbox.filter(op => !blocked.has(op.video_id) && sendable(op,capability))) {
        const wire = wireOperation(op,capability), size = JSON.stringify(wire).length;
        if(ops.length >= 20 || (ops.length && payloadSize + size > 150000)) break;
        ops.push(wire); sent.set(wire.op_id,{local:op.op_id,fields:Object.keys(wire.patch),full:capability.manual_field_schema === 1}); payloadSize += size;
      }
      const response = await rpc({action:'sync',profile,cursor:state.cursor,ops,client_manual_field_schema:capability.manual_field_schema,
        client_private_read_schema:capability.private_read_schema});
      if(!response?.ok) throw new Error(response?.error || 'Phản hồi GAS không hợp lệ');
      await mutate(s => {
        for(const item of response.changes || []) if((s.records[item.video_id]?.revision || 0) <= item.revision) s.records[item.video_id] = mergeRecord(s.records[item.video_id],item);
        for(const ack of response.acks || []) {
          const delivery = sent.get(ack.op_id); if(!delivery) continue;
          const op = s.outbox.find(v => v.op_id === delivery.local); if(!op) continue;
          if(ack.status === 'ok') {
            if(!ack.state || ack.state.video_id !== op.video_id) throw new Error('GAS chưa trả bản đã lưu; giữ lại hàng đợi');
            if((s.records[op.video_id]?.revision || 0) <= ack.state.revision) s.records[op.video_id] = mergeRecord(s.records[op.video_id],ack.state);
            s.history.push({event:'server_ack',op_id:op.op_id,transport_op_id:ack.op_id,fields:delivery.fields,revision:ack.revision});
            if(delivery.full) s.outbox = s.outbox.filter(v => v.op_id !== op.op_id);
            else {
              for(const field of delivery.fields) { delete op.patch[field]; delete op.base_values[field]; }
              if(Object.keys(op.patch).length) { op.base_revision = ack.state.revision; op.after_op = ack.op_id; op.status = 'pending'; }
              else s.outbox = s.outbox.filter(v => v.op_id !== op.op_id);
            }
          }
          else { op.status = 'conflict'; op.current = mergeRecord(s.records[op.video_id],ack.current); }
        }
        s.cursor = response.cursor;
      });
      lastError = '';
      if(response.has_more || state.outbox.some(op => !blocked.has(op.video_id) && op.status !== 'conflict' && sendable(op,capability))) setTimeout(sync,500);
    } catch(error) { lastError = error.message; }
    finally { busy = false; showStatus(); }
  }
  async function exportBackup() {
    if(!ready) return;
    if(!privateUnlocked) throw new Error('Cần GAS xác thực trước khi xuất backup có dữ liệu riêng. Dữ liệu local vẫn được giữ nguyên.');
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
  $('manualDate').oninput = () => { dateTouched = true; captureDraft(); };
  $('manualTime').oninput = () => { timeTouched = true; let value = null; try { value = exactTime($('manualTime').value); } catch {} showTimeSummary(value); captureDraft(); };
  $('manualCaption').oninput = captureDraft; $('manualTags').oninput = captureDraft; $('manualNotes').oninput = captureDraft;
  $('manualTimeNow').onclick = () => {
    const date = new Date(), part = number => String(number).padStart(2,'0');
    $('manualTime').value = `${date.getFullYear()}-${part(date.getMonth()+1)}-${part(date.getDate())}T${part(date.getHours())}:${part(date.getMinutes())}:${part(date.getSeconds())}`;
    timeTouched = true; showTimeSummary(exactTime($('manualTime').value)); captureDraft();
  };
  $('manualTimeClear').onclick = () => { $('manualTime').value = ''; timeTouched = true; showTimeSummary(null); captureDraft(); };
  $('manualPreviousDay').onclick = () => shiftDay(-1); $('manualNextDay').onclick = () => shiftDay(1);
  $('manualLastDate').onclick = () => { if(state.lastDate) { $('manualDate').value = state.lastDate; dateTouched = true; captureDraft(); } };
  $('manualKeepLocal').onclick = () => resolveConflict(true).catch(reportError);
  $('manualUseServer').onclick = () => resolveConflict(false).catch(reportError);
  document.querySelectorAll('[data-close]').forEach(button => button.onclick = () => button.dataset.close === 'manualDialog' ? closeEditor() : $(button.dataset.close).close());
  $('manualDialog').addEventListener('cancel',event => { event.preventDefault(); closeEditor(); });
  $('manualDisconnect').onclick = disconnect; $('manualExport').onclick = () => exportBackup().catch(reportError);
  $('manualForgetToken').onclick = () => {
    try {
      localStorage.removeItem(tokenKey()); sessionStorage.removeItem(tokenKey());
      sessionStorage.removeItem('tiktok-gas-token'); disconnect(); $('manualGasToken').value = '';
      $('manualSettingsMessage').textContent = 'Đã quên token cho user và URL này. Dữ liệu chỉnh sửa local vẫn giữ nguyên.';
    } catch(error) { reportError(error); }
  };
  $('manualRememberToken').onchange = () => {
    try {
      localStorage.setItem(rememberKey,String($('manualRememberToken').checked));
      if(!$('manualRememberToken').checked) localStorage.removeItem(tokenKey());
    } catch(error) { reportError(error); }
  };
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
    try {
      saveUrl(); disconnect(); token = $('manualGasToken').value.trim();
      sessionStorage.setItem(tokenKey(),token);
      const remember = $('manualRememberToken').checked;
      localStorage.setItem(rememberKey,String(remember));
      if(remember && token) localStorage.setItem(tokenKey(),token); else localStorage.removeItem(tokenKey());
    }
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
