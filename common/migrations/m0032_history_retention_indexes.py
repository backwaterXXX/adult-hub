"""Indexes for bounded deletion of expired operational history."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_duplicate_jobs_history_retention "
        "ON video_duplicate_jobs (created_at,id) "
        "WHERE state IN ('done','failed','cancelled')"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_duplicate_decisions_history_retention "
        "ON video_duplicate_decisions (created_at,loser_id) WHERE state='undone'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_duplicate_dismissals_history_retention "
        "ON video_duplicate_dismissals (created_at,left_id,right_id) WHERE active=0"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_orphan_quarantine_history_retention "
        "ON admin_orphan_quarantine (created_at,operation_id) "
        "WHERE state='restored'"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_external_metadata_attempts_history_retention "
        "ON external_metadata_attempts (updated_at,entity_type,entity_key,source)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_availability_history_retention "
        "ON video_availability_checks (checked_at,id)"
    )


migration = Migration(
    version=32,
    name="0032_history_retention_indexes",
    description="Index expired operational history for bounded monthly cleanup",
    apply_fn=apply,
)
