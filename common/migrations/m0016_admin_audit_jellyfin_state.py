"""Migration 0016: allow Jellyfin import-state checksum audit findings."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("ALTER TABLE admin_audit_items DROP CONSTRAINT IF EXISTS admin_audit_items_kind_check")
    conn.execute("""
        ALTER TABLE admin_audit_items ADD CONSTRAINT admin_audit_items_kind_check
        CHECK (kind IN ('missing_stream','missing_nfo','invalid_nfo','nfo_metadata_mismatch',
                        'catalog_reference_without_video','unknown_mapping','orphan_file',
                        'nfo_import_state_missing','nfo_import_state_mismatch'))
    """)


migration = Migration(
    version=16,
    name="0016_admin_audit_jellyfin_state",
    description="Allow durable missing or stale Jellyfin NFO import audit findings",
    apply_fn=apply,
)
