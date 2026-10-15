"""Add persistent, versioned per-video metadata overrides."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_metadata_overrides (
            video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
            fields JSONB NOT NULL DEFAULT '{}'::jsonb,
            revision BIGINT NOT NULL DEFAULT 1 CHECK (revision > 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_video_metadata_overrides_updated ON video_metadata_overrides (updated_at DESC)")


migration = Migration(
    version=11,
    name="0011_video_metadata_overrides",
    description="Persist explicit, revisioned video metadata overrides",
    apply_fn=apply,
)
