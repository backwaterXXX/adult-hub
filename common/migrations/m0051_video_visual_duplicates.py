"""Persist video frame signatures, visual comparisons, and their work queue."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_visual_signatures (
            video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
            algorithm_version TEXT NOT NULL,
            source_key TEXT NOT NULL,
            duration_seconds DOUBLE PRECISION NOT NULL,
            signature JSONB NOT NULL,
            contact_sheet BYTEA,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS video_visual_signatures_duration_idx "
                 "ON video_visual_signatures (duration_seconds, video_id)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_visual_comparisons (
            id TEXT PRIMARY KEY,
            left_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            right_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            algorithm_version TEXT NOT NULL,
            left_source_key TEXT NOT NULL,
            right_source_key TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('queued','processing','confirmed','review','different','inconclusive','failed')),
            evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
            frame_sheet BYTEA,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at BIGINT,
            CHECK (left_id < right_id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS video_visual_comparisons_state_idx "
                 "ON video_visual_comparisons (state, updated_at DESC)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_visual_jobs (
            id TEXT PRIMARY KEY,
            dedupe_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL CHECK (kind IN ('signature','verify_pair')),
            video_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
            left_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
            right_id TEXT REFERENCES videos(id) ON DELETE CASCADE,
            source_key TEXT NOT NULL DEFAULT '',
            right_source_key TEXT NOT NULL DEFAULT '',
            algorithm_version TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('queued','processing','done','failed','cancelled')),
            priority SMALLINT NOT NULL DEFAULT 20 CHECK (priority BETWEEN 0 AND 100),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            available_at BIGINT NOT NULL DEFAULT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP)::BIGINT,
            lease_token TEXT,
            lease_until BIGINT,
            error_code TEXT,
            error_detail TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at BIGINT,
            CHECK ((kind='signature' AND video_id IS NOT NULL AND left_id IS NULL AND right_id IS NULL)
                OR (kind='verify_pair' AND video_id IS NULL AND left_id IS NOT NULL AND right_id IS NOT NULL AND left_id < right_id)),
            CHECK ((state='processing') = (lease_token IS NOT NULL AND lease_until IS NOT NULL))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS video_visual_jobs_ready_idx "
                 "ON video_visual_jobs (available_at, created_at, id) WHERE state='queued'")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS video_visual_jobs_single_active_idx "
                 "ON video_visual_jobs ((1)) WHERE state='processing'")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_visual_state (
            id SMALLINT PRIMARY KEY CHECK (id=1),
            algorithm_version TEXT NOT NULL,
            report_started_at BIGINT NOT NULL,
            automatic_enabled_at BIGINT,
            pair_cursor BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


def _migration(version: int, name: str) -> Migration:
    return Migration(version, name, "Add bounded visual duplicate comparisons", apply)


migration = _migration(51, "0051_video_visual_duplicates")
current_migration = _migration(3, "0003_video_visual_duplicates")
