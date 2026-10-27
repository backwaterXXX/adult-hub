"""Align visual queue timestamps with the application's timestamptz adapter."""
from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    for table in ("video_visual_signatures", "video_visual_comparisons", "video_visual_jobs", "video_visual_state"):
        for column in ("created_at", "updated_at"):
            present = conn.execute("""SELECT data_type FROM information_schema.columns
                WHERE table_schema=current_schema() AND table_name=%s AND column_name=%s""",
                (table, column)).fetchone()
            if not present or present[0] == "timestamp with time zone":
                continue
            conn.execute(f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT")
            conn.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE TIMESTAMPTZ USING to_timestamp({column})")
            conn.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT CURRENT_TIMESTAMP")


migration = Migration(52, "0052_video_visual_timestamps", "Align visual queue timestamps", apply)
current_migration = Migration(4, "0004_video_visual_timestamps", "Align visual queue timestamps", apply)
