"""Persist external metadata suggestions that need operator review."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata_review_proposals (
            id BIGSERIAL PRIMARY KEY,
            video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            candidate_fingerprint TEXT NOT NULL,
            changes JSONB NOT NULL,
            source TEXT NOT NULL DEFAULT '',
            external_id TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
            expected_revision INTEGER NOT NULL DEFAULT 0,
            expected_source_fingerprint TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','accepted','rejected')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMPTZ,
            UNIQUE(video_id,candidate_fingerprint)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_metadata_review_pending ON metadata_review_proposals (created_at,id) WHERE status='pending'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_metadata_review_video ON metadata_review_proposals (video_id,status,created_at DESC)")


migration = Migration(
    version=36,
    name="0036_metadata_review_proposals",
    description="Persist reviewed candidates from external video metadata matching",
    apply_fn=apply,
)
