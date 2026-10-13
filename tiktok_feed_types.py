"""API provenance and narrow story placement; no browser/scroll dependencies."""
import json
import time
from functools import lru_cache
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

KNOWN_TYPES = {'/api/post/item_list/': 'post', '/api/story/item_list/': 'story',
               '/api/repost/item_list/': 'repost'}
LOCAL_ZONE = timezone(timedelta(hours=7))


def api_type(url):
    path = urlsplit(url).path.rstrip('/') + '/'
    if path == '/api/prefetch/explore/item_list/':
        return 'explore'
    return KNOWN_TYPES.get(path, 'unknown')


def timestamp_seconds(value):
    try:
        value = float(value)
        return value / 1000 if value > 100000000000 else value
    except (ValueError, TypeError):
        return 0


def api_items(payload):
    if not isinstance(payload, dict):
        return payload if isinstance(payload, list) else []
    values = payload.get('itemList') or payload.get('item_list') or payload.get('data') or []
    if isinstance(values, dict):
        return api_items(values)
    return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []


def source_metadata(raw, url, observed_at=None):
    kind = api_type(url)
    path = urlsplit(url).path
    result = {'source_type': kind, 'source_types': [kind], 'source_apis': [path],
              'source_confidence': 'api', 'source_basis': 'api_response'}
    created = timestamp_seconds(raw.get('createTime', raw.get('create_time')))
    if not created:
        try:
            created = int(raw.get('id', 0)) >> 32
        except (ValueError, TypeError):
            pass
    if created:
        result['create_time'] = created
    if kind == 'story':
        story = raw.get('story') or {}
        expires = timestamp_seconds(story.get('ExpiredAt', story.get('expiredAt')))
        if not expires and created:
            expires = created + 86400
        result['story_expires_at'] = expires
        if created:
            result['story_activity_date'] = datetime.fromtimestamp(created, LOCAL_ZONE).strftime('%Y-%m-%d')
        if observed_at:
            result['story_last_seen'] = observed_at
    return result


def merge_sources(target, incoming):
    if incoming.get('source_confidence') == 'inferred' and target.get('source_type', 'unknown') != 'unknown' and target.get('source_confidence') != 'inferred':
        return target
    if target.get('source_confidence') == 'inferred' and incoming.get('source_type') in ('post', 'story', 'repost', 'explore') and incoming.get('source_confidence') != 'inferred':
        target['source_types'] = []
        target['source_type'] = 'unknown'
        target['source_confidence'] = 'api'
        target['source_basis'] = incoming.get('source_basis', 'api_log')
    types = set(target.get('source_types', [])) | set(incoming.get('source_types', []))
    for value in (target.get('source_type'), incoming.get('source_type')):
        if value:
            types.add(value)
    target['source_types'] = sorted(types)
    target['source_apis'] = sorted(set(target.get('source_apis', [])) | set(incoming.get('source_apis', [])))
    target['source_type'] = next((t for t in ('post', 'story', 'repost', 'explore', 'unknown') if t in types), 'unknown')
    for key in ('create_time', 'story_expires_at', 'story_activity_date', 'story_last_seen'):
        if incoming.get(key):
            target[key] = incoming[key]
    return target


def source_label(item, now=None):
    kind = item.get('source_type', 'unknown')
    if item.get('source_confidence') == 'inferred':
        return kind + ' · tạm theo username'
    if kind == 'story':
        expires = timestamp_seconds(item.get('story_expires_at'))
        status = ('còn hạn' if expires > (time.time() if now is None else now) else 'hết hạn') if expires else 'chưa rõ hạn'
        return 'story · ' + status
    if kind == 'unknown':
        paths = item.get('source_apis', [])
        return 'chưa rõ' + (' · ' + paths[-1] if paths else '')
    return kind


def story_next_url(url, payload):
    if api_type(url) != 'story' or payload.get('HasMoreAfter') not in (True, 1, 'true'):
        return None
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    cursor = str(payload.get('MaxCursor') or '')
    if not cursor or cursor == '0' or cursor == query.get('cursor'):
        return None
    query['cursor'] = cursor
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def dated(item):
    value = item.get('repost_date') or item.get('story_activity_date', '')
    return valid_day(value) if isinstance(value, str) else ''


@lru_cache(maxsize=4096)
def valid_day(value):
    try:
        datetime.strptime(value, '%Y-%m-%d')
        return value
    except (ValueError, TypeError):
        return ''


def arrange_stories(items, now=None, observed_ids=None):
    """Keep body/repost order intact; only place post/story special groups."""
    now = time.time() if now is None else now
    if observed_ids is None:
        # Offline UI has no current API snapshot. Keep only the already-leading
        # post/story section; never resurrect an old/hidden post to the top.
        observed_ids = set()
        for item in items:
            if item.get('source_type') == 'post':
                observed_ids.add(str(item.get('id')))
            elif item.get('source_type') != 'story':
                break
    posts, active, expired, body = [], [], [], []
    for item in items:
        kind = item.get('source_type')
        expires = timestamp_seconds(item.get('story_expires_at'))
        if item.get('source_confidence') == 'inferred':
            body.append(item)  # Inference must not imply expiry or repositioning.
        elif kind == 'post' and observed_ids is not None and str(item.get('id')) in observed_ids:
            posts.append(item)
        elif kind == 'story' and expires > now:
            active.append(item)
        elif kind == 'story' and expires and expires <= now and dated(item):
            expired.append(item)
        else:
            body.append(item)
    newest = lambda v: (timestamp_seconds(v.get('create_time')), str(v.get('id', '')))
    posts.sort(key=newest, reverse=True)
    active.sort(key=newest, reverse=True)
    groups = {}
    for item in expired:
        groups.setdefault(dated(item), []).append(item)
    for day in sorted(groups, reverse=True):
        group = sorted(groups[day], key=newest, reverse=True)
        same = [i for i, v in enumerate(body) if dated(v) == day]
        if same:
            position = same[-1] + 1
        else:
            position = next((i for i, v in enumerate(body) if dated(v) and dated(v) < day), len(body))
        body[position:position] = group
    return posts + active + body


def recover_sources(items, log_paths, normalizer=None, excluded_ids=(), target_username=None):
    """Annotate existing records; optionally recover only missing post/story."""
    mapping = {str(v['id']): dict(v) for v in items}
    recovered = 0
    for path in log_paths:
        with open(path, encoding='utf-8-sig') as file:
            for line in file:
                try:
                    record = json.loads(line)
                    url = record.get('url', '')
                    if 'item_list' not in urlsplit(url).path:
                        continue
                    kind = api_type(url)
                    for raw in api_items(record.get('data', {})):
                        owner = (raw.get('author') or {}).get('uniqueId', '')
                        if kind in ('post', 'story') and target_username and owner.lower() != target_username.lower():
                            continue
                        identifier = str(raw.get('id', raw.get('aweme_id', '')))
                        current = mapping.get(identifier)
                        if current is None and normalizer and kind in ('post', 'story') and identifier and identifier not in excluded_ids:
                            current = normalizer(raw)
                            mapping[identifier] = current
                            recovered += 1
                        if current is not None:
                            merge_sources(current, source_metadata(raw, url, record.get('timestamp')))
                except (ValueError, TypeError, AttributeError, OverflowError, OSError):
                    continue
    return list(mapping.values()), recovered


def latest_source_patches(items, log_paths, target_username=None):
    """Metadata only; newest matching record wins regardless of file order."""
    wanted = {str(v['id']) for v in items if v.get('source_type', 'unknown') == 'unknown' or v.get('source_confidence') == 'inferred'}
    inferred = {str(v['id']) for v in items if v.get('source_confidence') == 'inferred'}
    newest = {}
    for path in log_paths:
        with open(path, encoding='utf-8-sig') as file:
            for line in file:
                try:
                    record = json.loads(line)
                    url = record.get('url', '')
                    if 'item_list' not in urlsplit(url).path:
                        continue
                    stamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
                    if stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=LOCAL_ZONE)
                    rank = stamp.timestamp()
                    kind = api_type(url)
                    for raw in api_items(record.get('data', {})):
                        identifier = str(raw.get('id', ''))
                        if identifier not in wanted:
                            continue
                        if identifier in inferred and kind == 'unknown':
                            continue
                        owner = (raw.get('author') or {}).get('uniqueId', '')
                        if kind in ('post', 'story') and target_username and owner.lower() != target_username.lower():
                            continue
                        previous = newest.get(identifier)
                        if previous is not None and rank <= previous[0]:
                            continue
                        newest[identifier] = (rank, {'source_type': kind, 'source_types': [kind],
                                                    'source_apis': [urlsplit(url).path],
                                                    'source_confidence': 'api', 'source_basis': 'api_log',
                                                    'source_updated_at': record['timestamp']})
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    return {identifier: value[1] for identifier, value in newest.items()}


def infer_source_patches(items, target_username):
    """User-approved heuristic; no inferred expiry, dates, or feed positions."""
    posts = [v for v in items if v.get('source_type') == 'post' and v.get('source_confidence') != 'inferred']
    if len(posts) != 1:
        raise ValueError('Quy tắc tạm yêu cầu đúng một post đã xác định; không tự đoán post.')
    patches = {}
    for item in items:
        if item.get('source_type', 'unknown') != 'unknown':
            continue
        owner = str(item.get('uniqueId') or '').strip().lstrip('@').casefold()
        if owner in ('', 'unknown', 'n/a'):
            continue
        kind = 'story' if owner == target_username.lstrip('@').casefold() else 'repost'
        patches[str(item['id'])] = {'source_type': kind, 'source_types': [kind],
                                   'source_apis': list(item.get('source_apis', [])),
                                   'source_confidence': 'inferred', 'source_basis': 'username_rule',
                                   'source_updated_at': datetime.now(LOCAL_ZONE).isoformat()}
    return patches
