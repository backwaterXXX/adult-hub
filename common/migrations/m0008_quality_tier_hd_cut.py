"""Migration 0008: 720p counts as HD in the quality tier."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

# Migration 0007 put the HD cut at 1000 pixels, which labelled every 1280x720
# upload as standard definition unless its bitrate happened to be high. 720p is
# HD by definition and it matches the rule the clients used before the tiers
# existed (height >= 720), so the generated column is rebuilt with a 700 pixel
# cut. Dropping the column also drops its index, so the index is recreated.
BITRATE_KBPS = "round(file_size_bytes * 8.0 / duration / 1000)::integer"
QUALITY_TIER_EXPRESSION = (
    "CASE "
    f"WHEN height >= 2000 OR (duration > 0 AND file_size_bytes > 0 AND {BITRATE_KBPS} >= 10000) THEN 'high'::text "
    f"WHEN height >= 700 OR (duration > 0 AND file_size_bytes > 0 AND {BITRATE_KBPS} >= 2500) THEN 'medium'::text "
    "WHEN height IS NOT NULL OR (duration > 0 AND file_size_bytes > 0) THEN 'low'::text "
    "ELSE NULL::text END"
)

STATEMENTS = (
    "ALTER TABLE public.videos DROP COLUMN IF EXISTS quality_tier",
    "ALTER TABLE public.videos ADD COLUMN IF NOT EXISTS quality_tier text "
    f"GENERATED ALWAYS AS ({QUALITY_TIER_EXPRESSION}) STORED",
    "CREATE INDEX IF NOT EXISTS idx_videos_quality_tier ON public.videos (quality_tier) "
    "WHERE quality_tier IS NOT NULL",
)


def apply(conn: Any, **_kwargs) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)


migration = Migration(
    version=8,
    name="0008_quality_tier_hd_cut",
    description="Treat 720p as HD in the quality tier",
    apply_fn=apply,
)
