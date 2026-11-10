"""File move and recovery journal helpers for duplicate jobs.

Moved verbatim from duplicate_job_worker.py. Names are read through the
duplicate_jobs compatibility facade (``_core``), as they were before the move.
"""
from __future__ import annotations

from sync import duplicate_jobs as _core


def _append_sidecar_moves(services, relative: str, media_dir, op_dir, file_snapshots: list) -> None:
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


def _check_journal_ids(recovery_snapshots: list, expected_ids: set) -> None:
    snapshot_ids = {s.get('video', {}).get('id') for s in recovery_snapshots}
    if snapshot_ids != expected_ids:
        raise ValueError('Incomplete operation journal; manual repair required.')


def _load_recovery_snapshots(conn, job_id):
    persisted = conn.execute('SELECT result FROM video_duplicate_jobs WHERE id=%s', (job_id,)).fetchone()
    recovery_snapshots = None
    if persisted and persisted[0]:
        try:
            saved = _core.json.loads(persisted[0])
            if isinstance(saved.get('snapshots'), list):
                recovery_snapshots = saved['snapshots']
        except (TypeError, ValueError):
            pass
    return recovery_snapshots


def _move_snapshot_files(conn, job_id, snapshots, services, run, missing_strm_message: str) -> None:
    """Move each snapshot's library files into quarantine, recording moves for compensation."""
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
                run.moved.append((src, dst))
            elif dst.exists():
                run.moved.append((src, dst))
            elif not dst.exists():
                if src.suffix == '.strm':
                    raise FileNotFoundError(missing_strm_message)
            moved_count += 1
            _core._record_job_progress(conn, job_id, 'moving_files', completed=moved_count, total=file_total)


def _move_hidden_files(conn, job_id, snapshots, services, run) -> None:
    """Move each hidden item's library files into quarantine, recording moves for compensation."""
    _move_snapshot_files(conn, job_id, snapshots, services, run, 'Missing STRM file for loser item.')
