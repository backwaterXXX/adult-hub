"""Track cumulative pHash processing time across attempts."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        ALTER TABLE video_phash_jobs
        ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ
    """)
    conn.execute("""
        ALTER TABLE video_phash_jobs
        ADD COLUMN IF NOT EXISTS processing_duration_ms BIGINT NOT NULL DEFAULT 0
        CHECK (processing_duration_ms >= 0)
    """)
    # Jobs running during the upgrade start their visible timer at rollout.
    conn.execute("""
        UPDATE video_phash_jobs
        SET processing_started_at = CURRENT_TIMESTAMP
        WHERE state = 'processing' AND processing_started_at IS NULL
    """)


migration = Migration(
    version=38,
    name="0038_video_phash_job_processing_time",
    description="Track cumulative processing time for pHash jobs",
    apply_fn=apply,
)
