"""Store the provider cover image for a catalogue performer."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs: Any) -> None:
    """Add ``image_url`` so the catalogue listing can show performer artwork.

    The catalogue sync only kept names and aliases, so a listed performer
    without a photo row had no artwork source at all. StashDB returns
    ``images { url }`` for every performer; keeping the first URL lets the
    artwork downloader materialise it locally.
    """
    conn.execute(
        "ALTER TABLE public.performer_catalog "
        "ADD COLUMN IF NOT EXISTS image_url text NOT NULL DEFAULT ''"
    )


migration = Migration(
    73,
    "0073_performer_catalog_image",
    "Keep the provider cover image URL on catalogue performers",
    apply,
)
current_migration = Migration(
    25,
    "0025_performer_catalog_image",
    "Keep the provider cover image URL on catalogue performers",
    apply,
)
