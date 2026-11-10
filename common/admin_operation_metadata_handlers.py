"""Admin operation handlers for metadata changes."""
from __future__ import annotations

from common import admin_operation_execution as _core


def handle_metadata_update_batch(conn, db_url, plan_id, row, config, scope, revisions, now):
    if row['operation_type'] == 'metadata_update_batch':
        from common.video_metadata import MetadataValidationError, load_metadata_states, metadata_source_fingerprint, validate_override_patch
        video_ids = scope.get('video_ids') if isinstance(scope, dict) else None
        expected_revisions = revisions if isinstance(revisions, dict) else {}
        fingerprints = scope.get('source_fingerprints') if isinstance(scope, dict) else None
        try:
            changes, reset_fields = validate_override_patch(scope.get('changes'), scope.get('reset_fields'))
        except (MetadataValidationError, AttributeError):
            conn.rollback()
            return {'outcome': 'conflict', 'message': 'The saved metadata batch is invalid.'}
        if not isinstance(video_ids, list) or not 1 <= len(video_ids) <= _core.MAX_QUARANTINE_BATCH or any((not isinstance(video_id, str) or not video_id for video_id in video_ids)) or (len(set(video_ids)) != len(video_ids)) or (not isinstance(fingerprints, dict)) or (not changes and (not reset_fields)):
            conn.rollback()
            return {'outcome': 'conflict', 'message': 'The saved metadata batch is invalid.'}
        conn.execute(
            'SELECT id FROM videos WHERE id = ANY(%s) ORDER BY id FOR UPDATE',
            (sorted(video_ids),),
        ).fetchall()
        current_states = load_metadata_states(conn, video_ids)
        stale_ids = []
        for video_id in video_ids:
            current = current_states.get(video_id)
            if current is None or expected_revisions.get(video_id) != current['revision'] or fingerprints.get(video_id) != metadata_source_fingerprint(current['source']):
                stale_ids.append(video_id)
        if stale_ids:
            conn.commit()
            return {'outcome': 'stale', 'stale_ids': stale_ids, 'message': 'One or more videos changed after this preview. Create a new preview.'}
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('enqueue_job:metadata_update',))
        active_jobs = _core._active_metadata_jobs(conn, {
            video_id: (
                current_states[video_id]['revision'], fingerprints[video_id], changes, reset_fields,
            )
            for video_id in video_ids
        })
        jobs = []
        queued_entries = []
        for video_id in video_ids:
            current = current_states[video_id]
            expected_revision = current['revision']
            source_fingerprint = fingerprints[video_id]
            job_id = active_jobs.get(video_id)
            reused = job_id is not None
            if not reused:
                job_id = _core.uuid.uuid4().hex
                payload = {'video_id': video_id, 'expected_revision': expected_revision, 'expected_source_fingerprint': source_fingerprint, 'changes': changes, 'reset_fields': reset_fields}
                queued_entries.append((job_id, _core.json.dumps(payload, ensure_ascii=False)))
            jobs.append({'video_id': video_id, 'job_id': job_id, 'reused': reused})
        _core._insert_queued_jobs(conn, 'metadata_update', queued_entries, now)
        result = {'jobs': jobs}
        conn.execute("UPDATE admin_operation_plans SET state='queued',result=%s,consumed_at=to_timestamp(%s) WHERE plan_id=%s AND state='previewed'", (_core.Jsonb(result), now, plan_id))
        reused_all = all((job['reused'] for job in jobs))
        _core._record_operation_queue_audit(conn, plan_id, row['operation_type'], scope, jobs, reused_all)
        conn.commit()
        return {'outcome': 'queued', 'plan_id': plan_id, 'jobs': jobs, 'reused': reused_all}
    return None

def handle_metadata_update(conn, db_url, plan_id, row, config, scope, revisions, now):
    if row['operation_type'] == 'metadata_update':
        from common.video_metadata import MetadataValidationError, preview_metadata_update
        video_id = str(scope.get('video_id') or '') if isinstance(scope, dict) else ''
        expected_revision = scope.get('expected_revision') if isinstance(scope, dict) else None
        try:
            current_preview = preview_metadata_update(db_url, video_id, expected_revision, scope.get('changes'), scope.get('reset_fields'))
        except MetadataValidationError:
            conn.rollback()
            return {'outcome': 'conflict', 'message': 'The saved metadata plan is invalid.'}
        if current_preview.get('outcome') == 'stale':
            conn.commit()
            return {'outcome': 'stale', 'stale_ids': [video_id], 'message': 'Metadata changed after this preview. Create a new preview.'}
        from common.video_metadata import load_metadata_state, metadata_source_fingerprint
        current_metadata = load_metadata_state(conn, video_id)
        if current_metadata is None or metadata_source_fingerprint(current_metadata['source']) != scope.get('source_fingerprint'):
            conn.commit()
            return {'outcome': 'stale', 'stale_ids': [video_id], 'message': 'Metadata changed after this preview. Create a new preview.'}
        proposal_id = scope.get('metadata_proposal_id')
        if proposal_id is not None:
            proposal = conn.execute(
                'SELECT video_id,status,expected_revision,expected_source_fingerprint '
                'FROM metadata_review_proposals WHERE id=%s FOR UPDATE', (proposal_id,),
            ).fetchone()
            if (proposal is None or str(proposal[0]) != video_id or str(proposal[1]) != 'pending'
                    or int(proposal[2]) != int(expected_revision)
                    or str(proposal[3]) != str(scope.get('source_fingerprint') or '')):
                conn.commit()
                return {'outcome': 'stale', 'stale_ids': [video_id], 'message': 'This metadata proposal changed or was already reviewed. Refresh the proposal list.'}
        if current_preview.get('outcome') != 'previewed':
            conn.commit()
            return {'outcome': 'conflict', 'message': 'The metadata plan can no longer be applied.'}
        conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('enqueue_job:metadata_update',))
        job_id = _core._active_metadata_job(conn, video_id, int(expected_revision), str(scope.get('source_fingerprint') or ''), current_preview['changes'], current_preview['reset_fields'])
        reused = job_id is not None
        if not reused:
            job_id = _core.uuid.uuid4().hex
            payload = {'video_id': video_id, 'expected_revision': int(expected_revision), 'expected_source_fingerprint': scope.get('source_fingerprint'), 'changes': current_preview['changes'], 'reset_fields': current_preview['reset_fields']}
            conn.execute("INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) VALUES (%s,'metadata_update','queued',%s,NULL,%s,%s)", (job_id, _core.json.dumps(payload, ensure_ascii=False), now, now))
        jobs = [{'video_id': video_id, 'job_id': job_id, 'reused': reused}]
        result = {'jobs': jobs}
        conn.execute("UPDATE admin_operation_plans SET state='queued',result=%s,consumed_at=to_timestamp(%s) WHERE plan_id=%s AND state='previewed'", (_core.Jsonb(result), now, plan_id))
        if proposal_id is not None:
            conn.execute(
                "UPDATE metadata_review_proposals SET status='accepted',reviewed_at=to_timestamp(%s) "
                "WHERE id=%s AND status='pending'", (now, proposal_id),
            )
        _core._record_operation_queue_audit(conn, plan_id, row['operation_type'], scope, jobs, reused)
        conn.commit()
        return {'outcome': 'queued', 'plan_id': plan_id, 'jobs': jobs, 'reused': reused}
    return None
