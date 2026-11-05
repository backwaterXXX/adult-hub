"""Execution loop for durable duplicate decision jobs."""
from __future__ import annotations
import uuid
import logging


logger = logging.getLogger(__name__)

if __package__:
    from . import duplicate_jobs as _core
    from . import duplicate_job_external_handlers as _external_handlers
    from .duplicate_job_undo import _run_undo
    from .duplicate_job_files import (
        _append_sidecar_moves, _check_journal_ids, _load_recovery_snapshots,
        _move_hidden_files,
    )
    from .duplicate_job_quarantine import _run_quarantine
else:
    import duplicate_jobs as _core
    import duplicate_job_external_handlers as _external_handlers
    from duplicate_job_undo import _run_undo
    from duplicate_job_files import (
        _append_sidecar_moves, _check_journal_ids, _load_recovery_snapshots,
        _move_hidden_files,
    )
    from duplicate_job_quarantine import _run_quarantine


class _JobRun:
    """Mutable state of one claimed job, read by the failure path to decide on compensation."""

    def __init__(self) -> None:
        self.moved: list[tuple[_core.Path, _core.Path]] = []
        self.catalog_committed = False


def _claim_next_job(conn, worker_actions, deferred, services):
    """Atomically move the oldest queued job to 'processing'. Returns None when no job can be claimed."""
    try:
        conn.execute('BEGIN')
        job = conn.execute("SELECT id, action, payload FROM video_duplicate_jobs WHERE state='queued' AND action = ANY(%s) AND NOT id = ANY(%s) ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED", (list(worker_actions), deferred)).fetchone()
        if not job:
            conn.commit()
            return None
        job_id, action, payload_raw = job
        now = int(_core.time.time())
        conn.execute("UPDATE video_duplicate_jobs SET state='processing', updated_at=%s WHERE id=%s AND state='queued'", (now, job_id))
        conn.commit()
    except Exception:
        conn.rollback()
        services.logger.exception('Could not claim duplicate decision job')
        return None
    return job_id, action, payload_raw, now


def _load_job_payload(conn, job_id, action, payload_raw, services):
    """Parse the job payload. Returns None after marking a corrupt job failed."""
    try:
        payload = _core.json.loads(payload_raw)
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        failure = _core.json.dumps({'error': 'Corrupt job payload; marked failed without execution.'}, ensure_ascii=False)
        with conn:
            conn.execute("UPDATE video_duplicate_jobs SET state='failed', result=%s, updated_at=%s WHERE id=%s AND state='processing'", (failure, int(_core.time.time()), job_id))
        services.logger.warning('Duplicate operation %s has a corrupt payload; marked failed', job_id)
        return None
    if action == 'performer_merge' and payload.get('expected_revision'):
        prior = conn.execute('SELECT result FROM video_duplicate_jobs WHERE id=%s', (job_id,)).fetchone()
        try:
            prior_result = _core.json.loads(prior[0] or '{}') if prior else {}
        except (TypeError, ValueError):
            prior_result = {}
        prior_progress = prior_result.get('progress') if isinstance(prior_result, dict) else None
        if isinstance(prior_progress, dict) and prior_progress.get('stage') == 'merging_performers':
            resume = payload.get('_resume') if isinstance(payload.get('_resume'), dict) else {}
            resume['merge_started'] = True
            payload['_resume'] = resume
    return payload


def _committed_result_without_rerun(conn, job_id, action, payload, services):
    """Return the result of a job whose catalog change committed before a crash, or None to run it."""
    if action == 'quarantine':
        video_id = str(payload.get('video_id') or '')
        committed = conn.execute("SELECT 1 FROM video_duplicate_decisions WHERE loser_id=%s AND keeper_id=%s AND operation_id=%s AND state='active'", (video_id, video_id, job_id)).fetchone()
        if committed and (not conn.execute('SELECT 1 FROM videos WHERE id=%s', (video_id,)).fetchone()):
            refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, 'quarantined', services)
            return {'operation_id': job_id, 'quarantined': [video_id], 'local_state': 'quarantined', 'jellyfin_refresh': 'updated' if refreshed else 'failed'}
    if action == 'hide':
        requested = {str(x) for x in payload.get('loser_ids', [])}
        committed = {r[0] for r in conn.execute("SELECT loser_id FROM video_duplicate_decisions WHERE operation_id=%s AND state='active'", (job_id,)).fetchall()}
        if requested and requested == committed:
            refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, 'hidden', services)
            return {'operation_id': job_id, 'hidden': sorted(requested), 'keeper_id': payload.get('keeper_id'), 'local_state': 'hidden', 'jellyfin_refresh': 'updated' if refreshed else 'failed'}
    if action == 'undo':
        operation = str(payload.get('operation_id') or '')
        active = conn.execute("SELECT count(*) FROM video_duplicate_decisions WHERE operation_id=%s AND state='active'", (operation,)).fetchone()[0]
        undone = conn.execute("SELECT loser_id FROM video_duplicate_decisions WHERE operation_id=%s AND state='undone'", (operation,)).fetchall()
        restored_ids = list(dict.fromkeys(str(row[0]) for row in undone))
        existing_ids = {
            str(row[0]) for row in conn.execute(
                'SELECT id FROM videos WHERE id = ANY(%s)', (restored_ids,)
            ).fetchall()
        } if restored_ids else set()
        if not active and restored_ids and existing_ids == set(restored_ids):
            refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, 'restored', services)
            return {'operation_id': operation, 'restored': restored_ids, 'local_state': 'restored', 'jellyfin_refresh': 'updated' if refreshed else 'failed'}
    return None


def _catalog_result(run, result):
    run.catalog_committed = True
    return result


def _run_catalog_full_audit(conn) -> dict:
    from common.job_queue import enqueue_jobs_batch
    batch_id = uuid.uuid4().hex
    audit_ids = enqueue_jobs_batch(getattr(conn, '_url', None), [
        ('metadata_quality_audit', {'audit_batch_id': batch_id}),
        ('consistency_audit', {'mode': 'file_consistency', 'audit_batch_id': batch_id, 'auto_repair': True}),
    ])
    return {'local_state': 'audit_queued', 'audit_batch_id': batch_id,
            'audit_job_ids': audit_ids}


def _run_external_action(action, conn, job_id, payload, media_dir, services, run) -> dict:
    result, committed = _external_handlers.execute_external_operation(
        action, conn, job_id, payload, media_dir, services
    )
    run.catalog_committed = committed
    return result


def _run_dismiss(conn, job_id, payload, now) -> dict:
    _core._record_job_progress(conn, job_id, 'saving_catalog')
    left, right = sorted((payload['left_id'], payload['right_id']))
    with conn:
        existing = conn.execute('SELECT active FROM video_duplicate_dismissals WHERE left_id=%s AND right_id=%s', (left, right)).fetchone()
        if existing and existing[0]:
            raise ValueError('This pair is already dismissed.')
        conn.execute('INSERT INTO video_duplicate_dismissals (left_id,right_id,active,operation_id,created_at) VALUES (%s,%s,1,%s,%s) ON CONFLICT(left_id,right_id) DO UPDATE SET active=1, operation_id=excluded.operation_id, created_at=excluded.created_at', (left, right, job_id, now))
    result = {'operation_id': job_id, 'local_state': 'dismissed'}
    return result


def _run_undismiss(conn, job_id, payload) -> dict:
    _core._record_job_progress(conn, job_id, 'saving_catalog')
    with conn:
        conn.execute('UPDATE video_duplicate_dismissals SET active=0 WHERE operation_id=%s', (payload['operation_id'],))
    result = {'operation_id': payload['operation_id'], 'local_state': 'dismissal undone'}
    return result


def _execute_action(conn, job_id, action, payload, media_dir, services, quarantine_root, now, run) -> dict:
    """Run the handler for one claimed job and return its result. Sets run.catalog_committed after catalog writes commit."""
    # Each entry pairs an action match with its runner. The first matching entry runs.
    dispatch = (
        (action == 'catalog_full_audit',
         lambda: _run_catalog_full_audit(conn)),
        (action == 'metadata_update',
         lambda: _catalog_result(run, _core._metadata_update(conn, job_id, payload, media_dir, services))),
        (action == 'performer_refresh',
         lambda: _catalog_result(run, _core._performer_refresh(conn, job_id, payload, media_dir, services))),
        (action == 'availability_check',
         lambda: _catalog_result(run, _core._availability_check(conn, job_id, payload, services))),
        (action == 'consistency_audit',
         lambda: _catalog_result(run, _core._consistency_audit(conn, job_id, media_dir, services, payload))),
        (action == 'metadata_quality_audit',
         lambda: _catalog_result(run, _core._metadata_quality_audit(conn, job_id, payload))),
        (action == 'audit_repair_nfo',
         lambda: _catalog_result(run, _core._audit_repair_nfo(conn, job_id, payload, media_dir, services))),
        (action == 'audit_import_refresh',
         lambda: _catalog_result(run, _core._audit_import_refresh(conn, job_id, payload, services))),
        (action == 'orphan_file_operation',
         lambda: _catalog_result(run, _core._orphan_file_operation(conn, job_id, payload, media_dir, quarantine_root, services, run.moved))),
        (action == 'backup_create'
         or action == 'backup_restore_test'
         or action == 'metadata_backfill'
         or action == 'duplicate_reconcile'
         or action == 'telegram_fingerprint_scan'
         or action == 'artwork_download'
         or action == 'artwork_generate'
         or action == 'collections_refresh'
         or action == 'performer_merge'
         or action == 'history_cleanup'
         or action == 'performer_catalog'
         or action == 'scene_backfill'
         or action == 'telegram_title_cleanup'
         or action == 'source_remove'
         or action == 'source_readd',
         lambda: _run_external_action(action, conn, job_id, payload, media_dir, services, run)),
        (action == 'dismiss',
         lambda: _run_dismiss(conn, job_id, payload, now)),
        (action == 'undismiss',
         lambda: _run_undismiss(conn, job_id, payload)),
        (action == 'hide',
         lambda: _run_hide(conn, job_id, payload, media_dir, services, quarantine_root, now, run)),
        (action == 'quarantine',
         lambda: _run_quarantine(conn, job_id, payload, media_dir, services, quarantine_root, now, run)),
        (action == 'undo',
         lambda: _run_undo(conn, job_id, payload, media_dir, services, quarantine_root, now, run)),
    )
    for matched, run_action in dispatch:
        if matched:
            return run_action()
    raise ValueError('Unsupported job type.')


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


def _require_pair_evidence(conn, pair_evidence, keeper: str, losers: list, manual_review: bool) -> None:
    for loser in losers:
        if not manual_review and not pair_evidence(conn, keeper, loser):
            raise ValueError('Duplicate identity has not been confirmed, or duration/part safeguards failed.')


def _load_hide_validation(conn, validation_ids: list, expected_revisions: dict) -> tuple:
    revision_snapshots = {}
    if expected_revisions:
        from common.admin_operations import _video_snapshots
        revision_snapshots = _video_snapshots(conn, validation_ids)
        video_rows = {
            video_id: snapshot[1]['video']
            for video_id, snapshot in revision_snapshots.items()
        }
        active_decisions = {
            video_id for video_id, (_revision, snapshot) in revision_snapshots.items()
            if snapshot.get('active_decision')
        }
    else:
        rows = conn.execute(
            'SELECT id,provider,slug,duration,performers FROM videos WHERE id = ANY(%s)',
            (validation_ids,),
        ).fetchall()
        video_rows = {
            str(row[0]): {
                'provider': row[1], 'slug': row[2], 'duration': row[3], 'performers': row[4],
            }
            for row in rows
        }
        decision_rows = conn.execute(
            "SELECT loser_id,keeper_id FROM video_duplicate_decisions "
            "WHERE state='active' AND loser_id = ANY(%s)",
            (validation_ids,),
        ).fetchall()
        active_decisions = {str(row[0]) for row in decision_rows}
        revision_snapshots = {}
    return video_rows, active_decisions, revision_snapshots


def _check_hide_fingerprints(video_rows: dict, fingerprints: dict) -> None:
    if fingerprints:
        for video_id, expected in fingerprints.items():
            current_row = video_rows.get(str(video_id))
            if current_row is None:
                raise ValueError('Data changed since preview; refresh scan.')
            current = tuple(current_row.get(field) for field in ('provider', 'slug', 'duration', 'performers'))
            actual = _core.hashlib.sha256(_core.json.dumps(current, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
            if actual != expected:
                raise ValueError('Metadata changed since preview; refresh scan.')


def _check_hide_losers(conn, keeper: str, losers: list, validation_ids: list, video_rows: dict,
                       active_decisions: set, services) -> None:
    pair_keys = {tuple(sorted((keeper, loser))) for loser in losers}
    pair_left_ids = list(dict.fromkeys(pair[0] for pair in pair_keys))
    pair_right_ids = list(dict.fromkeys(pair[1] for pair in pair_keys))
    dismissed_pairs = {
        (str(row[0]), str(row[1]))
        for row in conn.execute(
            'SELECT left_id,right_id FROM video_duplicate_dismissals '
            'WHERE active=1 AND left_id = ANY(%s) AND right_id = ANY(%s)',
            (pair_left_ids, pair_right_ids),
        ).fetchall()
    }
    decision_rows = conn.execute(
        "SELECT loser_id,keeper_id FROM video_duplicate_decisions "
        "WHERE state='active' AND loser_id = ANY(%s)",
        (validation_ids,),
    ).fetchall()
    active_edges = {(str(row[0]), str(row[1])) for row in decision_rows}
    for loser in losers:
        if loser not in video_rows:
            raise ValueError(f'Item to hide no longer exists: {loser}')
        pair_left, pair_right = sorted((keeper, loser))
        if (pair_left, pair_right) in dismissed_pairs:
            raise ValueError('Pair was marked as different videos since preview.')
        if services.video_is_disabled(loser):
            raise ValueError('Source of this item is disabled; decision was not saved.')
        if loser in active_decisions:
            raise ValueError('Item already has an active deduplication decision.')
        if (keeper, loser) in active_edges:
            raise ValueError('Decision would create a cycle.')


def _check_expected_revisions(keeper: str, expected_revisions: dict, revision_snapshots: dict) -> None:
    if expected_revisions:
        for video_id, expected in expected_revisions.items():
            current_revision, current = revision_snapshots.get(str(video_id), (None, None))
            if current_revision != expected or current is None or current.get('active_decision') or current.get('source_disabled') or (str(video_id) != keeper and current.get('shared_paths')):
                raise ValueError('Library data changed after the preview; create a new operation plan.')


def _hide_file_snapshots(files: list, shared_paths: set, op_dir, media_dir, services) -> list:
    file_snapshots = []
    for sf in files:
        relative = sf.get('file_path') or ''
        if relative in shared_paths:
            raise ValueError('File is shared by another entry; aborting hide.')
        _append_sidecar_moves(services, relative, media_dir, op_dir, file_snapshots)
    return file_snapshots


def _build_hide_snapshots(conn, losers: list, revision_snapshots: dict, op_dir, media_dir, services, snapshots: list) -> None:
    from common.admin_operations import _video_snapshots
    hide_snapshots = dict(revision_snapshots)
    missing_snapshot_ids = [loser for loser in losers if loser not in hide_snapshots]
    if missing_snapshot_ids:
        hide_snapshots.update(_video_snapshots(conn, missing_snapshot_ids))
    for loser in losers:
        _revision, source_snapshot = hide_snapshots.get(loser, (None, None))
        if source_snapshot is None:
            raise ValueError(f'Item to hide no longer exists: {loser}')
        video = source_snapshot['video']
        files = source_snapshot['files']
        cats = source_snapshot['categories']
        shared_paths = {
            str(shared.get('file_path') or '')
            for shared in source_snapshot.get('shared_paths', [])
        }
        file_snapshots = _hide_file_snapshots(files, shared_paths, op_dir, media_dir, services)
        snapshots.append({
            'video': video,
            'synced_files': files,
            'categories': cats,
            'files': file_snapshots,
        })


def _run_hide(conn, job_id, payload, media_dir, services, quarantine_root, now, run) -> dict:
    """Validate and quarantine duplicate items under one keeper, merge their metadata, and store undo snapshots."""
    _core._record_job_progress(conn, job_id, 'validating')
    keeper = str(payload['keeper_id'])
    losers = [str(x) for x in payload['loser_ids']]
    if not losers or keeper in losers or len(set(losers)) != len(losers):
        raise ValueError('Invalid item list.')
    from common.duplicate_evidence import pair_evidence
    manual_review = payload.get('operator_review') is True and bool(payload.get('expected_revisions'))
    _require_pair_evidence(conn, pair_evidence, keeper, losers, manual_review)
    fingerprints = payload.get('fingerprints') or {}
    expected_revisions = payload.get('expected_revisions') or {}
    validation_ids = list(dict.fromkeys([
        keeper, *losers,
        *(str(video_id) for video_id in fingerprints),
        *(str(video_id) for video_id in expected_revisions),
    ]))
    video_rows, active_decisions, revision_snapshots = _load_hide_validation(conn, validation_ids, expected_revisions)
    if keeper not in video_rows:
        raise ValueError('Keeper item no longer exists.')
    if keeper in active_decisions:
        raise ValueError('Keeper item is already hidden; choose an active item.')
    _check_hide_fingerprints(video_rows, fingerprints)
    _check_hide_losers(conn, keeper, losers, validation_ids, video_rows, active_decisions, services)
    _check_expected_revisions(keeper, expected_revisions, revision_snapshots)
    quarantine_root.mkdir(parents=True, exist_ok=True)
    op_dir = quarantine_root / job_id
    op_dir.mkdir(parents=True, exist_ok=True)
    services.write_catalog_backup(conn, op_dir, list(losers))
    recovery_snapshots = _load_recovery_snapshots(conn, job_id)
    if recovery_snapshots is not None:
        _check_journal_ids(recovery_snapshots, set(losers))
    snapshots = recovery_snapshots if recovery_snapshots is not None else []
    if recovery_snapshots is None and losers:
        _build_hide_snapshots(conn, losers, revision_snapshots, op_dir, media_dir, services, snapshots)
    _core._store_job_result_field(conn, job_id, 'operation_id', job_id)
    _core._store_job_result_field(conn, job_id, 'snapshots', snapshots)
    _move_hidden_files(conn, job_id, snapshots, services, run)
    _core._record_job_progress(conn, job_id, 'saving_catalog')
    with conn:
        _merge_keeper_metadata(conn, keeper, job_id, payload, snapshots, media_dir, services, now)
    run.catalog_committed = True
    refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, 'hidden', services)
    result = {'operation_id': job_id, 'hidden': losers, 'keeper_id': keeper, 'local_state': 'hidden', 'jellyfin_refresh': 'updated' if refreshed else 'failed'}
    _core._finish_job(conn, job_id, result)
    conn.commit()
    return result


def _queue_audit_auto_repairs(conn, job_id, services) -> None:
    """Queue safe NFO repairs after a complete library audit. Failures are logged, not raised."""
    try:
        # Only missing NFO sidecars are repaired unattended. Artwork
        # preview URLs can be syntactically present but unavailable
        # (Telegram returns 404 for some), which used to fail the
        # whole 25-item batch and requeue the same findings nightly.
        # Keep artwork findings visible for explicit repair instead.
        from common.admin_operation_audits import preview_audit_repair
        from common.admin_operations import execute_operation
        finding_ids = [int(row[0]) for row in conn.execute(
            "SELECT i.id FROM admin_audit_items i JOIN videos v ON v.id=i.video_id "
            "WHERE i.job_id=%s AND i.status='open' AND i.kind='missing_nfo' "
            "ORDER BY i.id LIMIT 25", (job_id,),
        ).fetchall()]
        if finding_ids:
            preview = preview_audit_repair(getattr(conn, '_url', None), str(job_id), finding_ids)
            if preview.get('outcome') == 'previewed':
                queued = execute_operation(getattr(conn, '_url', None), preview['plan_id'])
                if queued.get('outcome') == 'queued':
                    services.logger.info('Queued automatic repair for %s missing local files after audit %s', len(finding_ids), job_id)
    except Exception:
        services.logger.exception('Could not queue safe automatic repairs after audit %s', job_id)


def _compensate_moves(moved, services) -> None:
    """Move files back to their original paths after a failed job that did not commit catalog changes."""
    for origin, target in reversed(moved):
        try:
            if target.exists() and (not origin.exists()):
                origin.parent.mkdir(parents=True, exist_ok=True)
                services.duplicate_move_file(target, origin)
        except Exception:
            services.logger.exception('Could not compensate duplicate operation file move')


def _record_job_failure(conn, job_id, action, exc, services) -> None:
    """Mark a processing job failed and store the error message and failure code in its result."""
    previous = conn.execute('SELECT result FROM video_duplicate_jobs WHERE id=%s', (job_id,)).fetchone()
    try:
        failure_result = _core.json.loads(previous[0]) if previous and previous[0] else {}
        if not isinstance(failure_result, dict):
            failure_result = {}
    except (TypeError, ValueError):
        failure_result = {}
    failure_result['error'] = str(exc)[:300]
    from common.job_failures import classify_job_failure
    failure_code = classify_job_failure(action, failure_result)
    if failure_code:
        failure_result['failure_code'] = failure_code
    with conn:
        conn.execute("UPDATE video_duplicate_jobs SET state='failed', result=%s, updated_at=%s WHERE id=%s AND state='processing'", (_core.json.dumps(failure_result, ensure_ascii=False), int(_core.time.time()), job_id))
    services.logger.warning('Duplicate operation %s failed: %s', job_id, exc)


def process_video_duplicate_jobs_locked(conn: _core.db.Connection, media_dir: _core.Path, services: _core.DuplicateJobServices, *, include_long_running: bool=False) -> int:
    """Run queued duplicate decisions in the sync process, with file quarantine and undo snapshots."""
    worker_actions = tuple((action for action in _core.WORKER_ACTIONS if include_long_running or action not in _core._LONG_RUNNING_WORKER_ACTIONS))
    now = int(_core.time.time())
    # Jobs left 'processing' belong to a crashed run of this same worker: every
    # caller holds .adult-hub-sync.lock exclusively, so no live worker can own
    # them and they are requeued immediately.
    recovered = conn.execute("UPDATE video_duplicate_jobs SET state='queued', updated_at=%s WHERE state='processing' AND action = ANY(%s)", (now, list(worker_actions))).rowcount
    if recovered:
        services.logger.info('Requeued %s duplicate job(s) left processing by a dead worker', recovered)
    conn.commit()
    completed = 0
    # A re-added channel parked behind an active removal keeps its created_at, so
    # claiming the oldest queued job would pick it again every iteration and stall
    # the rest of the queue. Deferred ids are skipped for the rest of this pass.
    deferred: list[str] = []
    while True:
        claimed = _claim_next_job(conn, worker_actions, deferred, services)
        if claimed is None:
            break
        job_id, action, payload_raw, now = claimed
        payload = _load_job_payload(conn, job_id, action, payload_raw, services)
        if payload is None:
            continue
        _core._record_job_progress(conn, job_id, 'starting')
        quarantine_root = services.jobs_lock_root(conn, media_dir) / 'duplicate-quarantine'
        run = _JobRun()
        early_result = _committed_result_without_rerun(conn, job_id, action, payload, services)
        if early_result is not None:
            _core._finish_job(conn, job_id, early_result)
            completed += 1
            continue
        heartbeat_stop = _core.threading.Event()
        heartbeat = _core.threading.Thread(target=_core._heartbeat_processing_job, args=(getattr(conn, '_url', None) or _core.os.getenv('DATABASE_URL'), job_id, heartbeat_stop, services.logger), name=f'job-heartbeat-{job_id[:8]}', daemon=True)
        heartbeat.start()
        try:
            result = _execute_action(conn, job_id, action, payload, media_dir, services, quarantine_root, now, run)
            _core._finish_job(conn, job_id, result)
            if action == 'consistency_audit' and result.get('library_inventory_status') == 'complete' and payload.get('auto_repair'):
                _queue_audit_auto_repairs(conn, job_id, services)
            completed += 1
        except _external_handlers.ChannelRemovalStillActive as retry_exc:
            with conn:
                conn.execute(
                    "UPDATE video_duplicate_jobs SET state='queued', updated_at=%s WHERE id=%s AND state='processing'",
                    (int(_core.time.time()), job_id),
                )
            deferred.append(job_id)
            services.logger.info("Channel removal is still active; deferred job %s to the next pass: %s", job_id, retry_exc)
            continue
        except Exception as exc:
            if not run.catalog_committed and action != 'orphan_file_operation':
                _compensate_moves(run.moved, services)
            _record_job_failure(conn, job_id, action, exc, services)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
    return completed
