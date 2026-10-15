"""Keep provenance for hashes submitted by the TV player's validated decoder."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_phash_provenance (
            video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
            fingerprint TEXT NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{16}$'),
            generator_version TEXT NOT NULL,
            decoder_profile TEXT NOT NULL,
            submitted_by_user_id TEXT NOT NULL,
            submitted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


migration = Migration(
    version=28,
    name="0028_phash_provenance",
    description="Track validated client pHash generator and decoder provenance",
    apply_fn=apply,
)
