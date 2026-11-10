"""Hide steps for durable duplicate decision jobs: validation, snapshots and the hide run.

Moved verbatim from duplicate_job_worker.py. Shared state still comes from the
duplicate_jobs facade through ``_core``, so test patches on the facade keep applying.
"""

from __future__ import annotations

from sync import duplicate_jobs as _core
from sync.duplicate_job_files import (
    _append_sidecar_moves, _check_journal_ids, _load_recovery_snapshots,
    _move_hidden_files,
)
from sync.duplicate_job_merge import _merge_keeper_metadata


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
