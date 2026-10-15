"""Migration 0014: durable scheduled Admin maintenance actions."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_schedules (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            action TEXT NOT NULL CHECK (action IN ('availability_check','consistency_audit','backup_create')),
            payload TEXT NOT NULL DEFAULT '{}',
            schedule_type TEXT NOT NULL CHECK (schedule_type IN ('interval','daily')),
            interval_minutes INTEGER,
            daily_at TEXT,
            timezone TEXT NOT NULL DEFAULT 'Europe/Warsaw' CHECK (timezone = 'Europe/Warsaw'),
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            revision INTEGER NOT NULL DEFAULT 1,
            next_run_at TIMESTAMPTZ,
            last_run_at TIMESTAMPTZ,
            last_job_id TEXT,
            last_result TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK ((schedule_type='interval' AND interval_minutes BETWEEN 15 AND 43200 AND daily_at IS NULL)
                OR (schedule_type='daily' AND interval_minutes IS NULL AND daily_at ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_schedules_due ON admin_schedules (next_run_at) WHERE enabled=TRUE")


migration = Migration(
    version=14,
    name="0014_admin_schedules",
    description="Persist local-time maintenance schedules with restart-safe dispatch state",
    apply_fn=apply,
)
