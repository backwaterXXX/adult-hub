"""Migration 0003: Telegram channel state and filter tracking."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_channel_state (
            channel_id TEXT PRIMARY KEY,
            last_min_duration INTEGER,
            backfill_in_progress INTEGER DEFAULT 0,
            backfill_cursor BIGINT,
            backfill_completed_at BIGINT,
            updated_at BIGINT
        )
    """)
    for column in ("last_tags_whitelist", "last_tags_blacklist"):
        conn.execute(f"ALTER TABLE telegram_channel_state ADD COLUMN IF NOT EXISTS {column} TEXT")


migration = Migration(
    version=3,
    name="0003_telegram_channel_state",
    description="Telegram channel sync state, cursor, and tag filter settings",
    apply_fn=apply,
)
