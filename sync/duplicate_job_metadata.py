"""Metadata-related durable duplicate job handlers."""
from __future__ import annotations

if __package__:
    from . import duplicate_jobs as _core
else:
    import duplicate_jobs as _core

def _metadata_update(conn, job_id: str, payload: dict, media_dir: _core.Path, services) -> dict:
    """Apply revisioned title/plot overrides, regenerate safe NFOs, then refresh Jellyfin."""
    from common.video_metadata import resolve_metadata, validate_override_patch
    video_id = str(payload.get('video_id') or '')
    expected_revision = payload.get('expected_revision')
    if not video_id or type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('Invalid metadata update request.')
    changes, resets = validate_override_patch(payload.get('changes'), payload.get('reset_fields', []))
    if not changes and (not resets):
        raise ValueError('Choose at least one metadata change.')
    video_columns = _core.db.table_columns(conn, 'videos')
    video_row = conn.execute('SELECT * FROM videos WHERE id=%s FOR UPDATE', (video_id,)).fetchone()
    if video_row is None:
        raise ValueError('This catalog item no longer exists.')
    source = dict(zip(video_columns, video_row))
    expected_source_fingerprint = payload.get('expected_source_fingerprint')
    override_row = conn.execute('SELECT fields,revision FROM video_metadata_overrides WHERE video_id=%s FOR UPDATE', (video_id,)).fetchone()
    try:
        raw_overrides = override_row[0] if override_row else '{}'
        current = raw_overrides if isinstance(raw_overrides, dict) else _core.json.loads(raw_overrides or '{}')
    except (TypeError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    revision = int(override_row[1]) if override_row else 0
    target = dict(current)
    target.update(changes)
    for field in resets:
        target.pop(field, None)
    if expected_source_fingerprint and revision == expected_revision:
        actual_source_fingerprint = _core.hashlib.sha256(_core.json.dumps({field: source.get(field) for field in ('title', 'plot', 'studio', 'performers', 'tags', 'date_released')}, sort_keys=True, ensure_ascii=False, default=str, separators=(',', ':')).encode('utf-8')).hexdigest()
        if actual_source_fingerprint != expected_source_fingerprint:
            raise ValueError('Metadata source values changed after the preview. Reload and review the latest values.')
    result_row = conn.execute('SELECT result FROM video_duplicate_jobs WHERE id=%s', (job_id,)).fetchone()
    try:
        job_result = payload.get('_resume') if isinstance(payload.get('_resume'), dict) else {}
        current_result = _core.json.loads(result_row[0]) if result_row and result_row[0] else {}
        if isinstance(current_result, dict):
            job_result = {**job_result, **current_result}
    except (TypeError, ValueError):
        job_result = {}
    if not isinstance(job_result, dict):
        job_result = {}
    if revision == expected_revision:
        next_revision = revision + 1
        from common.admin_audit_events import record_metadata_change
        before_metadata = resolve_metadata(source, current)
        after_metadata = resolve_metadata(source, target)
        with conn:
            conn.execute('INSERT INTO video_metadata_overrides (video_id,fields,revision,updated_at) VALUES (%s,%s,%s,CURRENT_TIMESTAMP) ON CONFLICT(video_id) DO UPDATE SET fields=EXCLUDED.fields,revision=EXCLUDED.revision,updated_at=EXCLUDED.updated_at', (video_id, _core.Jsonb(target), next_revision))
            record_metadata_change(conn, job_id=job_id, video_id=video_id, before=before_metadata, after=after_metadata, before_overrides=current, after_overrides=target, fields=(*changes.keys(), *resets))
        revision = next_revision
    elif revision == expected_revision + 1 and current == target:
        pass
    else:
        raise ValueError('Metadata changed after the preview. Reload and review the latest values.')
    effective = resolve_metadata(source, target)
    source_fingerprint = _core.hashlib.sha256(_core.json.dumps({field: source.get(field) for field in ('title', 'plot')}, sort_keys=True, ensure_ascii=False, default=str, separators=(',', ':')).encode('utf-8')).hexdigest()
    needs_nfo = not (job_result.get('metadata_revision') == revision and job_result.get('nfo_source_fingerprint') == source_fingerprint and (job_result.get('nfo_written') is True))
    if needs_nfo:
        _core._record_job_progress(conn, job_id, 'applying_metadata')
        files = conn.execute('SELECT file_path FROM synced_files WHERE video_id=%s ORDER BY file_path', (video_id,)).fetchall()
        streams = []
        media_root = media_dir.resolve()
        for file_row in files:
            stream = services.resolve_media_path(str(file_row[0]), media_dir)
            if stream.is_symlink() or not stream.resolve().is_relative_to(media_root):
                raise ValueError('Unsafe library path detected.')
            if stream.suffix == '.strm':
                streams.append(stream)
        if not streams:
            raise ValueError('No synchronized library file is available for the metadata update.')
        categories = {str(row[0]) for row in conn.execute('SELECT category FROM video_categories WHERE video_id=%s ORDER BY category', (video_id,)).fetchall() if row[0]}
        tags = set(effective.get('tags') or [])
        nfo_video = {**source, **effective, 'dateadded': services.format_dateadded(source.get('first_seen'))}
        for stream in streams:
            nfo_path = stream.with_suffix('.nfo')
            if nfo_path.is_symlink() or not nfo_path.resolve().is_relative_to(media_root):
                raise ValueError('Unsafe NFO path detected.')
            services.create_nfo_file(nfo_video, categories, tags, nfo_path)
        nfo_content_hashes = {stream.with_suffix('.nfo').name: _core.hashlib.sha256(stream.with_suffix('.nfo').read_bytes()).hexdigest() for stream in streams}
        _core._store_job_result_field(conn, job_id, 'metadata_revision', revision)
        _core._store_job_result_field(conn, job_id, 'nfo_source_fingerprint', source_fingerprint)
        _core._store_job_result_field(conn, job_id, 'nfo_content_hashes', nfo_content_hashes)
        _core._store_job_result_field(conn, job_id, 'nfo_written', True)
        _core._store_job_result_field(conn, job_id, 'local_state', 'metadata_updated')
    refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, 'metadata_updated', services)
    if not refreshed:
        _core._store_job_result_field(conn, job_id, 'jellyfin_refresh', 'failed')
        _core._store_job_result_field(conn, job_id, 'jellyfin_import', 'not_requested')
        raise RuntimeError('Local metadata was saved, but Jellyfin refresh failed. Retry this task to resume the refresh.')
    _core._store_job_result_field(conn, job_id, 'jellyfin_import', 'not_confirmed')
    return {'video_id': video_id, 'metadata_revision': revision, 'local_state': 'metadata_updated', 'jellyfin_refresh': 'updated', 'jellyfin_import': 'not_confirmed'}

def _availability_check(conn, job_id: str, payload: dict, services) -> dict:
    """Probe at most 20 playback chains, recording only bounded evidence."""
    video_ids = payload.get('video_ids')
    if not isinstance(video_ids, list) or not 1 <= len(video_ids) <= 20 or any((not isinstance(value, str) or not value or len(value) > 1000 for value in video_ids)) or (len(set(video_ids)) != len(video_ids)):
        raise ValueError('Choose between 1 and 20 distinct catalog items.')
    completed = {'available': 0, 'unavailable': 0, 'transient_error': 0, 'unknown': 0, 'skipped': 0}
    for index, video_id in enumerate(video_ids, start=1):
        _core._record_job_progress(conn, job_id, 'checking_availability', completed=index - 1, total=len(video_ids))
        exists = conn.execute('SELECT 1 FROM videos WHERE id=%s', (video_id,)).fetchone()
        if not exists:
            completed['skipped'] += 1
            _core._record_job_progress(conn, job_id, 'checking_availability', completed=index, total=len(video_ids))
            continue
        try:
            if services.probe_stream_detailed is not None:
                result = services.probe_stream_detailed(video_id)
                if result not in {'available', 'unavailable', 'transient_error', 'unknown'}:
                    result = 'unknown'
                status = result
            else:
                result = services.probe_stream(video_id)
        except Exception as exc:
            result = None
            error_name = type(exc).__name__.lower()
            status = 'transient_error' if any((marker in error_name for marker in ('timeout', 'connect', 'network'))) else 'unknown'
        else:
            if services.probe_stream_detailed is None:
                status = 'available' if result is True else 'unavailable' if result is False else 'unknown'
        evidence = {'available': 'A media byte range was served by the resolver playback chain.', 'unavailable': 'The resolver confirmed a missing or non-playable upstream item.', 'transient_error': 'The playback probe failed because of a transient network error.', 'unknown': 'The playback probe was inconclusive; no availability change was made.'}[status]
        now = int(_core.time.time())
        with conn:
            conn.execute('INSERT INTO video_availability_checks (job_id,video_id,status,checked_at,evidence) VALUES (%s,%s,%s,to_timestamp(%s),%s) ON CONFLICT(job_id,video_id) DO UPDATE SET status=EXCLUDED.status,checked_at=EXCLUDED.checked_at,evidence=EXCLUDED.evidence', (job_id, video_id, status, now, evidence))
            if status == 'available':
                conn.execute('UPDATE videos SET playable=1 WHERE id=%s', (video_id,))
                conn.execute('DELETE FROM dead_streams WHERE video_id=%s', (video_id,))
            elif status == 'unavailable':
                conn.execute('UPDATE videos SET playable=0 WHERE id=%s', (video_id,))
                conn.execute('INSERT INTO dead_streams (video_id,reason,detected_at) VALUES (%s,%s,%s) ON CONFLICT(video_id) DO UPDATE SET reason=EXCLUDED.reason,detected_at=EXCLUDED.detected_at', (video_id, 'availability check confirmed unavailable', now))
        completed[status] += 1
        _core._record_job_progress(conn, job_id, 'checking_availability', completed=index, total=len(video_ids))
    return {'checked': sum(completed.values()) - completed['skipped'], 'counts': completed, 'local_state': 'availability_checked'}
