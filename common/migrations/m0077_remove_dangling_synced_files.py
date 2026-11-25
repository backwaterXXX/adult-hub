"""Remove library file records whose catalog item no longer exists.

``synced_files.video_id`` carries no foreign key to ``videos``, so a catalog item
deleted by a path that missed its file rows leaves references nobody can resolve.
The sync of 2026-11-02 left 91 such rows: every audit then reports them as
``catalog_reference_without_video`` and again as ``missing_stream`` for a video
that does not exist.
"""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs: Any) -> None:
    conn.execute(
        "DELETE FROM public.synced_files sf "
        "WHERE sf.video_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM public.videos v WHERE v.id = sf.video_id)"
    )


migration = Migration(
    77,
    "0077_remove_dangling_synced_files",
    "Delete library file records whose catalog item no longer exists",
    apply,
)
