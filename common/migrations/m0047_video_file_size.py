"""Durable per-video file size for Telegram documents.

Filled from media metadata already fetched by OSHASH probes (Content-Range
totals); no full downloads are performed for sizing.
"""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        ALTER TABLE videos
            ADD COLUMN IF NOT EXISTS file_size_bytes BIGINT
                CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0)
    """)
    conn.execute("""
        UPDATE videos v
        SET file_size_bytes = size.value
        FROM (
            SELECT f.video_id,
                   MAX((f.media_probe_data->>'file_size_bytes')::BIGINT) AS value
            FROM video_fingerprints f
            WHERE f.media_probe_data ? 'file_size_bytes'
              AND (f.media_probe_data->>'file_size_bytes') ~ '^[0-9]+$'
            GROUP BY f.video_id
        ) size
        WHERE v.id = size.video_id AND v.file_size_bytes IS NULL
          AND size.value >= 0
    """)


migration = Migration(
    version=47,
    name="0047_video_file_size",
    description="Add videos.file_size_bytes and backfill from OSHASH media probes",
    apply_fn=apply,
)
