"""Migration 0007: Telegram resolution and the resolution-aware quality tier."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

# The bitrate tier of migration 0006 works with the data the catalogue has, but
# it cannot tell a 4K master from a high bitrate 1080p file: only the resolution
# can. Telegram reports it in the video attribute of every document, which the
# sync now stores. The tier moves into its own generated column so the label and
# the filter read the same value: a row is high when it is 4K wide *or* high
# bitrate, medium when it is HD *or* mid bitrate, low otherwise. Keeping the OR
# means no row that the bitrate already called HQ falls back to HD.
BITRATE_KBPS = (
    "round(file_size_bytes * 8.0 / duration / 1000)::integer"
)
QUALITY_TIER_EXPRESSION = (
    "CASE "
    f"WHEN height >= 2000 OR (duration > 0 AND file_size_bytes > 0 AND {BITRATE_KBPS} >= 10000) THEN 'high'::text "
    f"WHEN height >= 1000 OR (duration > 0 AND file_size_bytes > 0 AND {BITRATE_KBPS} >= 2500) THEN 'medium'::text "
    "WHEN height IS NOT NULL OR (duration > 0 AND file_size_bytes > 0) THEN 'low'::text "
    "ELSE NULL::text END"
)

STATEMENTS = (
    "ALTER TABLE public.videos ADD COLUMN IF NOT EXISTS width integer",
    "ALTER TABLE public.videos ADD COLUMN IF NOT EXISTS height integer",
    "ALTER TABLE public.videos ADD COLUMN IF NOT EXISTS quality_tier text "
    f"GENERATED ALWAYS AS ({QUALITY_TIER_EXPRESSION}) STORED",
    "CREATE INDEX IF NOT EXISTS idx_videos_quality_tier ON public.videos (quality_tier) "
    "WHERE quality_tier IS NOT NULL",
)


def apply(conn: Any, **_kwargs) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)


migration = Migration(
    version=7,
    name="0007_video_resolution_quality_tier",
    description="Store the Telegram resolution and the resolution-aware tier",
    apply_fn=apply,
)
