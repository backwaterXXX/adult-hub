"""Permit automatic daily catalog health audit schedules."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("ALTER TABLE admin_schedules DROP CONSTRAINT IF EXISTS admin_schedules_action_check")
    conn.execute("""
        ALTER TABLE admin_schedules ADD CONSTRAINT admin_schedules_action_check
        CHECK (action IN ('availability_check','consistency_audit','metadata_quality_audit','catalog_full_audit','backup_create'))
    """)


migration = Migration(
    version=49,
    name="0049_catalog_automation",
    description="Allow daily metadata quality audits in maintenance schedules",
    apply_fn=apply,
)
