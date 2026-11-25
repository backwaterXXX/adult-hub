"""Drop the Jellyfin person-sync markers.

The integration is gone: nothing reads or writes ``jellyfin_person_sync`` any
more, and the table only held per-performer bookkeeping for the retired
media-server push. Migration 0004 no longer creates it, so this only has to
clean up databases that were migrated before the removal.
"""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs: Any) -> None:
    conn.execute("DROP TABLE IF EXISTS public.jellyfin_person_sync")


migration = Migration(
    74,
    "0074_drop_jellyfin_person_sync",
    "Drop the retired Jellyfin person-sync markers",
    apply,
)
