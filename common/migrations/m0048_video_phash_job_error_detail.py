"""Keep bounded diagnostics for failed pHash generation attempts."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        ALTER TABLE video_phash_jobs
        ADD COLUMN IF NOT EXISTS error_detail TEXT NOT NULL DEFAULT ''
    """)


migration = Migration(
    version=48,
    name="0048_video_phash_job_error_detail",
    description="Persist bounded pHash worker failure diagnostics",
    apply_fn=apply,
)
