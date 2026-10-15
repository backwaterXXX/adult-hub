"""Migration 0004: Performer enrichment tables (PBC, TPDB, photos, Jellyfin sync)."""

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS jellyfin_person_sync (
            name TEXT PRIMARY KEY,
            pbc_updated_at BIGINT NOT NULL,
            format_version INTEGER NOT NULL DEFAULT 0
        )
    """)


migration = Migration(
    version=4,
    name="0004_performer_enrichment",
    description="Performer aliases, bios, photos, and Jellyfin person sync markers",
    apply_fn=apply,
)
