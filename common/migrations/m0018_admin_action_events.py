"""Migration 0018: privacy-preserving Admin API activity events."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_action_events (
            id BIGSERIAL PRIMARY KEY,
            actor_type TEXT NOT NULL DEFAULT 'shared_admin'
                CHECK (actor_type = 'shared_admin'),
            method TEXT NOT NULL CHECK (method IN ('POST','PUT','PATCH','DELETE')),
            route TEXT NOT NULL CHECK (length(route) BETWEEN 1 AND 256),
            status_code INTEGER NOT NULL CHECK (status_code BETWEEN 100 AND 599),
            outcome TEXT NOT NULL CHECK (outcome IN ('completed','accepted')),
            correlation_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (correlation_id IS NULL OR correlation_id ~ '^[a-f0-9]{32}$')
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_admin_action_events_created "
        "ON admin_action_events (created_at DESC, id DESC)"
    )


migration = Migration(
    version=18,
    name="0018_admin_action_events",
    description="Record privacy-preserving outcomes of Admin v2 mutation requests",
    apply_fn=apply,
)
