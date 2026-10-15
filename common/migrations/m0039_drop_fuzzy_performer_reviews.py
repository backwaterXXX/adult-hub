"""Drop fuzzy performer reviews table."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("DROP TABLE IF EXISTS fuzzy_performer_reviews")


migration = Migration(
    version=39,
    name="0039_drop_fuzzy_performer_reviews",
    description="Drop fuzzy performer reviews table",
    apply_fn=apply,
)
