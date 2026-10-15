"""Persist media metadata discovered from OSHASH's existing byte ranges."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        ALTER TABLE video_fingerprints
            ADD COLUMN IF NOT EXISTS media_probe_status TEXT
                CHECK (media_probe_status IS NULL OR media_probe_status IN ('complete','partial','unavailable')),
            ADD COLUMN IF NOT EXISTS media_probe_bytes INTEGER
                CHECK (media_probe_bytes IS NULL OR media_probe_bytes >= 0),
            ADD COLUMN IF NOT EXISTS media_probe_data JSONB,
            ADD COLUMN IF NOT EXISTS media_probe_updated_at TIMESTAMPTZ
    """)


migration = Migration(
    version=29,
    name="0029_oshash_media_probe",
    description="Store media details found in OSHASH byte ranges",
    apply_fn=apply,
)
