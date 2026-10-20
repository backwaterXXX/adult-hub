"""Execution loop for durable duplicate decision jobs."""
from __future__ import annotations

if __package__:
    from . import duplicate_jobs as _core
    from . import duplicate_job_external_handlers as _external_handlers
else:
    import duplicate_jobs as _core
    import duplicate_job_external_handlers as _external_handlers

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
    while True:
        try:
            conn.execute('BEGIN')
            job = conn.execute("SELECT id, action, payload FROM video_duplicate_jobs WHERE state='queued' AND action = ANY(%s) ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED", (list(worker_actions),)).fetchone()
            if not job:
                conn.commit()
                break
            job_id, action, payload_raw = job
            now = int(_core.time.time())
            conn.execute("UPDATE video_duplicate_jobs SET state='processing', updated_at=%s WHERE id=%s AND state='queued'", (now, job_id))
            conn.commit()
        except Exception:
            conn.rollback()
            services.logger.exception('Could not claim duplicate decision job')
            break
        try:
            payload = _core.json.loads(payload_raw)
        except (TypeError, ValueError):
            payload = None
        if not isinstance(payload, dict):
            failure = _core.json.dumps({'error': 'Corrupt job payload; marked failed without execution.'}, ensure_ascii=False)
            with conn:
                conn.execute("UPDATE video_duplicate_jobs SET state='failed', result=%s, updated_at=%s WHERE id=%s AND state='processing'", (failure, int(_core.time.time()), job_id))
            services.logger.warning('Duplicate operation %s has a corrupt payload; marked failed', job_id)
            continue
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
        _core._record_job_progress(conn, job_id, 'starting')
        quarantine_root = services.jobs_lock_root(conn, media_dir) / 'duplicate-quarantine'
        moved: list[tuple[_core.Path, _core.Path]] = []
        catalog_committed = False
        if action == 'quarantine':
            video_id = str(payload.get('video_id') or '')
            committed = conn.execute("SELECT 1 FROM video_duplicate_decisions WHERE loser_id=%s AND keeper_id=%s AND operation_id=%s AND state='active'", (video_id, video_id, job_id)).fetchone()
            if committed and (not conn.execute('SELECT 1 FROM videos WHERE id=%s', (video_id,)).fetchone()):
                refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, 'quarantined', services)
                result = {'operation_id': job_id, 'quarantined': [video_id], 'local_state': 'quarantined', 'jellyfin_refresh': 'updated' if refreshed else 'failed'}
                _core._finish_job(conn, job_id, result)
                completed += 1
                continue
        if action == 'hide':
            requested = {str(x) for x in payload.get('loser_ids', [])}
            committed = {r[0] for r in conn.execute("SELECT loser_id FROM video_duplicate_decisions WHERE operation_id=%s AND state='active'", (job_id,)).fetchall()}
            if requested and requested == committed:
                refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, 'hidden', services)
                result = {'operation_id': job_id, 'hidden': sorted(requested), 'keeper_id': payload.get('keeper_id'), 'local_state': 'hidden', 'jellyfin_refresh': 'updated' if refreshed else 'failed'}
                _core._finish_job(conn, job_id, result)
                completed += 1
                continue
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
                result = {'operation_id': operation, 'restored': restored_ids, 'local_state': 'restored', 'jellyfin_refresh': 'updated' if refreshed else 'failed'}
                _core._finish_job(conn, job_id, result)
                completed += 1
                continue
        heartbeat_stop = _core.threading.Event()
        heartbeat = _core.threading.Thread(target=_core._heartbeat_processing_job, args=(getattr(conn, '_url', None) or _core.os.getenv('DATABASE_URL'), job_id, heartbeat_stop, services.logger), name=f'job-heartbeat-{job_id[:8]}', daemon=True)
        heartbeat.start()
        try:
            if action == 'metadata_update':
                result = _core._metadata_update(conn, job_id, payload, media_dir, services)
                catalog_committed = True
            elif action == 'performer_refresh':
                result = _core._performer_refresh(conn, job_id, payload, media_dir, services)
                catalog_committed = True
            elif action == 'availability_check':
                result = _core._availability_check(conn, job_id, payload, services)
                catalog_committed = True
            elif action == 'consistency_audit':
                result = _core._consistency_audit(conn, job_id, media_dir, services, payload)
                catalog_committed = True
            elif action == 'metadata_quality_audit':
                result = _core._metadata_quality_audit(conn, job_id, payload)
                catalog_committed = True
            elif action == 'audit_repair_nfo':
                result = _core._audit_repair_nfo(conn, job_id, payload, media_dir, services)
                catalog_committed = True
            elif action == 'audit_import_refresh':
                result = _core._audit_import_refresh(conn, job_id, payload, services)
                catalog_committed = True
            elif action == 'orphan_file_operation':
                result = _core._orphan_file_operation(conn, job_id, payload, media_dir, quarantine_root, services, moved)
                catalog_committed = True
            elif (
                action == 'backup_create'
                or action == 'backup_restore_test'
                or action == 'metadata_backfill'
                or action == 'telegram_fingerprint_scan'
                or action == 'collections_refresh'
                or action == 'performer_merge'
            ):
                result, catalog_committed = _external_handlers.execute_external_operation(
                    action, conn, job_id, payload, media_dir, services
                )
            elif action == 'dismiss':
                _core._record_job_progress(conn, job_id, 'saving_catalog')
                left, right = sorted((payload['left_id'], payload['right_id']))
                with conn:
                    existing = conn.execute('SELECT active FROM video_duplicate_dismissals WHERE left_id=%s AND right_id=%s', (left, right)).fetchone()
                    if existing and existing[0]:
                        raise ValueError('This pair is already dismissed.')
                    conn.execute('INSERT INTO video_duplicate_dismissals (left_id,right_id,active,operation_id,created_at) VALUES (%s,%s,1,%s,%s) ON CONFLICT(left_id,right_id) DO UPDATE SET active=1, operation_id=excluded.operation_id, created_at=excluded.created_at', (left, right, job_id, now))
                result = {'operation_id': job_id, 'local_state': 'dismissed'}
            elif action == 'undismiss':
                _core._record_job_progress(conn, job_id, 'saving_catalog')
                with conn:
                    conn.execute('UPDATE video_duplicate_dismissals SET active=0 WHERE operation_id=%s', (payload['operation_id'],))
                result = {'operation_id': payload['operation_id'], 'local_state': 'dismissal undone'}
            elif action == 'hide':
                _core._record_job_progress(conn, job_id, 'validating')
                keeper = str(payload['keeper_id'])
                losers = [str(x) for x in payload['loser_ids']]
                if not losers or keeper in losers or len(set(losers)) != len(losers):
                    raise ValueError('Invalid item list.')
                fingerprints = payload.get('fingerprints') or {}
                expected_revisions = payload.get('expected_revisions') or {}
                validation_ids = list(dict.fromkeys([
                    keeper, *losers,
                    *(str(video_id) for video_id in fingerprints),
                    *(str(video_id) for video_id in expected_revisions),
                ]))
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
                if keeper not in video_rows:
                    raise ValueError('Keeper item no longer exists.')
                if keeper in active_decisions:
                    raise ValueError('Keeper item is already hidden; choose an active item.')
                if fingerprints:
                    for video_id, expected in fingerprints.items():
                        current_row = video_rows.get(str(video_id))
                        if current_row is None:
                            raise ValueError('Data changed since preview; refresh scan.')
                        current = tuple(current_row.get(field) for field in ('provider', 'slug', 'duration', 'performers'))
                        actual = _core.hashlib.sha256(_core.json.dumps(current, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
                        if actual != expected:
                            raise ValueError('Metadata changed since preview; refresh scan.')
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
                if expected_revisions:
                    for video_id, expected in expected_revisions.items():
                        current_revision, current = revision_snapshots.get(str(video_id), (None, None))
                        if current_revision != expected or current is None or current.get('active_decision') or current.get('source_disabled') or (str(video_id) != keeper and current.get('shared_paths')):
                            raise ValueError('Library data changed after the preview; create a new operation plan.')
                quarantine_root.mkdir(parents=True, exist_ok=True)
                op_dir = quarantine_root / job_id
                op_dir.mkdir(parents=True, exist_ok=True)
                services.write_catalog_backup(conn, op_dir, list(losers))
                persisted = conn.execute('SELECT result FROM video_duplicate_jobs WHERE id=%s', (job_id,)).fetchone()
                recovery_snapshots = None
                if persisted and persisted[0]:
                    try:
                        saved = _core.json.loads(persisted[0])
                        if isinstance(saved.get('snapshots'), list):
                            recovery_snapshots = saved['snapshots']
                    except (TypeError, ValueError):
                        pass
                if recovery_snapshots is not None:
                    snapshot_ids = {s.get('video', {}).get('id') for s in recovery_snapshots}
                    if snapshot_ids != set(losers):
                        raise ValueError('Incomplete operation journal; manual repair required.')
                snapshots = recovery_snapshots if recovery_snapshots is not None else []
                if recovery_snapshots is None and losers:
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
                        file_snapshots = []
                        for sf in files:
                            relative = sf.get('file_path') or ''
                            if relative in shared_paths:
                                raise ValueError('File is shared by another entry; aborting hide.')
                            stream = services.resolve_media_path(relative, media_dir)
                            if stream.is_symlink() or not stream.resolve().is_relative_to(media_dir.resolve()):
                                raise ValueError('Unsafe library path detected.')
                            rel = stream.relative_to(media_dir.resolve())
                            for suffix in ('.strm', '.nfo', '.jpg', '.thumb_fail'):
                                src = stream.with_suffix(suffix)
                                dst = op_dir / 'files' / rel.parent / (rel.stem + suffix)
                                if src.is_symlink():
                                    raise ValueError('Symlink detected in library files.')
                                if src.exists():
                                    file_snapshots.append({'source': str(src), 'quarantine': str(dst)})
                        snapshots.append({
                            'video': video,
                            'synced_files': files,
                            'categories': cats,
                            'files': file_snapshots,
                        })
                _core._store_job_result_field(conn, job_id, 'operation_id', job_id)
                _core._store_job_result_field(conn, job_id, 'snapshots', snapshots)
                file_total = sum((len(snapshot['files']) for snapshot in snapshots))
                moved_count = 0
                _core._record_job_progress(conn, job_id, 'moving_files', completed=0, total=file_total)
                for snapshot in snapshots:
                    for move in snapshot['files']:
                        src, dst = (_core.Path(move['source']), _core.Path(move['quarantine']))
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        if src.exists():
                            if dst.exists():
                                if not services.duplicate_file_matches(src, dst):
                                    raise FileExistsError('Quarantine file has different contents.')
                                src.unlink()
                            else:
                                services.duplicate_move_file(src, dst)
                            moved.append((src, dst))
                        elif dst.exists():
                            moved.append((src, dst))
                        elif not dst.exists():
                            if src.suffix == '.strm':
                                raise FileNotFoundError('Missing STRM file for loser item.')
                        moved_count += 1
                        _core._record_job_progress(conn, job_id, 'moving_files', completed=moved_count, total=file_total)
                _core._record_job_progress(conn, job_id, 'saving_catalog')
                with conn:
                    keeper_channel = keeper.split(':')[1] if ':' in keeper and len(keeper.split(':')) >= 2 else ''
                    conn.execute('INSERT INTO video_sources\n                        (video_id, source_id, provider, provider_ref, source_url, channel_id, priority, is_active, created_at)\n                        VALUES (%s, %s, %s, %s, %s, %s, 1, 1, %s)\n                        ON CONFLICT (source_id) DO NOTHING', (keeper, keeper, 'telegram', keeper, f"https://t.me/c/{(keeper_channel[4:] if keeper_channel.startswith('-100') else keeper_channel.lstrip('-'))}" if keeper_channel else '', keeper_channel, now))
                    k_row = conn.execute('SELECT * FROM videos WHERE id=%s', (keeper,)).fetchone()
                    k_cols = _core.db.table_columns(conn, 'videos')
                    k_vid = dict(zip(k_cols, k_row)) if k_row else {}

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
                            return []
                    merged_perfs = set(_parse_list_field(k_vid.get('performers')))
                    merged_cats = set(_parse_list_field(k_vid.get('categories')))
                    merged_tags = set(_parse_list_field(k_vid.get('tags')))
                    k_studio = (k_vid.get('studio') or '').strip()
                    for snapshot in snapshots:
                        loser = snapshot['video']['id']
                        l_vid = snapshot.get('video') or {}
                        l_channel = loser.split(':')[1] if ':' in loser and len(loser.split(':')) >= 2 else ''
                        conn.execute('INSERT INTO video_sources\n                            (video_id, source_id, provider, provider_ref, source_url, channel_id, priority, is_active, created_at)\n                            VALUES (%s, %s, %s, %s, %s, %s, 2, 1, %s)\n                            ON CONFLICT (source_id) DO UPDATE SET\n                                video_id=EXCLUDED.video_id, provider=EXCLUDED.provider,\n                                provider_ref=EXCLUDED.provider_ref, source_url=EXCLUDED.source_url,\n                                channel_id=EXCLUDED.channel_id, priority=EXCLUDED.priority,\n                                is_active=EXCLUDED.is_active, created_at=EXCLUDED.created_at', (keeper, loser, 'telegram', loser, f"https://t.me/c/{(l_channel[4:] if l_channel.startswith('-100') else l_channel.lstrip('-'))}" if l_channel else '', l_channel, now))
                        merged_perfs.update(_parse_list_field(l_vid.get('performers')))
                        merged_cats.update(_parse_list_field(l_vid.get('categories')))
                        merged_tags.update(_parse_list_field(l_vid.get('tags')))
                        l_studio = (l_vid.get('studio') or '').strip()
                        if l_studio and l_studio != k_studio:
                            merged_tags.add(l_studio)
                        if not k_studio and l_studio:
                            k_studio = l_studio
                        conn.execute('DELETE FROM video_categories WHERE video_id=%s', (loser,))
                        conn.execute('DELETE FROM synced_files WHERE video_id=%s', (loser,))
                        conn.execute('DELETE FROM videos WHERE id=%s', (loser,))
                        conn.execute("INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot,rules_version) VALUES (%s,%s,%s,'active',%s,%s,%s)", (loser, keeper, job_id, now, _core.json.dumps(snapshot, ensure_ascii=False), payload.get('rules_version')))
                    if k_row:
                        conn.execute('UPDATE videos SET\n                            performers=%s, categories=%s, tags=%s, studio=%s WHERE id=%s', (_core.json.dumps(sorted(merged_perfs), ensure_ascii=False), _core.json.dumps(sorted(merged_cats), ensure_ascii=False), _core.json.dumps(sorted(merged_tags), ensure_ascii=False), k_studio, keeper))
                        for cat in merged_cats:
                            conn.execute('INSERT INTO video_categories (video_id, category) VALUES (%s, %s) ON CONFLICT DO NOTHING', (keeper, cat))
                        try:
                            k_file = conn.execute('SELECT file_path FROM synced_files WHERE video_id=%s', (keeper,)).fetchone()
                            if k_file:
                                k_strm = services.resolve_media_path(k_file[0], media_dir)
                                k_nfo = k_strm.with_suffix('.nfo')
                                if k_nfo.exists():
                                    k_dict = {'id': keeper, 'provider': k_vid.get('provider', 'telegram'), 'provider_video_id': keeper.split(':', 1)[-1] if ':' in keeper else keeper, 'title': k_vid.get('title', ''), 'slug': k_vid.get('slug', ''), 'duration': k_vid.get('duration', 0), 'thumbnail': k_vid.get('thumbnail', ''), 'performers': sorted(merged_perfs), 'performer_thumbs': services.load_performer_thumbs(conn, sorted(merged_perfs)), 'studio': k_studio, 'dateadded': services.format_dateadded(k_vid.get('first_seen') or now)}
                                    services.create_nfo_file(k_dict, set(merged_cats), merged_tags, k_nfo)
                        except Exception as nfo_exc:
                            services.logger.warning(f'Could not update keeper NFO metadata: {nfo_exc}')
                catalog_committed = True
                refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, 'hidden', services)
                result = {'operation_id': job_id, 'hidden': losers, 'keeper_id': keeper, 'local_state': 'hidden', 'jellyfin_refresh': 'updated' if refreshed else 'failed'}
                _core._finish_job(conn, job_id, result)
                conn.commit()
            elif action == 'quarantine':
                video_id = str(payload.get('video_id') or '')
                if not video_id:
                    raise ValueError('Missing item to quarantine.')
                if not conn.execute('SELECT 1 FROM videos WHERE id=%s', (video_id,)).fetchone():
                    raise ValueError('Item no longer exists.')
                if services.video_is_disabled(video_id):
                    raise ValueError('Source of this item is disabled; decision was not saved.')
                if conn.execute("SELECT 1 FROM video_duplicate_decisions WHERE loser_id=%s AND state='active'", (video_id,)).fetchone():
                    raise ValueError('Item is already quarantined.')
                quarantine_root.mkdir(parents=True, exist_ok=True)
                op_dir = quarantine_root / job_id
                op_dir.mkdir(parents=True, exist_ok=True)
                services.write_catalog_backup(conn, op_dir, [video_id])
                persisted = conn.execute('SELECT result FROM video_duplicate_jobs WHERE id=%s', (job_id,)).fetchone()
                recovery_snapshots = None
                if persisted and persisted[0]:
                    try:
                        saved = _core.json.loads(persisted[0])
                        if isinstance(saved.get('snapshots'), list):
                            recovery_snapshots = saved['snapshots']
                    except (TypeError, ValueError):
                        pass
                if recovery_snapshots is not None:
                    snapshot_ids = {s.get('video', {}).get('id') for s in recovery_snapshots}
                    if snapshot_ids != {video_id}:
                        raise ValueError('Incomplete operation journal; manual repair required.')
                snapshots = recovery_snapshots if recovery_snapshots is not None else []
                for loser in [] if recovery_snapshots is not None else [video_id]:
                    video_row = conn.execute('SELECT * FROM videos WHERE id=%s', (loser,)).fetchone()
                    video_cols = _core.db.table_columns(conn, 'videos')
                    files = conn.execute('SELECT * FROM synced_files WHERE video_id=%s', (loser,)).fetchall()
                    sf_cols = _core.db.table_columns(conn, 'synced_files')
                    cats = conn.execute('SELECT * FROM video_categories WHERE video_id=%s', (loser,)).fetchall()
                    vc_cols = _core.db.table_columns(conn, 'video_categories')
                    file_snapshots = []
                    for file_row in files:
                        sf = dict(zip(sf_cols, file_row))
                        relative = sf.get('file_path') or ''
                        other = conn.execute('SELECT 1 FROM synced_files WHERE file_path=%s AND video_id<>%s', (relative, loser)).fetchone()
                        if other:
                            raise ValueError('File is shared by another entry; aborting quarantine.')
                        stream = services.resolve_media_path(relative, media_dir)
                        if stream.is_symlink() or not stream.resolve().is_relative_to(media_dir.resolve()):
                            raise ValueError('Unsafe library path detected.')
                        rel = stream.relative_to(media_dir.resolve())
                        for suffix in ('.strm', '.nfo', '.jpg', '.thumb_fail'):
                            src = stream.with_suffix(suffix)
                            dst = op_dir / 'files' / rel.parent / (rel.stem + suffix)
                            if src.is_symlink():
                                raise ValueError('Symlink detected in library files.')
                            if src.exists():
                                file_snapshots.append({'source': str(src), 'quarantine': str(dst)})
                    snapshots.append({'video': dict(zip(video_cols, video_row)), 'synced_files': [dict(zip(sf_cols, r)) for r in files], 'categories': [dict(zip(vc_cols, r)) for r in cats], 'files': file_snapshots})
                    expected_revision = payload.get('expected_revision')
                    if expected_revision:
                        from common.admin_operations import _video_snapshot
                        current_revision, current = _video_snapshot(conn, loser)
                        if current_revision != expected_revision or current is None or current.get('active_decision') or current.get('source_disabled') or current.get('shared_paths'):
                            raise ValueError('Library data changed after the preview; create a new operation plan.')
                _core._store_job_result_field(conn, job_id, 'operation_id', job_id)
                _core._store_job_result_field(conn, job_id, 'snapshots', snapshots)
                file_total = sum((len(snapshot['files']) for snapshot in snapshots))
                moved_count = 0
                _core._record_job_progress(conn, job_id, 'moving_files', completed=0, total=file_total)
                for snapshot in snapshots:
                    for move in snapshot['files']:
                        src, dst = (_core.Path(move['source']), _core.Path(move['quarantine']))
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        if src.exists():
                            if dst.exists():
                                if not services.duplicate_file_matches(src, dst):
                                    raise FileExistsError('Quarantine file has different contents.')
                                src.unlink()
                            else:
                                services.duplicate_move_file(src, dst)
                            moved.append((src, dst))
                        elif dst.exists():
                            moved.append((src, dst))
                        elif not dst.exists():
                            if src.suffix == '.strm':
                                raise FileNotFoundError('Missing STRM file for quarantined item.')
                        moved_count += 1
                        _core._record_job_progress(conn, job_id, 'moving_files', completed=moved_count, total=file_total)
                _core._record_job_progress(conn, job_id, 'saving_catalog')
                with conn:
                    for snapshot in snapshots:
                        loser = snapshot['video']['id']
                        conn.execute('DELETE FROM video_categories WHERE video_id=%s', (loser,))
                        conn.execute('DELETE FROM synced_files WHERE video_id=%s', (loser,))
                        conn.execute('DELETE FROM videos WHERE id=%s', (loser,))
                        conn.execute("INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot) VALUES (%s,%s,%s,'active',%s,%s)", (loser, loser, job_id, now, _core.json.dumps(snapshot, ensure_ascii=False)))
                catalog_committed = True
                refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, 'quarantined', services)
                result = {'operation_id': job_id, 'quarantined': [video_id], 'local_state': 'quarantined', 'jellyfin_refresh': 'updated' if refreshed else 'failed'}
                _core._finish_job(conn, job_id, result)
                conn.commit()
            elif action == 'undo':
                operation = str(payload['operation_id'])
                rows = conn.execute("SELECT loser_id,snapshot FROM video_duplicate_decisions WHERE operation_id=%s AND state='active'", (operation,)).fetchall()
                if not rows:
                    raise ValueError('No active decision found to undo.')
                snapshots = [_core.json.loads(r[1]) for r in rows]
                job_stage_row = conn.execute('SELECT result FROM video_duplicate_jobs WHERE id=%s', (job_id,)).fetchone()
                try:
                    resuming_undo = bool(job_stage_row and job_stage_row[0] and ('undo_snapshots' in _core.json.loads(job_stage_row[0])))
                except (TypeError, ValueError):
                    resuming_undo = False
                for snapshot in snapshots:
                    video_id = snapshot['video']['id']
                    if conn.execute('SELECT 1 FROM videos WHERE id=%s', (video_id,)).fetchone():
                        raise ValueError('Item was added again; undo requires manual review.')
                    media_root = media_dir.resolve()
                    quarantine_base = quarantine_root.resolve()
                    for move in snapshot.get('files', []):
                        try:
                            source = _core.Path(move['source'])
                            quarantined = _core.Path(move['quarantine'])
                            if not source.is_absolute() or not quarantined.is_absolute():
                                raise ValueError
                            source.resolve(strict=False).relative_to(media_root)
                            quarantined.resolve(strict=False).relative_to(quarantine_base)
                        except (KeyError, OSError, ValueError, TypeError):
                            raise ValueError('Unsafe restore path detected; manual review is required.') from None
                    for saved_file in snapshot['synced_files']:
                        collision = conn.execute('SELECT video_id FROM synced_files WHERE file_path=%s', (saved_file['file_path'],)).fetchone()
                        if collision:
                            raise ValueError('File path is currently used by another entry.')
                    if not resuming_undo and any((_core.Path(move['source']).exists() for move in snapshot.get('files', []))):
                        raise ValueError('Target path exists; not overwritten.')
                _core._store_job_result_field(conn, job_id, 'operation_id', operation)
                _core._store_job_result_field(conn, job_id, 'undo_snapshots', snapshots)
                file_total = sum((len(snapshot.get('files', [])) for snapshot in snapshots))
                restored_count = 0
                _core._record_job_progress(conn, job_id, 'restoring_files', completed=0, total=file_total)
                for snapshot in snapshots:
                    for move in snapshot.get('files', []):
                        src, dst = (_core.Path(move['quarantine']), _core.Path(move['source']))
                        if src.exists():
                            if dst.exists():
                                if not services.duplicate_file_matches(src, dst):
                                    raise FileExistsError('Target path has different contents; not overwritten.')
                                if not resuming_undo:
                                    raise FileExistsError('Target path already exists.')
                                src.unlink()
                            else:
                                services.duplicate_move_file(src, dst)
                            moved.append((src, dst))
                        elif _core.Path(move['source']).suffix == '.strm' and (not dst.exists()):
                            raise FileNotFoundError('Missing STRM file in quarantine.')
                        elif dst.exists():
                            moved.append((src, dst))
                        restored_count += 1
                        _core._record_job_progress(conn, job_id, 'restoring_files', completed=restored_count, total=file_total)
                _core._record_job_progress(conn, job_id, 'saving_catalog')
                with conn:
                    for snapshot in snapshots:
                        video = snapshot['video']
                        names = list(video)
                        updates = [name for name in names if name != 'id']
                        assignments = ', '.join((f'{name}=EXCLUDED.{name}' for name in updates))
                        conn.execute(f"INSERT INTO videos ({','.join(names)}) VALUES ({','.join(('%s' for _ in names))}) ON CONFLICT (id) DO UPDATE SET {assignments}", [video[n] for n in names])
                        for table, records in (('synced_files', snapshot['synced_files']), ('video_categories', snapshot['categories'])):
                            for record in records:
                                names = list(record)
                                if table == 'synced_files':
                                    conflict_sql = 'ON CONFLICT (file_path) DO UPDATE SET ' + ', '.join((f'{name}=EXCLUDED.{name}' for name in names if name != 'file_path'))
                                else:
                                    conflict_sql = 'ON CONFLICT (video_id, category) DO NOTHING'
                                conn.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join(('%s' for _ in names))}) {conflict_sql}", [record[n] for n in names])
                        conn.execute("UPDATE video_duplicate_decisions SET state='undone' WHERE loser_id=%s", (video['id'],))
                        conn.execute('DELETE FROM video_sources WHERE source_id=%s', (video['id'],))
                catalog_committed = True
                refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, 'restored', services)
                result = {'operation_id': operation, 'restored': [s['video']['id'] for s in snapshots], 'local_state': 'restored', 'jellyfin_refresh': 'updated' if refreshed else 'failed'}
            else:
                raise ValueError('Unsupported job type.')
            _core._finish_job(conn, job_id, result)
            completed += 1
        except Exception as exc:
            if not catalog_committed and action != 'orphan_file_operation':
                for origin, target in reversed(moved):
                    try:
                        if target.exists() and (not origin.exists()):
                            origin.parent.mkdir(parents=True, exist_ok=True)
                            services.duplicate_move_file(target, origin)
                    except Exception:
                        services.logger.exception('Could not compensate duplicate operation file move')
            previous = conn.execute('SELECT result FROM video_duplicate_jobs WHERE id=%s', (job_id,)).fetchone()
            try:
                failure_result = _core.json.loads(previous[0]) if previous and previous[0] else {}
                if not isinstance(failure_result, dict):
                    failure_result = {}
            except (TypeError, ValueError):
                failure_result = {}
            failure_result['error'] = str(exc)[:300]
            with conn:
                conn.execute("UPDATE video_duplicate_jobs SET state='failed', result=%s, updated_at=%s WHERE id=%s AND state='processing'", (_core.json.dumps(failure_result, ensure_ascii=False), int(_core.time.time()), job_id))
            services.logger.warning('Duplicate operation %s failed: %s', job_id, exc)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
    return completed
