"""Migration 0004: Performer enrichment tables (PBC, TPDB, photos)."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS performer_aliases (
            alias TEXT PRIMARY KEY,
            canonical TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS performer_bios (
            name TEXT PRIMARY KEY,
            bio TEXT DEFAULT '',
            birth TEXT DEFAULT '',
            aliases TEXT DEFAULT '[]',
            updated_at BIGINT,
            nationality TEXT DEFAULT ''
        )
    """)
    conn.execute("ALTER TABLE performer_bios ADD COLUMN IF NOT EXISTS nationality TEXT DEFAULT ''")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS performer_photos (
            name TEXT PRIMARY KEY,
            thumb TEXT DEFAULT '',
            updated_at BIGINT
        )
    """)


migration = Migration(
    version=4,
    name="0004_performer_enrichment",
    description="Performer aliases, bios, photos",
    apply_fn=apply,
)
