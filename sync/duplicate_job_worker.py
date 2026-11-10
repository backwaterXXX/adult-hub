"""Execution loop for durable duplicate decision jobs."""
from __future__ import annotations
import logging


logger = logging.getLogger(__name__)

from sync import duplicate_jobs as _core  # noqa: E402
from sync import duplicate_job_external_handlers as _external_handlers  # noqa: E402
from sync.duplicate_job_undo import _run_undo  # noqa: E402
from sync.duplicate_job_quarantine import _run_quarantine  # noqa: E402
from sync.duplicate_job_hide import _run_hide  # noqa: E402
from sync.duplicate_job_dispatch import _catalog_result, _run_catalog_full_audit, _run_external_action, _run_dismiss, _run_undismiss  # noqa: E402


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


def _handle_system_schedule_start(conn: _core.db.Connection, job_id: str, action: str, payload: dict) -> None:
    if action in {'backup_create', 'scene_backfill'} and payload.get('_system_schedule_id'):
        from common.system_schedules import record_system_schedule_job_start
        record_system_schedule_job_start(conn, job_id, payload)


def _handle_system_schedule_finish(conn: _core.db.Connection, job_id: str, action: str, payload: dict, *, success: bool) -> None:
    if action in {'backup_create', 'scene_backfill'} and payload.get('_system_schedule_id'):
        from common.system_schedules import finish_system_schedule_job, maybe_enqueue_backup_history_cleanup
        finish_system_schedule_job(conn, job_id, payload, success=success)
        if success and action == 'backup_create':
            maybe_enqueue_backup_history_cleanup(conn, payload)


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
        _handle_system_schedule_start(conn, job_id, action, payload)
        quarantine_root = services.jobs_lock_root(conn, media_dir) / 'duplicate-quarantine'
        run = _JobRun()
        early_result = _committed_result_without_rerun(conn, job_id, action, payload, services)
        if early_result is not None:
            _core._finish_job(conn, job_id, early_result)
            _handle_system_schedule_finish(conn, job_id, action, payload, success=True)
            completed += 1
            continue
        heartbeat_stop = _core.threading.Event()
        heartbeat = _core.threading.Thread(target=_core._heartbeat_processing_job, args=(getattr(conn, '_url', None) or _core.os.getenv('DATABASE_URL'), job_id, heartbeat_stop, services.logger), name=f'job-heartbeat-{job_id[:8]}', daemon=True)
        heartbeat.start()
        try:
            result = _execute_action(conn, job_id, action, payload, media_dir, services, quarantine_root, now, run)
            _core._finish_job(conn, job_id, result)
            _handle_system_schedule_finish(conn, job_id, action, payload, success=True)
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
            _handle_system_schedule_finish(conn, job_id, action, payload, success=False)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
    return completed
