"""Migration 0022: retain before/after cast lists for reversible Telegram rebuilds."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_performer_rebuild_backup (
            run_id UUID NOT NULL,
            video_id TEXT NOT NULL,
            old_performers JSONB NOT NULL,
            new_performers JSONB NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(run_id,video_id)
        )
    """)


migration = Migration(
    version=22,
    name="0022_telegram_performer_rebuild_backup",
    description="Store before/after performer lists so bulk rebuilds can be rolled back",
    apply_fn=apply,
)
