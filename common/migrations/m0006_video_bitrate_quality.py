"""Migration 0006: the average bitrate behind the library quality tiers."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

# The "HD only" library filter ran in the clients off ``file_size_bytes >= 300
# MiB`` because the catalogue carried no resolution and no bitrate, so it kept
# 84 % of the library and meant nothing. The sync already records the two inputs
# of the average bitrate (the Telegram file size and the duration), so the value
# itself belongs in the database: a generated column puts every reader on the
# same formula, and a partial index turns the SD/HD/HQ filter into a range scan
# instead of an expression over all ~28 000 rows on every request.
BITRATE_EXPRESSION = (
    "CASE WHEN duration > 0 AND file_size_bytes > 0 "
    "THEN round(file_size_bytes * 8.0 / duration / 1000)::integer END"
)

STATEMENTS = (
    "ALTER TABLE public.videos ADD COLUMN IF NOT EXISTS bitrate_kbps integer "
    f"GENERATED ALWAYS AS ({BITRATE_EXPRESSION}) STORED",
    "CREATE INDEX IF NOT EXISTS idx_videos_bitrate ON public.videos (bitrate_kbps) "
    "WHERE bitrate_kbps IS NOT NULL",
)


def apply(conn: Any, **_kwargs) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)


migration = Migration(
    version=6,
    name="0006_video_bitrate_quality",
    description="Store the average bitrate for the quality tiers",
    apply_fn=apply,
)
