"""Migration 0003: materialised performer list for the client catalog."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

# The performer list unnested the performer array of every visible video on each
# request: 838 ms with JIT on a 27k catalog, 341 ms without it, plus the same
# aggregation again for the page. One row per canonical performer turns that
# into a small table read (6.7k rows in production).
STATEMENTS = (
    "CREATE TABLE IF NOT EXISTS public.performer_stats ("
    "canonical_id text PRIMARY KEY,"
    "canonical_name text NOT NULL,"
    "search_names text[] NOT NULL DEFAULT '{}',"
    "video_count integer NOT NULL,"
    "has_photo boolean NOT NULL DEFAULT false"
    ")",
    "CREATE INDEX IF NOT EXISTS idx_performer_stats_name "
    "ON public.performer_stats USING btree (canonical_name)",
    "CREATE INDEX IF NOT EXISTS idx_performer_stats_video_count "
    "ON public.performer_stats USING btree (video_count DESC, canonical_name, canonical_id)",
    "CREATE TABLE IF NOT EXISTS public.performer_stats_meta ("
    "id boolean PRIMARY KEY DEFAULT true CHECK (id),"
    "config_fingerprint text NOT NULL,"
    "refreshed_at timestamptz NOT NULL DEFAULT now(),"
    "row_count integer NOT NULL DEFAULT 0"
    ")",
)


def apply(conn: Any, **_kwargs) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)
    # The table is filled by the first client read (or the next sync pass), not
    # here: the migration runner owns this transaction and the refresh commits.


migration = Migration(
    version=3,
    name="0003_performer_stats",
    description="Materialised performer list for the client catalog",
    apply_fn=apply,
)
