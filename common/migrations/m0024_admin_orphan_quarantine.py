"""Migration 0024: durable reversible quarantine for orphan file families."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_orphan_quarantine (
            operation_id TEXT PRIMARY KEY,
            audit_id TEXT NOT NULL REFERENCES video_duplicate_jobs(id) ON DELETE RESTRICT,
            job_id TEXT REFERENCES video_duplicate_jobs(id) ON DELETE SET NULL,
            state TEXT NOT NULL CHECK (state IN ('moving','active','restoring','restored','failed')),
            files JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_orphan_quarantine_state ON admin_orphan_quarantine (state,updated_at DESC)")


migration = Migration(
    version=24,
    name="0024_admin_orphan_quarantine",
    description="Persist reversible orphan file quarantine operations",
    apply_fn=apply,
)
