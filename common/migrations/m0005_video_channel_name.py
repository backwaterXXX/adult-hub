"""Migration 0005: the Telegram channel label gets its own column."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

# ``studio`` accumulated channel labels ("Lukayan SHOW", "D - PREMIUM") next to
# real production studios ("BelAmi", "Men.com"), so studio filters mixed two
# different things. The channel label now has its own column; the sync pass moves
# existing channel labels out of studio and fills this column from the config.
STATEMENTS = (
    "ALTER TABLE public.videos ADD COLUMN IF NOT EXISTS channel_name text",
)


def apply(conn: Any, **_kwargs) -> None:
    for statement in STATEMENTS:
        conn.execute(statement)


migration = Migration(
    version=5,
    name="0005_video_channel_name",
    description="Separate the Telegram channel label from the studio",
    apply_fn=apply,
)
