"""Migration 0025: persist reversible quality finding acknowledgements."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_quality_acknowledgements (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            video_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('missing_metadata','missing_nfo','invalid_nfo',
                                                'nfo_metadata_mismatch','missing_artwork')),
            finding_fingerprint TEXT NOT NULL CHECK (finding_fingerprint ~ '^[0-9a-f]{32}$'),
            source_audit_id TEXT NOT NULL,
            source_finding_id BIGINT NOT NULL,
            note TEXT NOT NULL CHECK (length(note) BETWEEN 1 AND 500),
            state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','reopened')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(video_id,kind,finding_fingerprint)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_quality_ack_video ON admin_quality_acknowledgements (video_id,kind,updated_at DESC)")


migration = Migration(
    version=25,
    name="0025_admin_quality_acknowledgements",
    description="Persist reversible acknowledgements for recurring quality findings",
    apply_fn=apply,
)
