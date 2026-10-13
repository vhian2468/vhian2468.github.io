"""Read-only story-project adapter, strictly scoped to one username."""
import json
import re
import time
from pathlib import Path
from datetime import datetime
from tiktok_feed_types import source_metadata, merge_sources, arrange_stories, timestamp_seconds, LOCAL_ZONE

STORY_PROJECT = r'D:\tiktok\tiktok-story'
API_META_FIELDS = ('uniqueId', 'nickname', 'desc', 'cover', 'duration', 'music_title', 'music_id', 'date', 'digg', 'share', 'comment', 'play', 'create_time', 'story_expires_at', 'story_activity_date')


def capture_epoch(value):
    if isinstance(value, (int, float)):
        return timestamp_seconds(value)
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=LOCAL_ZONE)).timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def field_clock(item, field):
    if 'api_field_updated_at' in item:
        return capture_epoch(item['api_field_updated_at'].get(field))
    return capture_epoch(item.get('api_updated_at') or item.get('story_last_seen'))


def update_api_metadata(target, incoming, live=False):
    """Compare each observed field, never use file mtime/createTime as freshness."""
    present = incoming.get('api_present_fields', [key for key in API_META_FIELDS if key in incoming])
    manual = set(target.get('manual_fields', []))
    clocks = dict(target.get('api_field_updated_at', {}))
    if 'api_field_updated_at' not in target:
        previous_stamp = target.get('api_updated_at') or target.get('story_last_seen')
        if capture_epoch(previous_stamp):
            for key in API_META_FIELDS:
                if key in target and key in target.get('api_present_fields', API_META_FIELDS):
                    clocks[key] = previous_stamp
    applied = conflicts = 0
    for key in present:
        if key not in API_META_FIELDS or key in manual or key not in incoming:
            continue
        newer, current = field_clock(incoming, key), field_clock(target, key)
        missing = (key not in target or (target[key] in (None, '') and not current)
                   or ('api_present_fields' in target and key not in target['api_present_fields'] and not current))
        if missing or (newer and ((current and newer > current) or (live and not current))):
            target[key] = incoming[key]
            if newer:
                clocks[key] = incoming.get('api_field_updated_at', {}).get(key) or incoming.get('api_updated_at') or incoming.get('story_last_seen')
            applied += 1
            if 'api_present_fields' in target and key not in target['api_present_fields']:
                target['api_present_fields'] = list(target['api_present_fields']) + [key]
        elif not newer or not current:
            conflicts += 1
        # A retained older field keeps its own clock, not the new record clock.
        if key not in clocks and current:
            clocks[key] = target.get('api_updated_at') or target.get('story_last_seen')
    if clocks:
        target['api_field_updated_at'] = clocks
        target['api_updated_at'] = max(clocks.values(), key=capture_epoch)
    return applied, conflicts


def current_username(input_path):
    name = Path(input_path).stem
    if not name.startswith('data_'):
        raise ValueError('Tên database phải là data_<username>.json để xác định đúng user.')
    username = name[5:].removesuffix('_all')
    if not re.fullmatch(r'[A-Za-z0-9_.]+', username):
        raise ValueError('Username không hợp lệ; không đồng bộ.')
    return username


def read_stories(username, project_root=STORY_PROJECT):
    if not re.fullmatch(r'[A-Za-z0-9_.]+', username):
        raise ValueError('Username không hợp lệ')
    root = Path(project_root)
    paths = [root/f'data_story_{username}.json', root/'stories_data'/username/'db.json']
    available = [path for path in paths if path.is_file()]
    if not available:
        raise FileNotFoundError(f'Không có database story của @{username} tại {root}')
    merged = {}
    skipped_owner = 0
    unknown_clock = 0
    for path in sorted(available, key=lambda value: value.stat().st_mtime):
        payload = json.loads(path.read_text(encoding='utf-8-sig'))
        entries = payload.values() if isinstance(payload, dict) else payload
        if not isinstance(entries, (list, type({}.values()))):
            raise ValueError(f'Format story không hỗ trợ: {path.name}')
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f'Bản ghi không hợp lệ trong {path.name}')
            raw = entry.get('raw') or entry.get('api_raw') or entry
            if not isinstance(raw, dict):
                raise ValueError(f'Raw story không hợp lệ trong {path.name}')
            owner = (raw.get('author') or {}).get('uniqueId') or entry.get('uniqueId')
            if owner and owner.casefold().lstrip('@') != username.casefold():
                skipped_owner += 1
                continue
            identifier = str(raw.get('id') or entry.get('id') or '')
            if not identifier.isdigit():
                raise ValueError(f'Story thiếu ID hợp lệ trong {path.name}')
            stats = raw.get('stats') or entry.get('stats') or {}
            music = raw.get('music') or entry.get('music') or {}
            video = raw.get('video') or {}
            created = timestamp_seconds(raw.get('createTime') or entry.get('create_time'))
            api_raw = dict(raw)
            api_raw['id'] = identifier
            if created:
                api_raw['createTime'] = created
            normalized = {'id': identifier, 'uniqueId': username,
                          'nickname': (raw.get('author') or {}).get('nickname') or entry.get('nickname', ''),
                          'desc': raw.get('desc') or entry.get('desc', ''),
                          'cover': video.get('cover') or entry.get('cover', ''),
                          'duration': video.get('duration') or entry.get('duration', 0),
                          'music_title': music.get('title', ''),
                          'music_id': str(music.get('id') or music.get('mid') or ''),
                          'digg': stats.get('diggCount', 0), 'comment': stats.get('commentCount', 0),
                          'share': stats.get('shareCount', 0), 'play': stats.get('playCount', 0),
                          'repost_date': '', 'repost_caption': '', 'repost_order': 0,
                          'story_sync_files': [str(path)], 'story_sync_at': datetime.now(LOCAL_ZONE).isoformat()}
            normalized.update(source_metadata(api_raw, 'https://www.tiktok.com/api/story/item_list/'))
            stamp = entry.get('api_updated_at') or raw.get('api_updated_at')
            if not capture_epoch(stamp):
                unknown_clock += 1
            else:
                normalized['api_updated_at'] = stamp
            normalized['source_basis'] = 'story_project'
            normalized['date'] = datetime.fromtimestamp(normalized.get('create_time', 0), LOCAL_ZONE).strftime('%Y-%m-%d')
            if not normalized['cover']:
                image_cover = (raw.get('imagePost') or {}).get('cover') or {}
                urls = (image_cover.get('imageURL') or {}).get('urlList') or []
                normalized['cover'] = urls[0] if urls else ''
            present = []
            for key, raw_key in (('digg','diggCount'), ('share','shareCount'), ('comment','commentCount'), ('play','playCount')):
                if raw_key in stats:
                    present.append(key)
            if 'desc' in raw or 'desc' in entry: present.append('desc')
            if normalized['cover']: present.append('cover')
            if 'duration' in video or 'duration' in entry: present.append('duration')
            if 'title' in music: present.append('music_title')
            if music.get('id') or music.get('mid'): present.append('music_id')
            if normalized['nickname']: present.append('nickname')
            present.extend(['uniqueId', 'date'])
            present.extend(key for key in ('create_time','story_expires_at','story_activity_date') if key in normalized)
            normalized['api_present_fields'] = present
            if stamp:
                normalized['api_field_updated_at'] = {key:stamp for key in present}
            old = merged.get(identifier)
            if old:
                combined = dict(old)
                update_api_metadata(combined, normalized, live=bool(capture_epoch(normalized.get('api_updated_at'))))
                combined['api_present_fields'] = sorted(set(old.get('api_present_fields', [])) | set(present))
                combined['story_sync_files'] = sorted(set(old['story_sync_files'] + normalized['story_sync_files']))
                normalized = combined
            merged[identifier] = normalized
    return list(merged.values()), {'source_files': [str(p) for p in available], 'skipped_other_user': skipped_owner, 'records_without_api_timestamp':unknown_clock}


def sync_story_items(current, stories, allow_deleted=False, deleted_ids=(), now=None):
    """Add missing stories/fill empty fields; preserve existing user data/stats."""
    now = time.time() if now is None else now
    result = [dict(v) for v in current]
    mapping = {str(v['id']): v for v in result}
    if len(mapping) != len(result):
        raise ValueError('Database hiện tại có ID trùng; không đồng bộ')
    deleted = set(map(str, deleted_ids))
    counts = {'added':0, 'matched':0, 'updated':0, 'skipped_deleted':0, 'kept_post':0, 'metadata_fields_applied':0, 'metadata_fields_without_comparable_time':0}
    for incoming in stories:
        identifier = str(incoming['id'])
        if identifier in deleted and not allow_deleted:
            counts['skipped_deleted'] += 1
            continue
        existing = mapping.get(identifier)
        if existing is None:
            value = dict(incoming)
            result.append(value)
            mapping[identifier] = value
            counts['added'] += 1
            continue
        counts['matched'] += 1
        if existing.get('source_type') == 'post' and existing.get('source_confidence') != 'inferred':
            counts['kept_post'] += 1
            continue
        before = dict(existing)
        merge_sources(existing, incoming)
        for key in API_META_FIELDS:
            if key in before:
                existing[key] = before[key]
            else:
                existing.pop(key, None)
        applied, conflicts = update_api_metadata(existing, incoming)
        counts['metadata_fields_applied'] += applied
        counts['metadata_fields_without_comparable_time'] += conflicts
        existing['source_confidence'] = 'api'
        existing['source_basis'] = 'story_project'
        existing['story_sync_files'] = incoming['story_sync_files']
        existing['story_sync_at'] = incoming['story_sync_at']
        if existing != before:
            counts['updated'] += 1
    result = arrange_stories(result, now=now)
    for index, item in enumerate(result):
        item['feed_index'] = index
    return result, counts
