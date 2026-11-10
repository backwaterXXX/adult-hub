"""Legacy pre-baseline step 1: initial core schema for videos, files, categories, and streams.

This is not the fresh-install baseline (`m0001_baseline.py`, version 1). It is
used only by `legacy.py` as the first step of the pre-baseline chain, and it
runs only for databases whose recorded history predates the baseline.
"""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,
            provider TEXT,
            title TEXT,
            slug TEXT,
            duration INTEGER,
            thumbnail TEXT,
            performers TEXT,
            categories TEXT,
            tags TEXT,
            embed_host TEXT,
            playable INTEGER,
            stream_checked BIGINT,
            stream_fails INTEGER DEFAULT 0,
            studio TEXT,
            performers_checked_at BIGINT,
            performers_audited_at BIGINT,
            plot TEXT,
            date_released TEXT,
            scene_matched_at BIGINT,
            first_seen BIGINT,
            last_seen BIGINT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS dead_streams (
            video_id TEXT PRIMARY KEY,
            reason TEXT,
            detected_at BIGINT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS synced_files (
            file_path TEXT PRIMARY KEY,
            video_id TEXT,
            category TEXT,
            created_at BIGINT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_categories (
            video_id TEXT,
            category TEXT,
            PRIMARY KEY (video_id, category)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Performance indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_provider ON videos(provider)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_last_seen ON videos(last_seen)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sf_video_id ON synced_files(video_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vc_category ON video_categories(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vc_video_id ON video_categories(video_id)")


migration = Migration(
    version=1,
    # Stored in schema_migrations for legacy databases. Do not change it.
    name="0001_initial_schema",
    description="Core metadata tables: videos, dead_streams, synced_files, video_categories, meta",
    apply_fn=apply,
)
