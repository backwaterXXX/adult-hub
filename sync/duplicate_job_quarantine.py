"""Quarantine steps for durable duplicate decision jobs.

Moved verbatim from duplicate_job_worker.py. Names are read through the
duplicate_jobs compatibility facade (``_core``), as they were before the move.
"""
from __future__ import annotations

if __package__:
    from . import duplicate_jobs as _core
    from .duplicate_job_files import _append_sidecar_moves, _check_journal_ids, _load_recovery_snapshots, _move_snapshot_files
else:
    import duplicate_jobs as _core
    from duplicate_job_files import _append_sidecar_moves, _check_journal_ids, _load_recovery_snapshots, _move_snapshot_files


def _check_quarantine_target(conn, video_id: str, services) -> None:
    if not video_id:
        raise ValueError('Missing item to quarantine.')
    if not conn.execute('SELECT 1 FROM videos WHERE id=%s', (video_id,)).fetchone():
        raise ValueError('Item no longer exists.')
    if services.video_is_disabled(video_id):
        raise ValueError('Source of this item is disabled; decision was not saved.')
    if conn.execute("SELECT 1 FROM video_duplicate_decisions WHERE loser_id=%s AND state='active'", (video_id,)).fetchone():
        raise ValueError('Item is already quarantined.')


def _build_quarantine_snapshot(conn, loser: str, media_dir, services, op_dir, payload: dict) -> dict:
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
        _append_sidecar_moves(services, relative, media_dir, op_dir, file_snapshots)
    snapshot = {'video': dict(zip(video_cols, video_row)), 'synced_files': [dict(zip(sf_cols, r)) for r in files], 'categories': [dict(zip(vc_cols, r)) for r in cats], 'files': file_snapshots}
    expected_revision = payload.get('expected_revision')
    if expected_revision:
        from common.admin_operations import _video_snapshot
        current_revision, current = _video_snapshot(conn, loser)
        if current_revision != expected_revision or current is None or current.get('active_decision') or current.get('source_disabled') or current.get('shared_paths'):
            raise ValueError('Library data changed after the preview; create a new operation plan.')
    return snapshot


def _retire_quarantined_items(conn, job_id, snapshots: list, now) -> None:
    _core._record_job_progress(conn, job_id, 'saving_catalog')
    with conn:
        for snapshot in snapshots:
            loser = snapshot['video']['id']
            conn.execute('DELETE FROM video_categories WHERE video_id=%s', (loser,))
            conn.execute('DELETE FROM synced_files WHERE video_id=%s', (loser,))
            conn.execute('DELETE FROM videos WHERE id=%s', (loser,))
            conn.execute("INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot) VALUES (%s,%s,%s,'active',%s,%s)", (loser, loser, job_id, now, _core.json.dumps(snapshot, ensure_ascii=False)))


def _run_quarantine(conn, job_id, payload, media_dir, services, quarantine_root, now, run) -> dict:
    """Quarantine one library item and store its undo snapshot."""
    video_id = str(payload.get('video_id') or '')
    _check_quarantine_target(conn, video_id, services)
    quarantine_root.mkdir(parents=True, exist_ok=True)
    op_dir = quarantine_root / job_id
    op_dir.mkdir(parents=True, exist_ok=True)
    services.write_catalog_backup(conn, op_dir, [video_id])
    recovery_snapshots = _load_recovery_snapshots(conn, job_id)
    if recovery_snapshots is not None:
        _check_journal_ids(recovery_snapshots, {video_id})
    snapshots = recovery_snapshots if recovery_snapshots is not None else []
    if recovery_snapshots is None:
        snapshots.append(_build_quarantine_snapshot(conn, video_id, media_dir, services, op_dir, payload))
    _core._store_job_result_field(conn, job_id, 'operation_id', job_id)
    _core._store_job_result_field(conn, job_id, 'snapshots', snapshots)
    _move_snapshot_files(conn, job_id, snapshots, services, run, 'Missing STRM file for quarantined item.')
    _retire_quarantined_items(conn, job_id, snapshots, now)
    run.catalog_committed = True
    refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, 'quarantined', services)
    result = {'operation_id': job_id, 'quarantined': [video_id], 'local_state': 'quarantined', 'jellyfin_refresh': 'updated' if refreshed else 'failed'}
    _core._finish_job(conn, job_id, result)
    conn.commit()
    return result
