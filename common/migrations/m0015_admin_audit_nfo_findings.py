"""Migration 0015: classify malformed and stale local NFO sidecars."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("ALTER TABLE admin_audit_items DROP CONSTRAINT IF EXISTS admin_audit_items_kind_check")
    conn.execute("""
        ALTER TABLE admin_audit_items ADD CONSTRAINT admin_audit_items_kind_check
        CHECK (kind IN ('missing_stream','missing_nfo','invalid_nfo','nfo_metadata_mismatch',
                        'catalog_reference_without_video','unknown_mapping','orphan_file'))
    """)


migration = Migration(
    version=15,
    name="0015_admin_audit_nfo_findings",
    description="Allow durable audit findings for invalid or outdated NFO metadata",
    apply_fn=apply,
)
