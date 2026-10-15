"""Track manual performer profile values separately from provider enrichment."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS performer_profile_overrides (
            name TEXT PRIMARY KEY,
            fields JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(fields) = 'object'),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Keep every unrelated audit check constraint intact; widen only the two
    # known allowlists created by m0019. Fresh installs have both constraints
    # by this point in the ordered chain, while IF EXISTS also tolerates a
    # partially provisioned/legacy table without changing it unexpectedly.
    conn.execute("""
        DO $$
        BEGIN
            IF to_regclass('public.admin_audit_events') IS NOT NULL THEN
                ALTER TABLE admin_audit_events
                    DROP CONSTRAINT IF EXISTS admin_audit_events_event_type_check;
                ALTER TABLE admin_audit_events
                    DROP CONSTRAINT IF EXISTS admin_audit_events_object_type_check;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'admin_audit_events'::regclass
                      AND conname = 'admin_audit_events_event_type_check'
                ) THEN
                    ALTER TABLE admin_audit_events
                    ADD CONSTRAINT admin_audit_events_event_type_check
                    CHECK (event_type IN (
                        'metadata_update','source_settings_update','admin_operation_queued',
                        'schedule_created','schedule_enabled','performer_profile_update'
                    ));
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'admin_audit_events'::regclass
                      AND conname = 'admin_audit_events_object_type_check'
                ) THEN
                    ALTER TABLE admin_audit_events
                    ADD CONSTRAINT admin_audit_events_object_type_check
                    CHECK (object_type IN (
                        'video','source_configuration','operation_plan','schedule','performer'
                    ));
                END IF;
            END IF;
        END
        $$
    """)


migration = Migration(
    version=40,
    name="0040_performer_profile_overrides",
    description="Protect manual performer profile and alias changes from provider refreshes",
    apply_fn=apply,
)
