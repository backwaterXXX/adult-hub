"""Migration 0009: index the channel label behind the library channel filter."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

# The client library filters on ``lower(btrim(channel_name))``. Without an index
# that is a sequential scan with an expression over every row (~28 000), paid on
# each catalog request; the expression index lets Postgres answer the equality
# from an index scan instead.
STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_videos_channel_name ON public.videos "
    "(lower(btrim(COALESCE(channel_name, ''))))",
)


def apply(conn: Any, **_kwargs) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)


migration = Migration(
    version=9,
    name="0009_video_channel_name_index",
    description="Index the channel label used by the library filter",
    apply_fn=apply,
)
