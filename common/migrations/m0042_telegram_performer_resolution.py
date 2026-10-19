"""Persist the evidence behind automatically extracted Telegram casts."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS performer_resolution JSONB")


migration = Migration(
    version=42,
    name="0042_telegram_performer_resolution",
    description="Store provenance for Telegram cast extraction decisions",
    apply_fn=apply,
)
