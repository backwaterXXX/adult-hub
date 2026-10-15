"""Migration 0020: extend safe before/after history to queued operations and schedules."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    # Constraint names can differ after a restore or manual schema repair.
    # Remove only CHECK constraints whose definitions refer to these columns.
    conn.execute("""
        DO $$
        DECLARE constraint_row RECORD;
        BEGIN
            FOR constraint_row IN
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'admin_audit_events'::regclass
                  AND contype = 'c'
                  AND (position('event_type' in pg_get_constraintdef(oid)) > 0
                       OR position('object_type' in pg_get_constraintdef(oid)) > 0)
            LOOP
                EXECUTE format('ALTER TABLE admin_audit_events DROP CONSTRAINT %I', constraint_row.conname);
            END LOOP;
        END
        $$
    """)
    conn.execute("""
        ALTER TABLE admin_audit_events
        ADD CONSTRAINT admin_audit_events_event_type_check
        CHECK (event_type IN (
            'metadata_update','source_settings_update','admin_operation_queued',
            'schedule_created','schedule_enabled'
        ))
    """)
    conn.execute("""
        ALTER TABLE admin_audit_events
        ADD CONSTRAINT admin_audit_events_object_type_check
        CHECK (object_type IN ('video','source_configuration','operation_plan','schedule'))
    """)


migration = Migration(
    version=20,
    name="0020_admin_audit_operations_schedules",
    description="Record safe operation-plan and schedule before/after changes",
    apply_fn=apply,
)
