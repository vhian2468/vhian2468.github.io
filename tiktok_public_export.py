"""Public thumbnail web projection. Unknown/private fields fail closed.

The working database is not trusted to carry journal-only values.  Repost time
and labels are published only from an explicit, converged manual-journal
projection supplied by the caller.  Revision heads, conflicts, history and
notes are never copied to the public document.
"""
import copy
from datetime import datetime
import re

PUBLIC_VIDEO_FIELDS = frozenset((
    'id', 'uniqueId', 'nickname', 'desc', 'cover', 'cover_off', 'duration',
    'date', 'createTime', 'create_time', 'music_title', 'music_id',
    'digg', 'comment', 'share', 'play', 'collect',
    'repost_date', 'repost_caption', 'repost_order', 'feed_index',
    'source_type', 'source_types', 'source_confidence', 'hashtags',
))


def _public_repost_time(value):
    """Return one canonical current time value, or ``None`` on any ambiguity."""
    if not isinstance(value, dict) or value.get('kind') not in ('exact', 'range'):
        return None
    expected = ({'kind', 'zone', 'at'} if value.get('kind') == 'exact'
                else {'kind', 'zone', 'lower', 'upper'})
    if set(value) != expected:
        return None
    zone = value.get('zone')
    if not isinstance(zone, str) or not zone or len(zone) > 100:
        return None

    def instant(text):
        if (not isinstance(text, str) or not re.fullmatch(
                r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|[+-]\d\d:\d\d)', text)):
            raise ValueError('not text')
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('missing timezone')
        return parsed

    try:
        if value['kind'] == 'exact':
            instant(value['at'])
        elif instant(value['lower']) > instant(value['upper']):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return copy.deepcopy(value)


def _public_tags(value):
    if (not isinstance(value, list) or len(value) > 30 or
            any(not isinstance(tag, str) or not tag.strip() or len(tag) > 60
                for tag in value)):
        return None
    cleaned = [tag.strip() for tag in value]
    if len(set(cleaned)) != len(cleaned):
        return None
    return sorted(cleaned)


def public_videos(videos, manual_values=None):
    """Leave sources untouched and publish only whitelisted materialized values.

    ``manual_values`` must be the current projection returned by
    ``ManagerJournal.display_values``.  A conflicted or malformed field is
    omitted rather than choosing a winner.  Direct ``repost_time``/``tags``
    keys in ``videos`` are deliberately ignored.
    """
    manual_values = manual_values if isinstance(manual_values, dict) else {}

    def public_value(key, value):
        if isinstance(value, (str, int, float, bool)) or value is None:
            return True
        return key in ('source_types', 'hashtags') and isinstance(value, list) and all(isinstance(item, str) for item in value)

    result = []
    for video in videos:
        public = {key: copy.deepcopy(value) for key, value in video.items()
                  if key in PUBLIC_VIDEO_FIELDS and public_value(key, value)}
        projected = manual_values.get(str(video.get('id')), {})
        if isinstance(projected, dict):
            conflicts = projected.get('manual_conflicts', [])
            conflicts = (set(conflicts) if isinstance(conflicts, list) and
                         all(isinstance(field, str) for field in conflicts)
                         else {'repost_time', 'tags'})
            if 'repost_time' not in conflicts:
                stamp = _public_repost_time(projected.get('repost_time'))
                if stamp is not None:
                    public['repost_time'] = stamp
            if 'tags' not in conflicts:
                tags = _public_tags(projected.get('tags'))
                if tags:
                    public['tags'] = tags
        result.append(public)
    return result
