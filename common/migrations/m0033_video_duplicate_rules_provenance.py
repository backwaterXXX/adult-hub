"""Store the duplicate rule version used to create an accepted hide decision."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute(
        "ALTER TABLE video_duplicate_decisions "
        "ADD COLUMN IF NOT EXISTS rules_version INTEGER"
    )


migration = Migration(
    version=33,
    name="0033_video_duplicate_rules_provenance",
    description="Record duplicate scoring rules version for accepted candidate decisions",
    apply_fn=apply,
)
