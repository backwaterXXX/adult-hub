"""Migration 0045: retain a short, sanitized cause for failed metadata attempts."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("""
        ALTER TABLE external_metadata_attempts
        ADD COLUMN IF NOT EXISTS last_error_detail TEXT NOT NULL DEFAULT ''
    """)


migration = Migration(
    version=45,
    name="0045_external_metadata_error_detail",
    description="Persist sanitized exception details for external metadata failures",
    apply_fn=apply,
)
