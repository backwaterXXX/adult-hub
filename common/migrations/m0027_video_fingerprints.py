"""Migration 0027: cache per-video fingerprints for repeatable metadata lookups."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_fingerprints (
            video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            algorithm TEXT NOT NULL CHECK (algorithm IN ('OSHASH','PHASH')),
            fingerprint TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{16}$'),
            computed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (video_id, algorithm)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_video_fingerprints_algorithm
        ON video_fingerprints (algorithm, fingerprint)
    """)


migration = Migration(
    version=27,
    name="0027_video_fingerprints",
    description="Persist video fingerprints to avoid repeated source reads",
    apply_fn=apply,
)
