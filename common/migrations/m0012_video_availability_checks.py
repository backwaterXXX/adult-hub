"""Migration 0012: durable results from bounded playback availability checks."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_availability_checks (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES video_duplicate_jobs(id) ON DELETE CASCADE,
            video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK (status IN ('available','unavailable','transient_error','unknown')),
            checked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            evidence TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_video_availability_job_item "
                 "ON video_availability_checks (job_id, video_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_video_availability_latest "
                 "ON video_availability_checks (video_id, checked_at DESC, id DESC)")


migration = Migration(
    version=12,
    name="0012_video_availability_checks",
    description="Persist bounded, evidence-based playback availability checks",
    apply_fn=apply,
)
