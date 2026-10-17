"""Explicit manual annotations: GAS pull/import, three-way merge, immutable backups."""
import copy
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, date
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

FIELDS = ('repost_date', 'repost_caption')
ASSETS = Path(__file__).parent / 'manual_edit'
ENDPOINT_SETTINGS = ASSETS / 'endpoint_settings.json'


def validate_gas_url(url):
    url = url.strip()
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname != 'script.google.com'
            or not re.fullmatch(r'/macros/s/[A-Za-z0-9_-]+/exec', parts.path)
            or parts.query or parts.fragment or parts.username or parts.password or parts.port):
        raise ValueError('Cần URL GAS https://script.google.com/macros/s/.../exec, không kèm token/tham số')
    return url


def saved_gas_url(profile):
    profile = profile.removesuffix('_all')
    if not ENDPOINT_SETTINGS.exists():
        return ''
    config = json.loads(ENDPOINT_SETTINGS.read_text(encoding='utf-8'))
    url = config.get('profiles', {}).get(profile, '')
    return validate_gas_url(url) if url else ''


def save_gas_url(profile, url):
    profile = profile.removesuffix('_all')
    if not re.fullmatch(r'[A-Za-z0-9_.]+', profile):
        raise ValueError('Username không hợp lệ')
    url = validate_gas_url(url) if url.strip() else ''
    config = json.loads(ENDPOINT_SETTINGS.read_text(encoding='utf-8')) if ENDPOINT_SETTINGS.exists() else {}
    profiles = config.get('profiles', {})
    if url:
        profiles[profile] = url
    else:
        profiles.pop(profile, None)
    # Only public endpoint URLs. Never accept/persist a token here.
    atomic_json(ENDPOINT_SETTINGS, {'version':1,'profiles':profiles})
    return url


def atomic_json(path, value):
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False, suffix='.tmp') as file:
            temporary = file.name
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def profile_for(path):
    name = Path(path).stem
    if not name.startswith('data_'):
        raise ValueError('Database cần tên data_<username>.json')
    profile = name[5:].removesuffix('_all')
    if not re.fullmatch(r'[A-Za-z0-9_.]+', profile):
        raise ValueError('Username không hợp lệ')
    return profile


def validate_patch(patch):
    if not isinstance(patch, dict) or set(patch) - set(FIELDS):
        raise ValueError('Chỉ cho sửa repost_date và repost_caption')
    for key, value in patch.items():
        if not isinstance(value, str):
            raise ValueError('Giá trị chỉnh sửa phải là chuỗi')
        if key == 'repost_date' and value:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError('Ngày cần YYYY-MM-DD')
            date.fromisoformat(value)
        if key == 'repost_caption' and len(value) > 5000:
            raise ValueError('Caption quá 5000 ký tự')


def apply_annotations(data, records, ledger=None, force=False, remote=False):
    result = copy.deepcopy(data)
    mapping = {str(v['id']): v for v in result}
    if len(mapping) != len(result):
        raise ValueError('Database có ID trùng')
    ledger = copy.deepcopy(ledger or {})
    conflicts, missing = [], []
    changed = 0
    for record in records:
        identifier = str(record['video_id'])
        patch = record.get('fields', record.get('patch', {}))
        validate_patch(patch)
        target = mapping.get(identifier)
        if target is None:
            missing.append(identifier)
            continue
        previous = ledger.setdefault(identifier, {})
        for key, value in patch.items():
            before = previous.get(key)
            revision = int(record.get('revision', 0))
            if revision < 0:
                raise ValueError('Revision không hợp lệ')
            if before and revision < before.get('revision', 0):
                continue
            # First import compares with the value displayed when editing started.
            base = before['value'] if before else record.get('base_values', {}).get(key)
            current = str(target.get(key, '') or '')
            if remote and before and before.get('pending') and value != before['value']:
                conflicts.append({'video_id':identifier,'field':key,'local':current,'remote':value,'base':base,'reason':'offline edit not acknowledged'})
                continue
            if not force and current != value and (base is None or current != base):
                conflicts.append({'video_id':identifier,'field':key,'local':current,'remote':value,'base':base})
                continue
            if current != value:
                target[key] = value
                changed += 1
            previous[key] = {'value':value, 'revision':revision}
            if key in record.get('pending_fields', []):
                previous[key]['pending'] = True
            elif before and before.get('pending') and not remote:
                previous[key]['pending'] = True
            target['manual_fields'] = sorted(set(target.get('manual_fields', [])) | {key})
    return result, ledger, {'changed_fields':changed,'conflicts':conflicts,'missing_ids':missing}


def import_records(database_path, document, force=False, remote=False):
    path = Path(database_path)
    profile = profile_for(path)
    if document.get('profile') != profile:
        raise ValueError('User trong bản chỉnh sửa không khớp database')
    records = document.get('changes')
    if not isinstance(records, list):
        raise ValueError('Thiếu danh sách changes')
    original = path.read_bytes()
    raw = json.loads(original.decode('utf-8-sig'))
    data = list(raw.values()) if isinstance(raw, dict) else raw
    ledger_path = Path(str(path)+'.manual_edits.json')
    saved = json.loads(ledger_path.read_text(encoding='utf-8')) if ledger_path.exists() else {}
    updated, ledger, report = apply_annotations(data, records, saved.get('items', {}), force, remote)
    if path.read_bytes() != original:
        raise RuntimeError('Database thay đổi khi đang nhập; không ghi đè')
    backup = None
    if updated != data:
        backup = str(path)+'.manual-edit-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'-'+uuid.uuid4().hex+'.bak'
        with open(backup, 'xb') as file:
            file.write(original)
            file.flush()
            os.fsync(file.fileno())
    # Commit database before ledger, so an interrupted import can safely replay.
    if updated != data:
        atomic_json(path, updated)
    atomic_json(ledger_path, {'version':1,'profile':profile,'items':ledger})
    conflict_path = Path(str(path)+'.manual_edits.conflicts.json')
    old_conflicts = json.loads(conflict_path.read_text(encoding='utf-8')) if conflict_path.exists() else []
    addressed = {(str(r['video_id']), key) for r in records for key in r.get('fields', r.get('patch', {}))}
    pending = [c for c in old_conflicts if (c['video_id'],c['field']) not in addressed]
    report['conflicts'] = pending + report['conflicts']
    atomic_json(conflict_path, report['conflicts'])
    if updated != data:
        atomic_json(str(path)+'.manager.state.json', {'sha256':hashlib.sha256(path.read_bytes()).hexdigest(), 'edited_at':datetime.now().isoformat()})
    report['backup'] = backup
    return report


def reapply_saved_annotations(database_path):
    path = Path(str(database_path)+'.manual_edits.json')
    if not path.exists():
        return None
    saved = json.loads(path.read_text(encoding='utf-8'))
    changes = [{'video_id':identifier, 'fields':{key:value['value']},
                'base_values':{key:value['value']}, 'revision':value.get('revision',0)}
               for identifier,fields in saved.get('items', {}).items() for key,value in fields.items()]
    # No force: another manager edit must not be overwritten by old saved edits.
    return import_records(database_path, {'profile':profile_for(database_path),'changes':changes})


def gas_request(url, token, payload):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname != 'script.google.com' or not parts.path.endswith('/exec'):
        raise ValueError('Cần URL web app GAS https://script.google.com/.../exec')
    body = json.dumps({**payload,'token':token}).encode('utf-8')
    request = Request(url, data=body, headers={'Content-Type':'application/json'}, method='POST')
    with urlopen(request, timeout=45) as response:
        result = json.load(response)
    if not isinstance(result, dict) or not result.get('ok'):
        raise RuntimeError('GAS từ chối yêu cầu: '+str(result.get('error','phản hồi không hợp lệ') if isinstance(result, dict) else 'phản hồi không hợp lệ'))
    return result


def pull_annotations(database_path, url=None, token=None, force=False):
    url = url or os.environ.get('TIKTOK_GAS_URL', '') or saved_gas_url(profile_for(database_path))
    token = token or os.environ.get('TIKTOK_GAS_TOKEN', '')
    if not url or not token:
        raise ValueError('Cần TIKTOK_GAS_URL và TIKTOK_GAS_TOKEN hoặc nhập trong manager')
    cursor, records = 0, {}
    while True:
        response = gas_request(url, token, {'action':'pull','profile':profile_for(database_path),'cursor':cursor})
        for item in response.get('changes', []):
            records[str(item['video_id'])] = item
        next_cursor = int(response.get('cursor', cursor))
        if response.get('has_more') and next_cursor <= cursor:
            raise RuntimeError('GAS cursor không tiến; dừng để tránh lặp vô hạn')
        cursor = next_cursor
        if not response.get('has_more'):
            break
    return import_records(database_path, {'profile':profile_for(database_path),'changes':list(records.values())}, force, remote=True)


def inject_editor(html, profile):
    css = (ASSETS/'editor.css').read_text(encoding='utf-8')
    markup = (ASSETS/'editor.html').read_text(encoding='utf-8')
    script = (ASSETS/'editor.js').read_text(encoding='utf-8')
    config = json.dumps(profile.removesuffix('_all')).replace('<','\\u003c')
    endpoint = json.dumps(saved_gas_url(profile)).replace('<','\\u003c')
    block = '<style>'+css+'</style>'+markup+'<script>const MANUAL_PROFILE='+config+';const MANUAL_DEFAULT_GAS_URL='+endpoint+';\n'+script+'</script>'
    return html.replace('</body>', block+'\n</body>')
