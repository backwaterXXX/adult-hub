"""Persist human review labels for fuzzy performer name pairs."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fuzzy_performer_reviews (
            review_id TEXT PRIMARY KEY,
            name_a TEXT NOT NULL,
            name_b TEXT NOT NULL,
            same_person BOOLEAN NOT NULL,
            preferred_name TEXT NOT NULL DEFAULT '',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


migration = Migration(
    version=35,
    name="0035_fuzzy_performer_reviews",
    description="Persist human labels for fuzzy performer pair reviews",
    apply_fn=apply,
)
