"""Undo and restore steps for durable duplicate decision jobs.

Moved verbatim from duplicate_job_worker.py. Names are read through the
duplicate_jobs compatibility facade (``_core``), as they were before the move.
"""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

if __package__:
    from . import duplicate_jobs as _core
else:
    import duplicate_jobs as _core


def _restore_list(value):
    if isinstance(value, list):
        return value
    try:
        parsed = _core.json.loads(value or '[]')
        return parsed if isinstance(parsed, list) else []
    except (TypeError, ValueError):
        return []


def _check_undo_move_paths(snapshot: dict, media_root, quarantine_base) -> None:
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


def _check_undo_collisions(conn, snapshot: dict) -> None:
    for saved_file in snapshot['synced_files']:
        collision = conn.execute('SELECT video_id FROM synced_files WHERE file_path=%s', (saved_file['file_path'],)).fetchone()
        if collision:
            raise ValueError('File path is currently used by another entry.')


def _check_undo_snapshots(conn, snapshots: list, media_dir, quarantine_root, resuming_undo: bool) -> None:
    for snapshot in snapshots:
        video_id = snapshot['video']['id']
        if conn.execute('SELECT 1 FROM videos WHERE id=%s', (video_id,)).fetchone():
            raise ValueError('Item was added again; undo requires manual review.')
        media_root = media_dir.resolve()
        quarantine_base = quarantine_root.resolve()
        _check_undo_move_paths(snapshot, media_root, quarantine_base)
        _check_undo_collisions(conn, snapshot)
        if not resuming_undo and any((_core.Path(move['source']).exists() for move in snapshot.get('files', []))):
            raise ValueError('Target path exists; not overwritten.')


def _restore_undo_files(conn, job_id, snapshots: list, services, run, resuming_undo: bool) -> None:
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
                run.moved.append((src, dst))
            elif _core.Path(move['source']).suffix == '.strm' and (not dst.exists()):
                raise FileNotFoundError('Missing STRM file in quarantine.')
            elif dst.exists():
                run.moved.append((src, dst))
            restored_count += 1
            _core._record_job_progress(conn, job_id, 'restoring_files', completed=restored_count, total=file_total)


def _restore_snapshot_rows(conn, snapshot: dict) -> None:
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
    override_snapshot = snapshot.get('metadata_overrides')
    if isinstance(override_snapshot, dict):
        conn.execute(
            'INSERT INTO video_metadata_overrides (video_id,fields,revision,updated_at) '
            'VALUES (%s,%s,%s,coalesce(%s::timestamptz,CURRENT_TIMESTAMP)) '
            'ON CONFLICT (video_id) DO UPDATE SET fields=EXCLUDED.fields,'
            'revision=EXCLUDED.revision,updated_at=EXCLUDED.updated_at',
            (video['id'], _core.json.dumps(override_snapshot.get('fields') or {}, ensure_ascii=False),
             override_snapshot.get('revision', 1), override_snapshot.get('updated_at')),
        )
    conn.execute("UPDATE video_duplicate_decisions SET state='undone' WHERE loser_id=%s", (video['id'],))
    conn.execute('DELETE FROM video_sources WHERE source_id=%s', (video['id'],))


def _restore_keeper_video_fields(conn, keeper_id: str, current: dict, old_video: dict, new_video: dict) -> None:
    for field in ('title', 'plot', 'studio', 'date_released', 'thumbnail', 'performers', 'categories', 'tags',
                  'scene_match_source', 'scene_match_external_id', 'scene_match_url',
                  'scene_match_confidence'):
        if current.get(field) == new_video.get(field):
            conn.execute(f'UPDATE videos SET {field}=%s WHERE id=%s', (old_video.get(field), keeper_id))
    current_categories = set(_core.json.loads(current.get('categories') or '[]')
                             if isinstance(current.get('categories'), str) else current.get('categories') or [])
    after_categories = set(_core.json.loads(new_video.get('categories') or '[]')
                           if isinstance(new_video.get('categories'), str) else new_video.get('categories') or [])
    before_categories = _core.json.loads(old_video.get('categories') or '[]') if isinstance(old_video.get('categories'), str) else old_video.get('categories') or []
    if current_categories == after_categories:
        conn.execute('DELETE FROM video_categories WHERE video_id=%s', (keeper_id,))
        for category in before_categories:
            conn.execute('INSERT INTO video_categories (video_id,category) VALUES (%s,%s) ON CONFLICT DO NOTHING', (keeper_id, category))


def _restore_keeper_overrides(conn, keeper_id: str, before: dict, after: dict) -> None:
    current_override = conn.execute('SELECT fields FROM video_metadata_overrides WHERE video_id=%s', (keeper_id,)).fetchone()
    current_fields = current_override[0] if current_override else {}
    if isinstance(current_fields, str):
        current_fields = _core.json.loads(current_fields)
    if current_fields == after.get('overrides', {}):
        old_fields = before.get('overrides', {})
        if old_fields:
            conn.execute('INSERT INTO video_metadata_overrides (video_id,fields,revision,updated_at) VALUES (%s,%s,1,CURRENT_TIMESTAMP) ON CONFLICT (video_id) DO UPDATE SET fields=EXCLUDED.fields,revision=video_metadata_overrides.revision+1,updated_at=CURRENT_TIMESTAMP', (keeper_id, _core.json.dumps(old_fields, ensure_ascii=False)))
        else:
            conn.execute('DELETE FROM video_metadata_overrides WHERE video_id=%s', (keeper_id,))


def _restore_keeper_nfo(conn, keeper_id: str, columns, keeper_file, media_dir, services, now) -> None:
    stream = services.resolve_media_path(keeper_file[0], media_dir)
    if stream.is_symlink() or not stream.resolve().is_relative_to(media_dir.resolve()):
        raise ValueError('Unsafe keeper path detected while restoring metadata.')
    restored_row = conn.execute('SELECT * FROM videos WHERE id=%s', (keeper_id,)).fetchone()
    restored_video = dict(zip(columns, restored_row)) if restored_row else {}
    restored_categories = _restore_list(restored_video.get('categories'))
    restored_tags = _restore_list(restored_video.get('tags'))
    restored_performers = _restore_list(restored_video.get('performers'))
    services.create_nfo_file(
        {'id': keeper_id, 'provider': restored_video.get('provider', 'telegram'),
         'provider_video_id': keeper_id.split(':', 1)[-1],
         'title': restored_video.get('title') or '',
         'slug': restored_video.get('slug') or '',
         'duration': restored_video.get('duration') or 0,
         'thumbnail': restored_video.get('thumbnail') or '',
         'performers': restored_performers,
         'performer_thumbs': services.load_performer_thumbs(conn, restored_performers),
         'studio': restored_video.get('studio') or '',
         'plot': restored_video.get('plot') or '',
         'date_released': restored_video.get('date_released') or '',
         'dateadded': services.format_dateadded(restored_video.get('first_seen') or now)},
        set(restored_categories), set(restored_tags), stream.with_suffix('.nfo'), conn=conn,
    )


def _restore_keeper_metadata(conn, snapshots: list, media_dir, services, now) -> None:
    keeper_states = [snapshot for snapshot in snapshots if snapshot.get('keeper_before') and snapshot.get('keeper_after')]
    if keeper_states:
        before = keeper_states[0]['keeper_before']
        after = keeper_states[0]['keeper_after']
        keeper_id = str(before.get('video', {}).get('id') or '')
        current_row = conn.execute('SELECT * FROM videos WHERE id=%s', (keeper_id,)).fetchone()
        if current_row:
            columns = _core.db.table_columns(conn, 'videos')
            current = dict(zip(columns, current_row))
            old_video, new_video = before.get('video', {}), after.get('video', {})
            _restore_keeper_video_fields(conn, keeper_id, current, old_video, new_video)
            _restore_keeper_overrides(conn, keeper_id, before, after)
            keeper_file = conn.execute(
                'SELECT file_path FROM synced_files WHERE video_id=%s ORDER BY file_path LIMIT 1',
                (keeper_id,),
            ).fetchone()
            if keeper_file:
                _restore_keeper_nfo(conn, keeper_id, columns, keeper_file, media_dir, services, now)


def _run_undo(conn, job_id, payload, media_dir, services, quarantine_root, now, run) -> dict:
    """Restore the items of a previous duplicate operation from their undo snapshots."""
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
    _check_undo_snapshots(conn, snapshots, media_dir, quarantine_root, resuming_undo)
    _core._store_job_result_field(conn, job_id, 'operation_id', operation)
    _core._store_job_result_field(conn, job_id, 'undo_snapshots', snapshots)
    _restore_undo_files(conn, job_id, snapshots, services, run, resuming_undo)
    _core._record_job_progress(conn, job_id, 'saving_catalog')
    with conn:
        for snapshot in snapshots:
            _restore_snapshot_rows(conn, snapshot)
        _restore_keeper_metadata(conn, snapshots, media_dir, services, now)
    run.catalog_committed = True
    refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, 'restored', services)
    result = {'operation_id': operation, 'restored': [s['video']['id'] for s in snapshots], 'local_state': 'restored', 'jellyfin_refresh': 'updated' if refreshed else 'failed'}
    return result
