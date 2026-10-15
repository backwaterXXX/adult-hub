"""Migration 0013: durable, paginated Admin consistency audit findings."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_audit_items (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES video_duplicate_jobs(id) ON DELETE CASCADE,
            video_id TEXT,
            kind TEXT NOT NULL CHECK (kind IN ('missing_stream','missing_nfo','invalid_nfo','nfo_metadata_mismatch','catalog_reference_without_video','unknown_mapping','orphan_file')),
            status TEXT NOT NULL CHECK (status IN ('open','resolved')),
            evidence TEXT NOT NULL DEFAULT '',
            checked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_items_job ON admin_audit_items (job_id,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_items_kind ON admin_audit_items (kind,status,checked_at DESC)")


migration = Migration(
    version=13,
    name="0013_admin_audit_items",
    description="Persist read-only library consistency audit findings",
    apply_fn=apply,
)
