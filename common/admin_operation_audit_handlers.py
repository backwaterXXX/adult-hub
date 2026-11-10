"""Admin operation handlers for file and consistency audit plans."""
from __future__ import annotations

from common import admin_operation_execution as _core


def handle_orphan_file_operation(conn, db_url, plan_id, row, config, scope, revisions, now):
    if row['operation_type'] in {'orphan_file_quarantine', 'orphan_file_restore'}:
        operation_type = str(row['operation_type'])
        if operation_type == 'orphan_file_quarantine':
            audit_id = str(scope.get('audit_id') or '') if isinstance(scope, dict) else ''
            finding_ids = scope.get('finding_ids') if isinstance(scope, dict) else None
            if not audit_id or not isinstance(finding_ids, list) or (not finding_ids) or (len(finding_ids) > _core.MAX_ORPHAN_QUARANTINE_BATCH) or any((type(value) is not int or value <= 0 for value in finding_ids)):
                conn.rollback()
                return {'outcome': 'conflict', 'message': 'The saved orphan quarantine scope is invalid.'}
            current = _core._orphan_finding_state(conn, audit_id, finding_ids, _core.Path(_core.os.getenv('MEDIA_DIR', '/media/adult')))
            if current is None or current['revisions'] != revisions:
                conn.commit()
                return {'outcome': 'stale', 'stale_ids': list(map(str, finding_ids)), 'message': 'Audit findings or files changed after this preview. Create a new preview.'}
            job_payload = {'mode': 'quarantine', 'plan_id': plan_id}
        else:
            operation_id = str(scope.get('operation_id') or '') if isinstance(scope, dict) else ''
            entry = conn.execute('SELECT state,files FROM admin_orphan_quarantine WHERE operation_id=%s FOR UPDATE', (operation_id,)).fetchone()
            files = entry[1] if entry and isinstance(entry[1], list) else _core.json.loads(entry[1] or '[]') if entry else []
            digest = _core.hashlib.sha256(_core._canonical(files).encode('utf-8')).hexdigest() if files else None
            if not operation_id or not entry or entry[0] != 'active' or (digest != revisions.get('operation_id')):
                conn.commit()
                return {'outcome': 'stale', 'stale_ids': [operation_id] if operation_id else [], 'message': 'The quarantine record changed after this preview. Create a new preview.'}
            job_payload = {'mode': 'restore', 'operation_id': operation_id}
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('enqueue_job:orphan_file_operation',))
        reused_job_id = _core._active_orphan_file_job(conn, str(job_payload['mode']), scope, revisions)
        reused = reused_job_id is not None
        job_id = reused_job_id or _core.uuid.uuid4().hex
        if not reused:
            conn.execute("INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) VALUES (%s,'orphan_file_operation','queued',%s,NULL,%s,%s)", (job_id, _core.json.dumps(job_payload, ensure_ascii=False), now, now))
        jobs = [{'job_id': job_id, 'reused': reused}]
        conn.execute("UPDATE admin_operation_plans SET state='queued',result=%s,consumed_at=to_timestamp(%s) WHERE plan_id=%s AND state='previewed'", (_core.Jsonb({'jobs': jobs}), now, plan_id))
        _core._record_operation_queue_audit(conn, plan_id, operation_type, scope, jobs, reused)
        conn.commit()
        return {'outcome': 'queued', 'plan_id': plan_id, 'jobs': jobs, 'reused': reused}
    return None

def handle_audit_repair_nfo(conn, db_url, plan_id, row, config, scope, revisions, now):
    if row['operation_type'] == 'audit_repair_nfo':
        audit_id = str(scope.get('audit_id') or '') if isinstance(scope, dict) else ''
        finding_ids = scope.get('finding_ids') if isinstance(scope, dict) else None
        if not audit_id or not isinstance(finding_ids, list) or (not finding_ids) or (len(finding_ids) > _core.MAX_AUDIT_REPAIR_BATCH) or any((type(value) is not int or value <= 0 for value in finding_ids)):
            conn.rollback()
            return {'outcome': 'conflict', 'message': 'The saved repair scope is invalid.'}
        current = _core._audit_repair_state(conn, audit_id, finding_ids)
        if current is None or current['revisions'] != revisions:
            conn.commit()
            return {'outcome': 'stale', 'stale_ids': [str(item.get('video_id')) for item in (row['preview'].get('items', []) if isinstance(row['preview'], dict) else [])], 'message': 'Audit findings or library metadata changed after this preview. Create a new preview.'}
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('enqueue_job:audit_repair_nfo',))
        job_id = None
        for queued_id, queued_payload in conn.execute("SELECT id,payload FROM video_duplicate_jobs WHERE action='audit_repair_nfo' AND state IN ('queued','processing') ORDER BY created_at,id").fetchall():
            try:
                decoded = _core.json.loads(queued_payload) if isinstance(queued_payload, str) else queued_payload
            except (TypeError, ValueError):
                continue
            if isinstance(decoded, dict) and decoded.get('plan_id') == plan_id:
                job_id = str(queued_id)
                break
        reused = job_id is not None
        if not reused:
            job_id = _core.uuid.uuid4().hex
            conn.execute("INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) VALUES (%s,'audit_repair_nfo','queued',%s,NULL,%s,%s)", (job_id, _core.json.dumps({'plan_id': plan_id}, ensure_ascii=False), now, now))
        jobs = [{'job_id': job_id, 'reused': reused}]
        result = {'jobs': jobs}
        conn.execute("UPDATE admin_operation_plans SET state='queued',result=%s,consumed_at=to_timestamp(%s) WHERE plan_id=%s AND state='previewed'", (_core.Jsonb(result), now, plan_id))
        _core._record_operation_queue_audit(conn, plan_id, row['operation_type'], scope, jobs, reused)
        conn.commit()
        return {'outcome': 'queued', 'plan_id': plan_id, 'jobs': jobs, 'reused': reused}
    return None

def handle_audit_import_refresh(conn, db_url, plan_id, row, config, scope, revisions, now):
    if row['operation_type'] == 'audit_import_refresh':
        audit_id = str(scope.get('audit_id') or '') if isinstance(scope, dict) else ''
        finding_ids = scope.get('finding_ids') if isinstance(scope, dict) else None
        if not audit_id or not isinstance(finding_ids, list) or (not finding_ids) or (len(finding_ids) > _core.MAX_AUDIT_REPAIR_BATCH) or any((type(value) is not int or value <= 0 for value in finding_ids)):
            conn.rollback()
            return {'outcome': 'conflict', 'message': 'The saved import refresh scope is invalid.'}
        current = _core._audit_import_refresh_state(conn, audit_id, finding_ids)
        if current is None or current['revisions'] != revisions:
            conn.commit()
            return {'outcome': 'stale', 'stale_ids': list(revisions), 'message': 'Audit findings or library metadata changed after this preview. Create a new preview.'}
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('enqueue_job:audit_import_refresh',))
        job_id = None
        for queued_id, queued_payload in conn.execute("SELECT id,payload FROM video_duplicate_jobs WHERE action='audit_import_refresh' AND state IN ('queued','processing') ORDER BY created_at,id").fetchall():
            try:
                decoded = _core.json.loads(queued_payload) if isinstance(queued_payload, str) else queued_payload
            except (TypeError, ValueError):
                continue
            if isinstance(decoded, dict) and decoded.get('plan_id') == plan_id:
                job_id = str(queued_id)
                break
        reused = job_id is not None
        if not reused:
            job_id = _core.uuid.uuid4().hex
            conn.execute("INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) VALUES (%s,'audit_import_refresh','queued',%s,NULL,%s,%s)", (job_id, _core.json.dumps({'plan_id': plan_id}), now, now))
        jobs = [{'job_id': job_id, 'reused': reused}]
        conn.execute("UPDATE admin_operation_plans SET state='queued',result=%s,consumed_at=to_timestamp(%s) WHERE plan_id=%s AND state='previewed'", (_core.Jsonb({'jobs': jobs}), now, plan_id))
        _core._record_operation_queue_audit(conn, plan_id, row['operation_type'], scope, jobs, reused)
        conn.commit()
        return {'outcome': 'queued', 'plan_id': plan_id, 'jobs': jobs, 'reused': reused}
    return None
