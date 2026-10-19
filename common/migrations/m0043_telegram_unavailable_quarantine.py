"""Track reversible Telegram message availability quarantine."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_unavailable_quarantine (
            video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
            channel_id TEXT NOT NULL,
            message_id BIGINT NOT NULL,
            file_unique_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('suspect','quarantined','failed')),
            miss_count INTEGER NOT NULL DEFAULT 1,
            last_checked_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            files JSONB NOT NULL DEFAULT '[]'::jsonb,
            last_error TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_telegram_unavailable_channel_check
        ON telegram_unavailable_quarantine(channel_id, last_checked_at)
    """)


migration = Migration(
    version=43,
    name="0043_telegram_unavailable_quarantine",
    description="Track reversible quarantine for missing Telegram messages",
    apply_fn=apply,
)
