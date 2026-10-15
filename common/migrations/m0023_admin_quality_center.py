"""Migration 0023: store safe field details for catalog quality findings."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("ALTER TABLE admin_audit_items ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb")
    conn.execute("ALTER TABLE admin_audit_items DROP CONSTRAINT IF EXISTS admin_audit_items_kind_check")
    conn.execute("""
        ALTER TABLE admin_audit_items ADD CONSTRAINT admin_audit_items_kind_check
        CHECK (kind IN ('missing_stream','missing_nfo','invalid_nfo','nfo_metadata_mismatch',
                        'catalog_reference_without_video','unknown_mapping','orphan_file',
                        'nfo_import_state_missing','nfo_import_state_mismatch','missing_artwork',
                        'missing_metadata'))
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_items_quality ON admin_audit_items (job_id,kind,status,id)")


migration = Migration(
    version=23,
    name="0023_admin_quality_center",
    description="Store field-level details and missing metadata findings for the quality center",
    apply_fn=apply,
)
