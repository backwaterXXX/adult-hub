"""Persist server-side pHash jobs and their retry/lease state."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_phash_jobs (
            id TEXT PRIMARY KEY,
            video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            algorithm_version TEXT NOT NULL,
            decoder_profile TEXT NOT NULL,
            duration_us BIGINT NOT NULL CHECK (duration_us BETWEEN 1 AND 86400000000),
            submitted_by_user_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('queued','processing','done','failed','cancelled')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            available_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            lease_token TEXT,
            lease_until TIMESTAMPTZ,
            fingerprint TEXT CHECK (fingerprint IS NULL OR fingerprint ~ '^[0-9a-f]{16}$'),
            bytes_read BIGINT CHECK (bytes_read IS NULL OR bytes_read >= 0),
            error_code TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMPTZ,
            UNIQUE (video_id, algorithm_version, decoder_profile),
            CHECK ((state = 'processing') = (lease_token IS NOT NULL AND lease_until IS NOT NULL))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS video_phash_jobs_ready_idx
        ON video_phash_jobs(available_at, created_at, id) WHERE state = 'queued'
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS video_phash_jobs_single_active_idx
        ON video_phash_jobs ((1)) WHERE state = 'processing'
    """)


migration = Migration(
    version=37,
    name="0037_video_phash_jobs",
    description="Persist pHash generation jobs, retries, and worker leases",
    apply_fn=apply,
)
