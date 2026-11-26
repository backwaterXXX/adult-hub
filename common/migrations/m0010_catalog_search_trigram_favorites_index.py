"""Migration 0010: trigram search indexes and a favorite lookup index."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

# The catalog search runs case-insensitive ``ILIKE '%term%'`` over title,
# studio and plot. Without trigram indexes that is a sequential scan of the whole
# catalog (~370 ms cold). GIN trigram indexes answer the same predicate in a few
# milliseconds. The favorite count/list also probes (user_id, video_id) per row;
# the composite index keeps that lookup cheap. The lower() expression indexes
# back the case-insensitive performer and category filters.
STATEMENTS = (
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX IF NOT EXISTS idx_videos_title_trgm "
    "ON public.videos USING gin (title gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_videos_studio_trgm "
    "ON public.videos USING gin (studio gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_videos_plot_trgm "
    "ON public.videos USING gin (plot gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_client_user_favorites_user_video "
    "ON public.client_user_favorites USING btree (user_id, video_id)",
    # Case-insensitive performer and category filters read the mirror tables;
    # without these expression indexes each request scans them.
    "CREATE INDEX IF NOT EXISTS idx_video_performers_name_lower "
    "ON public.video_performers (lower(performer_name), video_id)",
    "CREATE INDEX IF NOT EXISTS idx_video_categories_category_lower "
    "ON public.video_categories (lower(category), video_id)",
)


def apply(conn: Any, **_kwargs) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)


migration = Migration(
    version=10,
    name="0010_catalog_search_trigram_favorites_index",
    description="Trigram indexes for catalog search and a favorite lookup index",
    apply_fn=apply,
)
