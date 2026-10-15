"""Stable external performer identities, alias variants, proposals and Telegram source text."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS performer_catalog (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            name TEXT NOT NULL,
            updated_at TIMESTAMPTZ,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            UNIQUE(source, source_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS performer_catalog_aliases (
            id BIGSERIAL PRIMARY KEY,
            performer_id TEXT NOT NULL REFERENCES performer_catalog(id) ON DELETE CASCADE,
            alias TEXT NOT NULL,
            normalized TEXT NOT NULL,
            origin TEXT NOT NULL,
            approved BOOLEAN NOT NULL DEFAULT TRUE,
            UNIQUE(performer_id, normalized, origin)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_performer_alias_normalized ON performer_catalog_aliases(normalized)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_performer_proposals (
            id BIGSERIAL PRIMARY KEY,
            video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            candidate TEXT NOT NULL,
            normalized TEXT NOT NULL,
            matched_text TEXT NOT NULL DEFAULT '',
            source_field TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
            performer_id TEXT REFERENCES performer_catalog(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(video_id, normalized)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_telegram_performer_proposals_status ON telegram_performer_proposals(status, created_at)")
    conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS source_text TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS source_filename TEXT NOT NULL DEFAULT ''")
    conn.execute("""
        UPDATE videos SET source_text=COALESCE(plot,'')
        WHERE provider='telegram' AND source_text='' AND COALESCE(plot,'')<>''
    """)


migration = Migration(
    version=21,
    name="0021_performer_catalog",
    description="Stable performer identities, approved aliases and Telegram source fields",
    apply_fn=apply,
)
