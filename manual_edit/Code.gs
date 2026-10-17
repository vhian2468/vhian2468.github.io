/* Manual annotations only. Journal is authoritative and is NEVER pruned. */
const JOURNAL_HEADERS_ = ['op_id','profile','video_id','revision','server_seq','updated_at','request_json','state_json'];
const EDIT_HEADERS_ = ['profile','video_id','repost_date','repost_caption','revision','updated_at'];

// Run from Apps Script editor once, then read API_TOKEN in Script Properties.
// Private trailing underscore keeps setup inaccessible through google.script.run.
function setup_() {
  const properties = PropertiesService.getScriptProperties();
  if(!properties.getProperty('API_TOKEN')) properties.setProperty('API_TOKEN',Utilities.getUuid()+Utilities.getUuid());
  if(!properties.getProperty('ALLOWED_PROFILE')) properties.setProperty('ALLOWED_PROFILE','pe.siro_phan');
  if(!properties.getProperty('ALLOWED_ORIGINS')) properties.setProperty('ALLOWED_ORIGINS','https://vhian2468.github.io');
  if(!properties.getProperty('SPREADSHEET_ID')) properties.setProperty('SPREADSHEET_ID',SpreadsheetApp.create('TikTok manual edits').getId());
  const book = SpreadsheetApp.openById(properties.getProperty('SPREADSHEET_ID'));
  [['Journal',JOURNAL_HEADERS_],['Edits',EDIT_HEADERS_]].forEach(([name,headers]) => {
    const sheet = book.getSheetByName(name) || book.insertSheet(name);
    if(!sheet.getLastRow()) sheet.getRange(1,1,1,headers.length).setValues([headers]);
    sheet.setFrozenRows(1);
  });
}

function doGet(e) {
  if(e.parameter.bridge !== '1') return HtmlService.createHtmlOutput('TikTok edit sync endpoint. Configure the bridge in your HTML editor.');
  const origin = String(e.parameter.parent || '');
  const allowed = (PropertiesService.getScriptProperties().getProperty('ALLOWED_ORIGINS') || '').split(',').map(v=>v.trim());
  if(!allowed.includes(origin)) return HtmlService.createHtmlOutput('Origin not allowed.');
  const channel = String(e.parameter.channel || '');
  if(!/^[a-f0-9-]{36}$/i.test(channel)) return HtmlService.createHtmlOutput('Invalid channel');
  const template = HtmlService.createTemplateFromFile('Bridge');
  template.config = JSON.stringify({origin,channel}).replace(/</g,'\\u003c');
  return template.evaluate().setTitle('TikTok sync bridge').setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function doPost(e) {
  let result;
  try { result = manualRpc(JSON.parse(e.postData.contents)); }
  catch(error) { result = {ok:false,error:String(error.message || error)}; }
  return ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON);
}

function manualRpc(request) {
  try {
    const properties = PropertiesService.getScriptProperties();
    const expected = properties.getProperty('API_TOKEN') || '';
    if(!expected || typeof request.token !== 'string' || !equalToken_(request.token,expected)) throw new Error('Unauthorized');
    if(request.profile !== properties.getProperty('ALLOWED_PROFILE')) throw new Error('Profile not allowed');
    if(!['sync','pull'].includes(request.action)) throw new Error('Action not allowed');
    if(JSON.stringify(request).length > 200000) throw new Error('Request too large');
    const lock = LockService.getScriptLock();
    if(!lock.tryLock(5000)) throw new Error('Busy; retry later');
    try { return execute_(request,properties); } finally { lock.releaseLock(); }
  } catch(error) { return {ok:false,error:String(error.message || error)}; }
}

function equalToken_(a,b) {
  let difference = a.length ^ b.length;
  for(let i=0;i<Math.max(a.length,b.length);i++) difference |= (a.charCodeAt(i)||0) ^ (b.charCodeAt(i)||0);
  return difference === 0;
}

function canonicalOp_(op,profile) {
  if(op.profile !== profile || !/^\d{15,25}$/.test(String(op.video_id))) throw new Error('Invalid profile/video ID');
  if(!/^[a-f0-9-]{36}$/i.test(op.op_id)) throw new Error('Invalid operation ID');
  if(!Number.isSafeInteger(op.base_revision) || op.base_revision < 0) throw new Error('Invalid base revision');
  const patch = op.patch;
  if(!patch || typeof patch !== 'object' || Array.isArray(patch)) throw new Error('Invalid patch');
  const keys = Object.keys(patch).sort();
  if(!keys.length || keys.some(k=>!['repost_date','repost_caption'].includes(k))) throw new Error('Unsupported fields');
  const normalized = {}, base = {};
  keys.forEach(key => {
    const value = patch[key];
    if(typeof value !== 'string') throw new Error('Field must be text');
    if(key === 'repost_caption' && value.length > 5000) throw new Error('Caption too long');
    if(key === 'repost_date' && value && !validDate_(value)) throw new Error('Invalid date');
    normalized[key] = value;
    if(op.base_values && typeof op.base_values[key] === 'string') base[key] = op.base_values[key];
  });
  if(op.after_op && !/^[a-f0-9-]{36}$/i.test(op.after_op)) throw new Error('Invalid dependency');
  return {op_id:op.op_id,profile,video_id:String(op.video_id),base_revision:op.base_revision,after_op:op.after_op||'',patch:normalized,base_values:base};
}

function validDate_(value) {
  if(!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(value+'T12:00:00Z');
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0,10) === value;
}

function execute_(request,properties) {
  const book = SpreadsheetApp.openById(properties.getProperty('SPREADSHEET_ID'));
  const journal = book.getSheetByName('Journal'), edits = book.getSheetByName('Edits');
  if(!journal || !edits) throw new Error('Run setup_ first');
  const count = journal.getLastRow()-1;
  const rows = count > 0 ? journal.getRange(2,1,count,8).getValues() : [];
  const current = new Map(), seen = new Map();
  let sequence = 0;
  rows.forEach(row => {
    if(!row[0]) return;
    const op = JSON.parse(row[6]), state = JSON.parse(row[7]);
    seen.set(op.op_id,{request:op,state}); current.set(op.profile+'|'+op.video_id,state);
    sequence = Math.max(sequence,state.server_seq);
  });
  const cursor = Number(request.cursor || 0);
  if(!Number.isSafeInteger(cursor) || cursor < 0 || cursor > sequence) throw new Error('Invalid/stale cursor; server history may have been restored. Reconnect after exporting backup.');
  const operations = request.action === 'sync' ? request.ops || [] : [];
  if(!Array.isArray(operations) || operations.length > 20) throw new Error('Maximum 20 operations per batch');
  const acks = [], newRows = [];
  operations.forEach(raw => {
    const op = canonicalOp_(raw,request.profile), previous = seen.get(op.op_id);
    if(previous) {
      if(JSON.stringify(previous.request) !== JSON.stringify(op)) throw new Error('Operation ID reused with different data');
      acks.push({op_id:op.op_id,status:'ok',revision:previous.state.revision,state:previous.state}); return;
    }
    const key = op.profile+'|'+op.video_id;
    const old = current.get(key) || {profile:op.profile,video_id:op.video_id,revision:0,fields:{},base_values:{}};
    let baseRevision = op.base_revision;
    if(op.after_op) {
      const dependency = seen.get(op.after_op);
      if(!dependency || dependency.request.video_id !== op.video_id || dependency.request.profile !== op.profile) {
        acks.push({op_id:op.op_id,status:'conflict',current:old}); return;
      }
      baseRevision = dependency.state.revision;
    }
    if(old.revision !== baseRevision) { acks.push({op_id:op.op_id,status:'conflict',current:old}); return; }
    const state = JSON.parse(JSON.stringify(old));
    Object.keys(op.patch).forEach(field => {
      if(!(field in state.base_values) && field in op.base_values) state.base_values[field] = op.base_values[field];
      state.fields[field] = op.patch[field];
    });
    state.revision++; state.server_seq = ++sequence; state.updated_at = new Date().toISOString();
    newRows.push([op.op_id,op.profile,op.video_id,state.revision,state.server_seq,state.updated_at,JSON.stringify(op),JSON.stringify(state)]);
    seen.set(op.op_id,{request:op,state}); current.set(key,state);
    acks.push({op_id:op.op_id,status:'ok',revision:state.revision,state});
  });
  // Journal commit comes first. A retry recovers from a later projection error.
  if(newRows.length) {
    journal.getRange(journal.getLastRow()+1,1,newRows.length,8).setNumberFormat('@').setValues(newRows);
    SpreadsheetApp.flush();
  }
  if(operations.length) {
    const projection = [...current.values()].map(state => [state.profile,state.video_id,state.fields.repost_date||'',safeCell_(state.fields.repost_caption||''),state.revision,state.updated_at]);
    if(projection.length) edits.getRange(2,1,projection.length,6).setNumberFormat('@').setValues(projection);
  }
  const boundary = Math.min(sequence,cursor+300);
  const changes = [...current.values()].filter(state => state.profile === request.profile && state.server_seq > cursor && state.server_seq <= boundary);
  return {ok:true,acks,changes,cursor:boundary,has_more:boundary < sequence};
}

function safeCell_(value) { return /^[=+\-@\t\r]/.test(value) ? "'"+value : value; }
