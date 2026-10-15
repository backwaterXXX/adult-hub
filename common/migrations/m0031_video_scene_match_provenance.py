"""Persist provenance for the latest accepted scene metadata match."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS scene_match_source TEXT")
    conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS scene_match_external_id TEXT")
    conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS scene_match_url TEXT")
    conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS scene_match_confidence DOUBLE PRECISION")


migration = Migration(
    version=31,
    name="0031_video_scene_match_provenance",
    description="Persist source, reference, and confidence for scene metadata matches",
    apply_fn=apply,
)
