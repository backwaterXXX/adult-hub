"""Migration 0019: before/after audit events for Admin-managed changes."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit_events (
            id BIGSERIAL PRIMARY KEY,
            event_key TEXT NOT NULL UNIQUE,
            actor_type TEXT NOT NULL DEFAULT 'shared_admin'
                CHECK (actor_type = 'shared_admin'),
            event_type TEXT NOT NULL CHECK (event_type IN ('metadata_update','source_settings_update')),
            object_type TEXT NOT NULL CHECK (object_type IN ('video','source_configuration')),
            object_id TEXT NOT NULL,
            job_id TEXT REFERENCES video_duplicate_jobs(id) ON DELETE SET NULL,
            changes JSONB NOT NULL,
            truncated BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (jsonb_typeof(changes) = 'object')
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_events_created "
        "ON admin_audit_events (created_at DESC,id DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_events_object "
        "ON admin_audit_events (object_type,object_id,created_at DESC)"
    )


migration = Migration(
    version=19,
    name="0019_admin_change_events",
    description="Persist before/after changes for Admin-managed metadata and settings",
    apply_fn=apply,
)
