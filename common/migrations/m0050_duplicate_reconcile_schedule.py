"""Enable the built-in recurring duplicate reconciliation schedule."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("ALTER TABLE admin_schedules DROP CONSTRAINT IF EXISTS admin_schedules_action_check")
    conn.execute("""
        ALTER TABLE admin_schedules ADD CONSTRAINT admin_schedules_action_check
        CHECK (action IN ('availability_check','consistency_audit','metadata_quality_audit',
                          'catalog_full_audit','backup_create','duplicate_reconcile'))
    """)


def _migration(version: int, name: str) -> Migration:
    return Migration(
        version=version,
        name=name,
        description="Allow scheduled automatic duplicate reconciliation",
        apply_fn=apply,
    )


migration = _migration(
    version=50,
    name="0050_duplicate_reconcile_schedule",
)
current_migration = _migration(version=2, name="0002_duplicate_reconcile_schedule")
