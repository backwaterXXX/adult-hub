"""Migration 0002: Video deduplication jobs, decisions, dismissals, and video sources."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_duplicate_jobs (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            state TEXT NOT NULL,
            payload TEXT NOT NULL,
            result TEXT,
            created_at BIGINT NOT NULL,
            updated_at BIGINT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vdj_queue ON video_duplicate_jobs (action, state, created_at)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_duplicate_decisions (
            loser_id TEXT PRIMARY KEY,
            keeper_id TEXT NOT NULL,
            operation_id TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            snapshot TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_duplicate_dismissals (
            left_id TEXT NOT NULL,
            right_id TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            operation_id TEXT NOT NULL,
            created_at BIGINT NOT NULL,
            PRIMARY KEY(left_id, right_id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_sources (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            video_id TEXT NOT NULL,
            source_id TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL,
            provider_ref TEXT,
            source_url TEXT,
            channel_id TEXT,
            priority INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1,
            created_at BIGINT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vs_video_id ON video_sources(video_id, is_active, priority)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vs_source_id ON video_sources(source_id)")


migration = Migration(
    version=2,
    name="0002_duplicate_jobs_and_sources",
    description="Durable deduplication queue, decisions, dismissals, and failover video sources",
    apply_fn=apply,
)
