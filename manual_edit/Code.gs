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
  // Apps Script's Run button invokes this without an HTTP event. Keep that
  // diagnostic path harmless; a real bridge still has to pass origin/channel.
  e = e && e.parameter ? e : {parameter:{}};
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
    if(!['sync','pull','export_journal','capabilities'].includes(request.action)) throw new Error('Action not allowed');
    if(JSON.stringify(request).length > 200000) throw new Error('Request too large');
    const manualFieldSchema = properties.getProperty('MANUAL_FIELD_SCHEMA') === '1' ? 1 : 0;
    if(request.action === 'capabilities') return {
      ok:true,service:'tiktok-manual-bridge-v2',authenticated:true,auth_mode:'bearer_token',
      manual_field_schema:manualFieldSchema,private_read_schema:manualFieldSchema,
      fields:manualFieldSchema === 1 ? ['repost_date','repost_time','repost_caption','tags','notes'] : ['repost_date','repost_caption'],
      public_fields:manualFieldSchema === 1 ? ['repost_date','repost_time','repost_caption','tags'] : ['repost_date','repost_caption'],
      private_fields:manualFieldSchema === 1 ? ['notes'] : []
    };
    const lock = LockService.getScriptLock();
    if(!lock.tryLock(5000)) throw new Error('Busy; retry later');
    try { return legacyMigrationDispatch_(request,properties); } finally { lock.releaseLock(); }
  } catch(error) { return {ok:false,error:String(error.message || error)}; }
}

function equalToken_(a,b) {
  let difference = a.length ^ b.length;
  for(let i=0;i<Math.max(a.length,b.length);i++) difference |= (a.charCodeAt(i)||0) ^ (b.charCodeAt(i)||0);
  return difference === 0;
}

function canonicalManualValue_(key,value) {
  if(key === 'repost_date') {
    if(typeof value !== 'string' || value && !validDate_(value)) throw new Error('Invalid date');
    return value;
  }
  if(key === 'repost_caption') {
    if(typeof value !== 'string' || value.length > 5000) throw new Error('Invalid/long caption');
    return value;
  }
  if(key === 'notes') {
    if(typeof value !== 'string' || value.length > 20000) throw new Error('Invalid/long notes');
    return value;
  }
  if(key === 'tags') {
    if(!Array.isArray(value) || value.length > 30 || new Set(value).size !== value.length ||
       value.some(tag=>typeof tag !== 'string' || !tag.trim() || tag !== tag.trim() || tag.length > 60)) throw new Error('Invalid tags');
    return value.slice();
  }
  if(key === 'repost_time') {
    if(value === null) return null;
    if(!value || typeof value !== 'object' || Array.isArray(value) || !['exact','range'].includes(value.kind) ||
       typeof value.zone !== 'string' || !value.zone || value.zone.length > 100) throw new Error('Invalid repost time');
    const keys=Object.keys(value).sort().join('|');
    if(value.kind === 'exact') {
      if(keys !== 'at|kind|zone' || !validInstant_(value.at)) throw new Error('Invalid exact repost time');
    } else if(keys !== 'kind|lower|upper|zone' || !validInstant_(value.lower) || !validInstant_(value.upper) || Date.parse(value.lower)>Date.parse(value.upper)) {
      throw new Error('Invalid repost time range');
    }
    return JSON.parse(JSON.stringify(value));
  }
  throw new Error('Unsupported field');
}

function validInstant_(value) {
  return typeof value === 'string' && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)$/.test(value) && Number.isFinite(Date.parse(value));
}

function canonicalOp_(op,profile,manualFieldSchema,privateRead) {
  if(op.profile !== profile || !/^\d{15,25}$/.test(String(op.video_id))) throw new Error('Invalid profile/video ID');
  if(!/^[a-f0-9-]{36}$/i.test(op.op_id)) throw new Error('Invalid operation ID');
  if(!Number.isSafeInteger(op.base_revision) || op.base_revision < 0) throw new Error('Invalid base revision');
  const patch = op.patch;
  if(!patch || typeof patch !== 'object' || Array.isArray(patch)) throw new Error('Invalid patch');
  const keys = Object.keys(patch).sort();
  const allowed=manualFieldSchema === 1 ? ['repost_date','repost_time','repost_caption','tags','notes'] : ['repost_date','repost_caption'];
  if(!keys.length || keys.some(k=>!allowed.includes(k))) throw new Error('Unsupported fields');
  const allowPrivate=privateRead===undefined?manualFieldSchema===1:privateRead===true;
  if(keys.includes('notes')&&!allowPrivate)throw new Error('Private note requires authenticated private-read capability');
  const normalized = {}, base = {};
  keys.forEach(key => {
    normalized[key] = canonicalManualValue_(key,patch[key]);
    if(op.base_values && Object.prototype.hasOwnProperty.call(op.base_values,key)) base[key] = canonicalManualValue_(key,op.base_values[key]);
  });
  if(op.after_op && !/^[a-f0-9-]{36}$/i.test(op.after_op)) throw new Error('Invalid dependency');
  return {op_id:op.op_id,profile,video_id:String(op.video_id),base_revision:op.base_revision,after_op:op.after_op||'',patch:normalized,base_values:base};
}

function clientState_(state,privateRead) {
  const result=JSON.parse(JSON.stringify(state));
  if(!privateRead){
    if(result.fields)delete result.fields.notes;
    if(result.base_values)delete result.base_values.notes;
  }
  return result;
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
  const manualFieldSchema=properties.getProperty('MANUAL_FIELD_SCHEMA') === '1' && request.client_manual_field_schema === 1 ? 1 : 0;
  const privateRead=manualFieldSchema===1&&request.client_private_read_schema===1;
  const acks = [], newRows = [];
  operations.forEach(raw => {
    const op = canonicalOp_(raw,request.profile,manualFieldSchema,privateRead), previous = seen.get(op.op_id);
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
  const changes = [...current.values()].filter(state => state.profile === request.profile && state.server_seq > cursor && state.server_seq <= boundary)
    .map(state=>clientState_(state,privateRead));
  const clientAcks=acks.map(ack=>{
    const item={...ack};
    if(item.state)item.state=clientState_(item.state,privateRead);
    if(item.current)item.current=clientState_(item.current,privateRead);
    return item;
  });
  return {ok:true,acks:clientAcks,changes,cursor:boundary,has_more:boundary < sequence};
}

function safeCell_(value) { return /^[=+\-@\t\r]/.test(value) ? "'"+value : value; }

/* Read-only migration export and an administrator-owned freeze gate. The
 * default remains unfrozen. Never accept a client request that changes this
 * property; set/remove LEGACY_WRITES_FROZEN only in Script Properties. */
function legacyMigrationDispatch_(request,properties){
  if(request.action==='export_journal')return legacyExport_(request,properties);
  if(properties.getProperty('LEGACY_WRITES_FROZEN')==='1'&&request.action==='sync'&&
      Array.isArray(request.ops||[])&&(request.ops||[]).length)
    throw Error('LEGACY_WRITES_FROZEN: migration in progress; retain local edits and retry after client upgrade');
  return execute_(request,properties);
}
function legacyExportHash_(text){
  return Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256,text,Utilities.Charset.UTF_8)
    .map(b=>('0'+((b+256)%256).toString(16)).slice(-2)).join('');
}
function legacyExport_(request,properties){
  const sheetId=properties.getProperty('SPREADSHEET_ID');
  const journal=SpreadsheetApp.openById(sheetId).getSheetByName('Journal');
  if(!journal)throw Error('Missing Journal; do not initialize a replacement');
  const cursor=request.cursor;
  if(!Number.isSafeInteger(cursor)||cursor<0)throw Error('Invalid export cursor');
  const last=Math.max(0,journal.getLastRow()-1);
  const boundary=request.boundary===undefined?last:request.boundary;
  if(!Number.isSafeInteger(boundary)||boundary<0||boundary>last||cursor>boundary)throw Error('Invalid export boundary');
  if(cursor>0&&typeof request.snapshot_sha256!=='string')throw Error('Snapshot hash required');
  const source=legacyExportHash_(sheetId+'|'+request.profile);
  const authority=source.slice(0,8)+'-'+source.slice(8,12)+'-5'+source.slice(13,16)+'-a'+source.slice(17,20)+'-'+source.slice(20,32);
  const cells=boundary?journal.getRange(2,1,boundary,8).getValues():[];
  const snapshot=legacyExportHash_(JSON.stringify(cells));
  if(request.snapshot_sha256!==undefined&&request.snapshot_sha256!==snapshot)throw Error('Journal snapshot changed; preserve partial export and start a new audit');
  const items=[];let bytes=0,next=cursor;
  for(let i=cursor;i<boundary&&items.length<100;i++){
    const row=cells[i],operation=JSON.parse(row[6]),state=JSON.parse(row[7]);
    if(operation.profile!==request.profile||state.profile!==request.profile||state.video_id!==operation.video_id||
       state.server_seq!==i+1||String(row[0])!==operation.op_id||String(row[1])!==request.profile||
       String(row[2])!==operation.video_id||Number(row[3])!==state.revision||Number(row[4])!==state.server_seq)
      throw Error('Journal row scope/index mismatch');
    const item={request:operation,state},size=JSON.stringify(item).length;
    if(size>180000)throw Error('Journal row too large');
    if(items.length&&bytes+size>180000)break;
    items.push(item);bytes+=size;next=i+1;
  }
  return {ok:true,format:'legacy-annotation-export-v1',profile:request.profile,authority,
          boundary,snapshot_sha256:snapshot,cursor:next,has_more:next<boundary,items,
          writes_frozen:properties.getProperty('LEGACY_WRITES_FROZEN')==='1',current_server_seq:last};
}
