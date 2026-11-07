"""Bounded cleanup for expired administrative and job history."""

from __future__ import annotations

import logging

from common import db

LOGGER = logging.getLogger(__name__)
BATCH_SIZE = 2_000


def _delete_batches(conn: db.Connection, statement: str) -> int:
    total = 0
    while True:
        cursor = conn.execute(statement, (BATCH_SIZE,))
        deleted = max(0, cursor.rowcount)
        conn.commit()
        total += deleted
        if deleted < BATCH_SIZE:
            return total


def prune_history(conn: db.Connection | None = None) -> dict[str, int]:
    """Delete reversible or append-only history older than one calendar month.

    Current active decisions, active quarantines, and running jobs are retained.
    A quarantine's source audit job is retained until the quarantine record is
    itself eligible for removal.
    """
    owned = conn is None
    connection = conn or db.connect()
    try:
        statements = {
            "admin_orphan_quarantine": """
                WITH expired AS (
                    SELECT operation_id FROM admin_orphan_quarantine
                    WHERE state='restored'
                      AND created_at < now() - interval '1 month'
                    ORDER BY created_at,operation_id LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                DELETE FROM admin_orphan_quarantine q USING expired e
                WHERE q.operation_id=e.operation_id
            """,
            "admin_action_events": """
                WITH expired AS (
                    SELECT id FROM admin_action_events
                    WHERE created_at < now() - interval '1 month'
                    ORDER BY created_at,id LIMIT %s FOR UPDATE SKIP LOCKED
                )
                DELETE FROM admin_action_events e USING expired x WHERE e.id=x.id
            """,
            "admin_audit_events": """
                WITH expired AS (
                    SELECT id FROM admin_audit_events
                    WHERE created_at < now() - interval '1 month'
                    ORDER BY created_at,id LIMIT %s FOR UPDATE SKIP LOCKED
                )
                DELETE FROM admin_audit_events e USING expired x WHERE e.id=x.id
            """,
            "admin_operation_plans": """
                WITH expired AS (
                    SELECT plan_id FROM admin_operation_plans
                    WHERE created_at < now() - interval '1 month'
                    ORDER BY created_at,plan_id LIMIT %s FOR UPDATE SKIP LOCKED
                )
                DELETE FROM admin_operation_plans p USING expired e WHERE p.plan_id=e.plan_id
            """,
            "external_metadata_attempts": """
                WITH expired AS (
                    SELECT entity_type,entity_key,source FROM external_metadata_attempts
                    WHERE updated_at < now() - interval '1 month'
                    ORDER BY updated_at,entity_type,entity_key,source LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                DELETE FROM external_metadata_attempts a USING expired e
                WHERE a.entity_type=e.entity_type AND a.entity_key=e.entity_key AND a.source=e.source
            """,
            "video_availability_checks": """
                WITH expired AS (
                    SELECT id FROM video_availability_checks
                    WHERE checked_at < now() - interval '1 month'
                    ORDER BY checked_at,id LIMIT %s FOR UPDATE SKIP LOCKED
                )
                DELETE FROM video_availability_checks a USING expired e WHERE a.id=e.id
            """,
            "video_duplicate_decisions": """
                WITH expired AS (
                    SELECT loser_id FROM video_duplicate_decisions
                    WHERE state='undone'
                      AND created_at < now() - interval '1 month'
                    ORDER BY created_at,loser_id LIMIT %s FOR UPDATE SKIP LOCKED
                )
                DELETE FROM video_duplicate_decisions d USING expired e WHERE d.loser_id=e.loser_id
            """,
            "video_duplicate_dismissals": """
                WITH expired AS (
                    SELECT left_id,right_id FROM video_duplicate_dismissals
                    WHERE active=0
                      AND created_at < now() - interval '1 month'
                    ORDER BY created_at,left_id,right_id LIMIT %s FOR UPDATE SKIP LOCKED
                )
                DELETE FROM video_duplicate_dismissals d USING expired e
                WHERE d.left_id=e.left_id AND d.right_id=e.right_id
            """,
            "video_duplicate_jobs": """
                WITH expired AS (
                    SELECT j.id FROM video_duplicate_jobs j
                    WHERE j.state IN ('done','failed','cancelled')
                      AND j.created_at < now() - interval '1 month'
                      AND NOT EXISTS (
                          SELECT 1 FROM admin_orphan_quarantine q WHERE q.audit_id=j.id
                      )
                    ORDER BY j.created_at,j.id LIMIT %s FOR UPDATE SKIP LOCKED
                ), deleted AS (
                    DELETE FROM video_duplicate_jobs j USING expired e
                    WHERE j.id=e.id RETURNING j.id
                ), schedules AS (
                    UPDATE admin_schedules s SET last_job_id=NULL
                    WHERE s.last_job_id IN (SELECT id FROM deleted)
                    RETURNING s.id
                ), system_scheds AS (
                    UPDATE system_schedules s SET last_job_id=NULL
                    WHERE s.last_job_id IN (SELECT id FROM deleted)
                    RETURNING s.id
                )
                SELECT count(*) FROM deleted
            """,
        }
        counts: dict[str, int] = {}
        for name, sql in statements.items():
            if name == "video_duplicate_jobs":
                total = 0
                while True:
                    cursor = connection.execute(sql, (BATCH_SIZE,))
                    deleted = int(cursor.fetchone()[0])
                    connection.commit()
                    total += deleted
                    if deleted < BATCH_SIZE:
                        break
                counts[name] = total
            else:
                counts[name] = _delete_batches(connection, sql)
        LOGGER.info("Monthly history retention completed counts=%s", counts)
        return counts
    except Exception:
        connection.rollback()
        raise
    finally:
        if owned:
            connection.close()
