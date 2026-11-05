"""Duplicate-job merge steps: keeper selection, loser merge and keeper metadata.

Moved verbatim from duplicate_job_worker.py. Shared state still comes from the
duplicate_jobs facade through ``_core``, so test patches on the facade keep applying.
"""

from __future__ import annotations

import logging

if __package__:
    from . import duplicate_jobs as _core
else:
    import duplicate_jobs as _core

logger = logging.getLogger(__name__)


def _epoch(value):
    if value is None:
        return 0.0
    return float(value.timestamp()) if hasattr(value, 'timestamp') else float(value)


def _iso_timestamp(value):
    if value is None:
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return _core.time.strftime('%Y-%m-%dT%H:%M:%SZ', _core.time.gmtime(float(value)))


def _parse_list_field(val):
    if not val:
        return []
    if isinstance(val, list):
        return val
    try:
        if isinstance(val, str) and val.startswith('['):
            parsed = _core.json.loads(val)
            return parsed if isinstance(parsed, list) else []
        return [x.strip() for x in str(val).split(',') if x.strip()]
    except Exception:
        logger.debug("Ignored exception in _parse_list_field", exc_info=True)
        return []


def _performer_priority(video):
    resolution = video.get('performer_resolution') or {}
    if isinstance(resolution, str):
        try:
            resolution = _core.json.loads(resolution)
        except (TypeError, ValueError):
            resolution = {}
    source = str(video.get('scene_match_source') or '').casefold()
    return (100 if any(token in source for token in ('oshash', 'phash', 'fingerprint'))
            else 70 if source else int((resolution or {}).get('priority') or 10))


class _KeeperMerge:
    """Keeper row and metadata candidates gathered while losers are merged into it."""

    def __init__(self, conn, keeper: str) -> None:
        self.k_row = conn.execute('SELECT * FROM videos WHERE id=%s', (keeper,)).fetchone()
        self.k_cols = _core.db.table_columns(conn, 'videos')
        self.k_vid = dict(zip(self.k_cols, self.k_row)) if self.k_row else {}
        self.override_row = conn.execute(
            'SELECT fields,updated_at FROM video_metadata_overrides WHERE video_id=%s',
            (keeper,),
        ).fetchone()
        self.override_fields = (_core.json.loads(self.override_row[0])
                                if self.override_row and isinstance(self.override_row[0], str)
                                else dict(self.override_row[0] or {}) if self.override_row else {})
        self.before = {
            'video': dict(self.k_vid),
            'overrides': self.override_fields,
            'override_updated_at': _epoch(self.override_row[1]) if self.override_row else None,
        }
        self.merged_perfs = set(_parse_list_field(self.k_vid.get('performers')))
        self.merged_cats = set(_parse_list_field(self.k_vid.get('categories')))
        self.merged_tags = set(_parse_list_field(self.k_vid.get('tags')))
        self.k_studio = (self.k_vid.get('studio') or '').strip()
        self.metadata_fields = ('title', 'plot', 'studio', 'date_released')
        self.metadata_candidates = {field: [] for field in self.metadata_fields}
        self.list_override_candidates = {'performers': [], 'tags': []}
        self.performer_candidates = []
        self.merged_override_fields = None
        keeper_performers = _parse_list_field(self.k_vid.get('performers'))
        if keeper_performers:
            self.performer_candidates.append((_performer_priority(self.k_vid), len(keeper_performers),
                                              True, keeper_performers))
        self.override_time = _epoch(self.override_row[1]) if self.override_row else 0.0
        for field in self.list_override_candidates:
            if field in self.override_fields:
                self.list_override_candidates[field].append((self.override_time, True,
                                                             self.override_fields[field]))
        for field in self.metadata_fields:
            value = self.k_vid.get(field)
            if value not in (None, ''):
                self.metadata_candidates[field].append((10, 0.0, True, str(value)))


def _attach_keeper_source(conn, keeper: str, now) -> None:
    keeper_channel = keeper.split(':')[1] if ':' in keeper and len(keeper.split(':')) >= 2 else ''
    conn.execute('INSERT INTO video_sources\n                        (video_id, source_id, provider, provider_ref, source_url, channel_id, priority, is_active, created_at)\n                        VALUES (%s, %s, %s, %s, %s, %s, 1, 1, %s)\n                        ON CONFLICT (source_id) DO NOTHING', (keeper, keeper, 'telegram', keeper, f"https://t.me/c/{(keeper_channel[4:] if keeper_channel.startswith('-100') else keeper_channel.lstrip('-'))}" if keeper_channel else '', keeper_channel, now))


def _collect_loser_candidates(merge: _KeeperMerge, l_vid: dict, loser_overrides: dict,
                              loser_override_time: float) -> None:
    source_priority = _performer_priority(l_vid)
    for field in merge.metadata_fields:
        value = l_vid.get(field)
        if value not in (None, ''):
            merge.metadata_candidates[field].append((source_priority, 0.0, False, str(value)))
    for field, value in loser_overrides.items():
        if field in merge.metadata_candidates:
            merge.metadata_candidates[field].append((1000, loser_override_time, False, str(value)))
        elif field in merge.list_override_candidates:
            merge.list_override_candidates[field].append((loser_override_time, False, value))
    loser_performers = _parse_list_field(l_vid.get('performers'))
    if loser_performers:
        merge.performer_candidates.append((_performer_priority(l_vid), len(loser_performers),
                                           False, loser_performers))
    for field, value in merge.override_fields.items():
        if field in merge.metadata_candidates:
            merge.metadata_candidates[field].append((1000, merge.override_time, True, str(value)))


def _copy_scene_match_and_thumbnail(k_vid: dict, l_vid: dict) -> None:
    if (not k_vid.get('scene_match_external_id') and l_vid.get('scene_match_external_id')):
        k_vid['scene_match_source'] = l_vid.get('scene_match_source')
        k_vid['scene_match_external_id'] = l_vid.get('scene_match_external_id')
        k_vid['scene_match_url'] = l_vid.get('scene_match_url')
        k_vid['scene_match_confidence'] = l_vid.get('scene_match_confidence')
    if not k_vid.get('thumbnail') and l_vid.get('thumbnail'):
        k_vid['thumbnail'] = l_vid.get('thumbnail')


def _attach_loser_source(conn, keeper: str, loser: str, now) -> None:
    l_channel = loser.split(':')[1] if ':' in loser and len(loser.split(':')) >= 2 else ''
    conn.execute('INSERT INTO video_sources\n                            (video_id, source_id, provider, provider_ref, source_url, channel_id, priority, is_active, created_at)\n                            VALUES (%s, %s, %s, %s, %s, %s, 2, 1, %s)\n                            ON CONFLICT (source_id) DO UPDATE SET\n                                video_id=EXCLUDED.video_id, provider=EXCLUDED.provider,\n                                provider_ref=EXCLUDED.provider_ref, source_url=EXCLUDED.source_url,\n                                channel_id=EXCLUDED.channel_id, priority=EXCLUDED.priority,\n                                is_active=EXCLUDED.is_active, created_at=EXCLUDED.created_at', (keeper, loser, 'telegram', loser, f"https://t.me/c/{(l_channel[4:] if l_channel.startswith('-100') else l_channel.lstrip('-'))}" if l_channel else '', l_channel, now))


def _merge_loser_sets(merge: _KeeperMerge, l_vid: dict) -> None:
    merge.merged_perfs.update(_parse_list_field(l_vid.get('performers')))
    merge.merged_cats.update(_parse_list_field(l_vid.get('categories')))
    merge.merged_tags.update(_parse_list_field(l_vid.get('tags')))
    l_studio = (l_vid.get('studio') or '').strip()
    if l_studio and l_studio != merge.k_studio:
        merge.merged_tags.add(l_studio)
    if not merge.k_studio and l_studio:
        merge.k_studio = l_studio


def _pick_metadata_winners(merge: _KeeperMerge) -> None:
    merge.merged_override_fields = dict(merge.override_fields)
    for field, candidates in merge.metadata_candidates.items():
        if not candidates:
            continue
        candidates.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        winner = candidates[0][3]
        if field in {'title', 'plot', 'studio', 'date_released'}:
            merge.k_vid[field] = winner
        if candidates[0][0] >= 1000:
            merge.merged_override_fields[field] = winner
    merge.k_studio = merge.k_vid.get('studio') or ''


def _retire_loser_rows(conn, keeper: str, loser: str, job_id, snapshot: dict, now, payload: dict) -> None:
    conn.execute('DELETE FROM video_categories WHERE video_id=%s', (loser,))
    conn.execute('DELETE FROM synced_files WHERE video_id=%s', (loser,))
    conn.execute('DELETE FROM videos WHERE id=%s', (loser,))
    conn.execute("INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot,rules_version) VALUES (%s,%s,%s,'active',%s,%s,%s)", (loser, keeper, job_id, now, _core.json.dumps(snapshot, ensure_ascii=False), payload.get('rules_version')))


def _merge_loser(conn, merge: _KeeperMerge, keeper: str, job_id, payload: dict, snapshot: dict, now) -> None:
    loser = snapshot['video']['id']
    l_vid = snapshot.get('video') or {}
    snapshot['keeper_before'] = merge.before
    if payload.get('automatic'):
        snapshot['automatic'] = True
        snapshot['duplicate_evidence'] = str(payload.get('evidence') or '')[:40]
    loser_override_row = conn.execute(
        'SELECT fields,updated_at,revision FROM video_metadata_overrides WHERE video_id=%s',
        (loser,),
    ).fetchone()
    loser_overrides = (_core.json.loads(loser_override_row[0])
                       if loser_override_row and isinstance(loser_override_row[0], str)
                       else dict(loser_override_row[0] or {}) if loser_override_row else {})
    if loser_override_row:
        snapshot['metadata_overrides'] = {
            'fields': loser_overrides,
            'updated_at': _iso_timestamp(loser_override_row[1]),
            'revision': int(loser_override_row[2]),
        }
    loser_override_time = _epoch(loser_override_row[1]) if loser_override_row else 0.0
    _collect_loser_candidates(merge, l_vid, loser_overrides, loser_override_time)
    _copy_scene_match_and_thumbnail(merge.k_vid, l_vid)
    _attach_loser_source(conn, keeper, loser, now)
    _merge_loser_sets(merge, l_vid)
    _pick_metadata_winners(merge)
    _retire_loser_rows(conn, keeper, loser, job_id, snapshot, now, payload)


def _apply_list_override_winners(merge: _KeeperMerge) -> None:
    for field, candidates in merge.list_override_candidates.items():
        if candidates:
            candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
            value = candidates[0][2]
            if field == 'performers':
                merge.merged_perfs = set(_parse_list_field(value))
            else:
                merge.merged_tags = set(_parse_list_field(value))
            merge.merged_override_fields[field] = value


def _strongest_performers(merge: _KeeperMerge) -> None:
    if not merge.list_override_candidates['performers'] and merge.performer_candidates:
        strongest_priority = max(item[0] for item in merge.performer_candidates)
        merge.merged_perfs = {
            performer for priority, _length, _is_keeper, values in merge.performer_candidates
            if priority == strongest_priority for performer in values
        }


def _refresh_keeper_nfo(conn, merge: _KeeperMerge, keeper: str, media_dir, services, now) -> None:
    k_vid = merge.k_vid
    try:
        k_file = conn.execute('SELECT file_path FROM synced_files WHERE video_id=%s', (keeper,)).fetchone()
        if k_file:
            k_strm = services.resolve_media_path(k_file[0], media_dir)
            k_nfo = k_strm.with_suffix('.nfo')
            if k_nfo.exists():
                k_dict = {'id': keeper, 'provider': k_vid.get('provider', 'telegram'), 'provider_video_id': keeper.split(':', 1)[-1] if ':' in keeper else keeper, 'title': k_vid.get('title', ''), 'slug': k_vid.get('slug', ''), 'duration': k_vid.get('duration', 0), 'thumbnail': k_vid.get('thumbnail', ''), 'performers': sorted(merge.merged_perfs), 'performer_thumbs': services.load_performer_thumbs(conn, sorted(merge.merged_perfs)), 'studio': k_vid.get('studio') or '', 'plot': k_vid.get('plot', ''), 'date_released': k_vid.get('date_released', ''), 'dateadded': services.format_dateadded(k_vid.get('first_seen') or now)}
                services.create_nfo_file(k_dict, set(merge.merged_cats), merge.merged_tags, k_nfo)
    except Exception as nfo_exc:
        services.logger.warning('Could not update keeper NFO metadata: %s', nfo_exc)


def _finalize_keeper_row(conn, merge: _KeeperMerge, keeper: str, job_id, snapshots, media_dir, services, now) -> None:
    k_vid = merge.k_vid
    merge.merged_perfs.update(_parse_list_field(k_vid.get('performers')))
    merge.merged_tags.update(_parse_list_field(k_vid.get('tags')))
    merge.merged_cats.update(_parse_list_field(k_vid.get('categories')))
    _apply_list_override_winners(merge)
    _strongest_performers(merge)
    conn.execute('UPDATE videos SET title=%s,plot=%s,studio=%s,date_released=%s,thumbnail=%s,performers=%s,categories=%s,tags=%s,scene_match_source=%s,scene_match_external_id=%s,scene_match_url=%s,scene_match_confidence=%s WHERE id=%s',
                 (k_vid.get('title'), k_vid.get('plot'), k_vid.get('studio'),
                  k_vid.get('date_released'), k_vid.get('thumbnail'),
                  _core.json.dumps(sorted(merge.merged_perfs), ensure_ascii=False),
                  _core.json.dumps(sorted(merge.merged_cats), ensure_ascii=False),
                  _core.json.dumps(sorted(merge.merged_tags), ensure_ascii=False),
                  k_vid.get('scene_match_source'), k_vid.get('scene_match_external_id'),
                  k_vid.get('scene_match_url'), k_vid.get('scene_match_confidence'), keeper))
    if merge.merged_override_fields:
        conn.execute('INSERT INTO video_metadata_overrides (video_id,fields,revision,updated_at) VALUES (%s,%s,1,CURRENT_TIMESTAMP) ON CONFLICT (video_id) DO UPDATE SET fields=EXCLUDED.fields,revision=video_metadata_overrides.revision+1,updated_at=CURRENT_TIMESTAMP',
                     (keeper, _core.json.dumps(merge.merged_override_fields, ensure_ascii=False)))
    keeper_after_row = conn.execute('SELECT * FROM videos WHERE id=%s', (keeper,)).fetchone()
    keeper_after = dict(zip(merge.k_cols, keeper_after_row)) if keeper_after_row else {}
    after_override = conn.execute(
        'SELECT fields FROM video_metadata_overrides WHERE video_id=%s', (keeper,),
    ).fetchone()
    keeper_after_overrides = (after_override[0] if after_override else {})
    if isinstance(keeper_after_overrides, str):
        keeper_after_overrides = _core.json.loads(keeper_after_overrides)
    conn.execute(
        "UPDATE video_duplicate_decisions SET snapshot=(jsonb_set(snapshot::jsonb,'{keeper_after}',%s::jsonb,TRUE)::text) "
        "WHERE operation_id=%s AND state='active'",
        (_core.json.dumps({'video': keeper_after, 'overrides': keeper_after_overrides}, ensure_ascii=False), job_id),
    )
    for snapshot in snapshots:
        snapshot['keeper_after'] = {'video': keeper_after, 'overrides': keeper_after_overrides}
    for cat in merge.merged_cats:
        conn.execute('INSERT INTO video_categories (video_id, category) VALUES (%s, %s) ON CONFLICT DO NOTHING', (keeper, cat))
    _refresh_keeper_nfo(conn, merge, keeper, media_dir, services, now)


def _merge_keeper_metadata(conn, keeper, job_id, payload, snapshots, media_dir, services, now) -> None:
    """Attach losers as sources of the keeper, merge their metadata into it, and record decisions."""
    _attach_keeper_source(conn, keeper, now)
    merge = _KeeperMerge(conn, keeper)
    for snapshot in snapshots:
        _merge_loser(conn, merge, keeper, job_id, payload, snapshot, now)
    if merge.k_row:
        _finalize_keeper_row(conn, merge, keeper, job_id, snapshots, media_dir, services, now)
