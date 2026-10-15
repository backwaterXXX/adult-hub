"""Migration 0005: Stream cache and playback diagnostics."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stream_cache (
            cache_key TEXT PRIMARY KEY,
            stream_url TEXT NOT NULL,
            expire_at DOUBLE PRECISION NOT NULL,
            last_accessed DOUBLE PRECISION NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stream_cache_expire ON stream_cache(expire_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stream_cache_lru ON stream_cache(last_accessed)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS diagnostic_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS diagnostic_reports (
            report_id TEXT PRIMARY KEY,
            ended_at DOUBLE PRECISION NOT NULL,
            report_json TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_diagnostic_reports_ended ON diagnostic_reports(ended_at DESC)")


migration = Migration(
    version=5,
    name="0005_stream_cache_and_diagnostics",
    description="Persistent stream cache and playback diagnostics settings and reports",
    apply_fn=apply,
)
