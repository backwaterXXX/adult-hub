"""Dispatch helpers for durable duplicate decision jobs: catalog results and simple runners.

Moved verbatim from duplicate_job_worker.py. Shared state still comes from the
duplicate_jobs facade through ``_core``, so test patches on the facade keep applying.
"""

from __future__ import annotations
import uuid

if __package__:
    from . import duplicate_jobs as _core
    from . import duplicate_job_external_handlers as _external_handlers
else:
    import duplicate_jobs as _core
    import duplicate_job_external_handlers as _external_handlers


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
