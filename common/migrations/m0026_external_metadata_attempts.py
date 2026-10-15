"""Migration 0026: fair retry scheduling for external metadata lookups."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS external_metadata_attempts (
            entity_type TEXT NOT NULL CHECK (entity_type IN ('video','performer')),
            entity_key TEXT NOT NULL,
            source TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('found','no_match','error')),
            attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
            error_streak INTEGER NOT NULL DEFAULT 0 CHECK (error_streak >= 0),
            last_attempt_at TIMESTAMPTZ NOT NULL,
            next_attempt_at TIMESTAMPTZ NOT NULL,
            last_error_code TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (entity_type,entity_key,source)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_external_metadata_attempts_due
        ON external_metadata_attempts (entity_type,source,next_attempt_at,entity_key)
    """)


migration = Migration(
    version=26,
    name="0026_external_metadata_attempts",
    description="Track external metadata outcomes, refresh windows, and retry backoff",
    apply_fn=apply,
)
