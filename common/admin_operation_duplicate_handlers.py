"""Admin operation handlers for duplicate decisions and video quarantine."""
from __future__ import annotations

if __package__:
    from . import admin_operation_execution as _core
else:
    import admin_operation_execution as _core


def handle_restore(conn, db_url, plan_id, row, config, scope, revisions, now):
    if row['operation_type'] == 'restore':
        operation_id = str(scope.get('operation_id') or '') if isinstance(scope, dict) else ''
        snapshots = _core._restore_snapshot_rows(conn, operation_id)
        current = _core.hashlib.sha256(_core._canonical([{'video_id': video_id, 'snapshot': snapshot} for video_id, snapshot in snapshots]).encode('utf-8')).hexdigest() if snapshots else None
        if not operation_id or current != revisions.get('operation_id'):
            conn.commit()
            preview = row['preview'] if isinstance(row['preview'], dict) else _core.json.loads(row['preview'] or '{}')
            stale_ids = [str(item.get('video_id')) for item in preview.get('items', []) if isinstance(item, dict) and item.get('video_id')]
            return {'outcome': 'stale', 'stale_ids': stale_ids, 'message': 'The recorded decision changed after this preview. Create a new preview.'}
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('enqueue_job:undo',))
        job_id = _core._active_undo_job(conn, operation_id)
        reused = job_id is not None
        if not reused:
            job_id = _core.uuid.uuid4().hex
            conn.execute("INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) VALUES (%s,'undo','queued',%s,NULL,%s,%s)", (job_id, _core.json.dumps({'operation_id': operation_id}), now, now))
        jobs = [{'job_id': job_id, 'reused': reused}]
        result = {'jobs': jobs}
        conn.execute("UPDATE admin_operation_plans SET state='queued',result=%s,consumed_at=to_timestamp(%s) WHERE plan_id=%s AND state='previewed'", (_core.Jsonb(result), now, plan_id))
        _core._record_operation_queue_audit(conn, plan_id, row['operation_type'], scope, jobs, reused)
        conn.commit()
        return {'outcome': 'queued', 'plan_id': plan_id, 'jobs': jobs, 'reused': reused}
    return None

def handle_hide(conn, db_url, plan_id, row, config, scope, revisions, now):
    if row['operation_type'] == 'hide':
        keeper_id = str(scope.get('keeper_id') or '') if isinstance(scope, dict) else ''
        loser_ids = scope.get('loser_ids') if isinstance(scope, dict) else None
        if not keeper_id or not isinstance(loser_ids, list) or (not loser_ids) or (len(loser_ids) > _core.MAX_QUARANTINE_BATCH):
            conn.rollback()
            return {'outcome': 'conflict', 'message': 'The saved operation scope is invalid.'}
        stale_ids = []
        snapshots = _core._video_snapshots(conn, [keeper_id, *map(str, loser_ids)], config)
        for video_id in [keeper_id, *map(str, loser_ids)]:
            current, snapshot = snapshots.get(video_id, (None, None))
            if current is None or current != revisions.get(video_id) or snapshot['active_decision'] or snapshot['source_disabled']:
                stale_ids.append(video_id)
        if stale_ids:
            conn.commit()
            return {'outcome': 'stale', 'stale_ids': stale_ids, 'message': 'Library data changed after this preview. Create a new preview.'}
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('enqueue_job:hide',))
        job_id = _core._active_hide_job(conn, keeper_id, list(map(str, loser_ids)))
        reused = job_id is not None
        if not reused:
            job_id = _core.uuid.uuid4().hex
            payload = {'keeper_id': keeper_id, 'loser_ids': list(map(str, loser_ids)), 'expected_revisions': revisions}
            if scope.get('operator_review') is True:
                payload['operator_review'] = True
            rules_version = scope.get('rules_version') if isinstance(scope, dict) else None
            if type(rules_version) is int and rules_version > 0:
                payload['rules_version'] = rules_version
            conn.execute("INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) VALUES (%s,'hide','queued',%s,NULL,%s,%s)", (job_id, _core.json.dumps(payload, ensure_ascii=False), now, now))
        jobs = [{'job_id': job_id, 'reused': reused}]
        result = {'jobs': jobs}
        conn.execute("UPDATE admin_operation_plans SET state='queued',result=%s,consumed_at=to_timestamp(%s) WHERE plan_id=%s AND state='previewed'", (_core.Jsonb(result), now, plan_id))
        _core._record_operation_queue_audit(conn, plan_id, row['operation_type'], scope, jobs, reused)
        conn.commit()
        return {'outcome': 'queued', 'plan_id': plan_id, 'jobs': jobs, 'reused': reused}
    return None

def handle_hide_batch(conn, db_url, plan_id, row, config, scope, revisions, now):
    if row['operation_type'] == 'hide_batch':
        groups = scope.get('groups') if isinstance(scope, dict) else None
        if not isinstance(groups, list) or not groups or len(groups) > _core.MAX_QUARANTINE_BATCH:
            conn.rollback()
            return {'outcome': 'conflict', 'message': 'The saved operation scope is invalid.'}
        stale_ids = []
        normalized_groups = []
        total_losers = 0
        for group in groups:
            if not isinstance(group, dict):
                conn.rollback()
                return {'outcome': 'conflict', 'message': 'The saved operation scope is invalid.'}
            keeper_id = str(group.get('keeper_id') or '')
            loser_ids = group.get('loser_ids')
            if not keeper_id or not isinstance(loser_ids, list) or (not loser_ids):
                conn.rollback()
                return {'outcome': 'conflict', 'message': 'The saved operation scope is invalid.'}
            loser_ids = list(map(str, loser_ids))
            total_losers += len(loser_ids)
            if total_losers > _core.MAX_QUARANTINE_BATCH:
                conn.rollback()
                return {'outcome': 'conflict', 'message': 'The saved operation scope is invalid.'}
            normalized_groups.append((keeper_id, loser_ids))
        snapshot_ids = list(dict.fromkeys(
            video_id for keeper_id, loser_ids in normalized_groups for video_id in [keeper_id, *loser_ids]
        ))
        snapshots = _core._video_snapshots(conn, snapshot_ids, config)
        dismissal_pairs = {
            tuple(sorted((keeper_id, loser_id)))
            for keeper_id, loser_ids in normalized_groups
            for loser_id in loser_ids
        }
        dismissed_pairs = set()
        if dismissal_pairs:
            pair_values = sorted(dismissal_pairs)
            pair_predicate = " OR ".join("(left_id=%s AND right_id=%s)" for _pair in pair_values)
            pair_params = [video_id for pair in pair_values for video_id in pair]
            dismissed_pairs = {
                tuple(sorted((str(left_id), str(right_id))))
                for left_id, right_id in conn.execute(
                    "SELECT left_id,right_id FROM video_duplicate_dismissals "
                    f"WHERE active=1 AND ({pair_predicate})",
                    pair_params,
                ).fetchall()
            }
        for keeper_id, loser_ids in normalized_groups:
            for video_id in [keeper_id, *loser_ids]:
                current, snapshot = snapshots.get(video_id, (None, None))
                if current is None or current != revisions.get(video_id) or snapshot['active_decision'] or snapshot['source_disabled'] or (video_id != keeper_id and snapshot['shared_paths']):
                    stale_ids.append(video_id)
            for loser_id in loser_ids:
                if tuple(sorted((keeper_id, loser_id))) in dismissed_pairs:
                    stale_ids.append(loser_id)
        if stale_ids:
            conn.commit()
            return {'outcome': 'stale', 'stale_ids': list(dict.fromkeys(stale_ids)), 'message': 'Library data changed after this preview. Create a new preview.'}
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('enqueue_job:hide',))
        active_jobs = _core._active_hide_jobs(conn, [
            (keeper_id, tuple(loser_ids)) for keeper_id, loser_ids in normalized_groups
        ])
        jobs = []
        queued_entries = []
        for keeper_id, loser_ids in normalized_groups:
            target = (keeper_id, tuple(sorted(set(loser_ids))))
            job_id = active_jobs.get(target)
            reused = job_id is not None
            if not reused:
                job_id = _core.uuid.uuid4().hex
                group_revisions = {video_id: revisions[video_id] for video_id in [keeper_id, *loser_ids]}
                payload = {'keeper_id': keeper_id, 'loser_ids': loser_ids, 'expected_revisions': group_revisions}
                queued_entries.append((job_id, _core.json.dumps(payload, ensure_ascii=False)))
                active_jobs[target] = job_id
            jobs.append({'job_id': job_id, 'reused': reused})
        _core._insert_queued_jobs(conn, 'hide', queued_entries, now)
        result = {'jobs': jobs}
        conn.execute("UPDATE admin_operation_plans SET state='queued',result=%s,consumed_at=to_timestamp(%s) WHERE plan_id=%s AND state='previewed'", (_core.Jsonb(result), now, plan_id))
        _core._record_operation_queue_audit(conn, plan_id, row['operation_type'], scope, jobs, all((job['reused'] for job in jobs)))
        conn.commit()
        return {'outcome': 'queued', 'plan_id': plan_id, 'jobs': jobs, 'reused': all((job['reused'] for job in jobs))}
    return None

def handle_performer_merge(conn, db_url, plan_id, row, config, scope, revisions, now):
    if row['operation_type'] == 'performer_merge':
        alias = str(scope.get('alias') or '').strip() if isinstance(scope, dict) else ''
        canonical = str(scope.get('canonical') or '').strip() if isinstance(scope, dict) else ''
        expected = revisions.get('performer_merge') if isinstance(revisions, dict) else None
        if not alias or not canonical or alias.lower() == canonical.lower() or (not isinstance(expected, str)):
            conn.rollback()
            return {'outcome': 'conflict', 'message': 'The saved performer merge scope is invalid.'}
        try:
            from common.fuzzy_performers import performer_merge_revision
        except ImportError:
            from fuzzy_performers import performer_merge_revision
        if performer_merge_revision(conn, alias, canonical) != expected:
            conn.commit()
            return {'outcome': 'stale', 'stale_ids': [alias], 'message': 'Performer records changed after this preview. Create a new preview.'}
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('enqueue_job:performer_merge',))
        job_id = None
        for queued_id, queued_payload in conn.execute("SELECT id,payload FROM video_duplicate_jobs WHERE action='performer_merge' AND state IN ('queued','processing') ORDER BY created_at,id").fetchall():
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
            conn.execute("INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) VALUES (%s,'performer_merge','queued',%s,NULL,%s,%s)", (job_id, _core.json.dumps({'plan_id': plan_id, 'expected_revision': expected, 'merges': [{'alias': alias, 'canonical': canonical}]}, ensure_ascii=False), now, now))
        jobs = [{'job_id': job_id, 'reused': reused}]
        conn.execute("UPDATE admin_operation_plans SET state='queued',result=%s,consumed_at=to_timestamp(%s) WHERE plan_id=%s AND state='previewed'", (_core.Jsonb({'jobs': jobs}), now, plan_id))
        _core._record_operation_queue_audit(conn, plan_id, row['operation_type'], scope, jobs, reused)
        conn.commit()
        return {'outcome': 'queued', 'plan_id': plan_id, 'jobs': jobs, 'reused': reused}
    return None

def handle_quarantine(conn, db_url, plan_id, row, config, scope, revisions, now):
    if row['operation_type'] != 'quarantine':
        conn.rollback()
        return {'outcome': 'invalid', 'message': 'Unsupported operation type.'}
    ids = scope.get('video_ids') if isinstance(scope, dict) else None
    if not isinstance(ids, list) or not ids:
        conn.rollback()
        return {'outcome': 'conflict', 'message': 'The saved operation scope is invalid.'}
    stale_ids = []
    snapshots = _core._video_snapshots(conn, list(map(str, ids)), config)
    for video_id in ids:
        current, snapshot = snapshots.get(str(video_id), (None, None))
        if current is None or current != revisions.get(str(video_id)) or snapshot['active_decision'] or snapshot['source_disabled'] or snapshot['shared_paths']:
            stale_ids.append(str(video_id))
    if stale_ids:
        conn.commit()
        return {'outcome': 'stale', 'stale_ids': stale_ids, 'message': 'Library data changed after this preview. Create a new preview.'}
    conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('enqueue_job:quarantine',))
    active_quarantine_jobs = _core._active_quarantine_jobs(conn, list(map(str, ids)))
    jobs = []
    queued_entries = []
    for video_id in ids:
        video_id = str(video_id)
        job_id = active_quarantine_jobs.get(video_id)
        reused = job_id is not None
        if not reused:
            payload = {'video_id': video_id, 'expected_revision': revisions[str(video_id)]}
            job_id = _core.uuid.uuid4().hex
            queued_entries.append((job_id, _core.json.dumps(payload, ensure_ascii=False)))
        jobs.append({'video_id': video_id, 'job_id': job_id, 'reused': reused})
    _core._insert_queued_jobs(conn, 'quarantine', queued_entries, now)
    result = {'jobs': jobs}
    conn.execute("UPDATE admin_operation_plans SET state='queued',result=%s,consumed_at=to_timestamp(%s) WHERE plan_id=%s AND state='previewed'", (_core.Jsonb(result), now, plan_id))
    _core._record_operation_queue_audit(conn, plan_id, row['operation_type'], scope, jobs, all((job['reused'] for job in jobs)))
    conn.commit()
    return {'outcome': 'queued', 'plan_id': plan_id, 'jobs': jobs, 'reused': all((job['reused'] for job in jobs))}
    return None
