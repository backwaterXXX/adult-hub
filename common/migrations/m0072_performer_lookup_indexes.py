"""Index the performer lookups used by the catalogue performer listing."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs: Any) -> None:
    """Speed up the name and alias lookups in ``query_catalog_performers``.

    The listing joins every performer element of every visible video against the
    catalogue by ``lower(name)`` and ``lower(alias)``. Without matching indexes
    each element scanned the whole catalogue, so one page took minutes.
    """
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_performer_catalog_lower_name "
        "ON public.performer_catalog USING btree (lower(name))"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_performer_alias_lower_alias "
        "ON public.performer_catalog_aliases USING btree (lower(alias)) WHERE approved"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_performer_photos_lower_name "
        "ON public.performer_photos USING btree (lower(name))"
    )


migration = Migration(
    72,
    "0072_performer_lookup_indexes",
    "Index lower(name) and lower(alias) for the catalogue performer listing",
    apply,
)
current_migration = Migration(
    24,
    "0024_performer_lookup_indexes",
    "Index lower(name) and lower(alias) for the catalogue performer listing",
    apply,
)
