"""Persist client-extracted pHash frames as fallback artwork."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_phash_thumbnails (
            video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
            image_bytes BYTEA NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


migration = Migration(
    version=34,
    name="0034_video_phash_thumbnails",
    description="Store a sampled pHash frame for low-quality or missing video artwork",
    apply_fn=apply,
)
