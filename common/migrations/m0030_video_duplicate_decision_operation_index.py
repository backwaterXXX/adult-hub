"""Index active duplicate decisions by operation for restore and undo paths."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_duplicate_decisions_operation_state "
        "ON video_duplicate_decisions (operation_id, state)"
    )


migration = Migration(
    version=30,
    name="0030_video_duplicate_decision_operation_index",
    description="Index duplicate decisions by operation and state for restore and undo",
    apply_fn=apply,
)
