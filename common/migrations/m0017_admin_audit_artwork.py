"""Migration 0017: allow missing local artwork audit findings."""

from __future__ import annotations

from typing import Any
from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("ALTER TABLE admin_audit_items DROP CONSTRAINT IF EXISTS admin_audit_items_kind_check")
    conn.execute("""
        ALTER TABLE admin_audit_items ADD CONSTRAINT admin_audit_items_kind_check
        CHECK (kind IN ('missing_stream','missing_nfo','invalid_nfo','nfo_metadata_mismatch',
                        'catalog_reference_without_video','unknown_mapping','orphan_file',
                        'nfo_import_state_missing','nfo_import_state_mismatch','missing_artwork'))
    """)


migration = Migration(
    version=17,
    name="0017_admin_audit_artwork",
    description="Allow durable audit findings for missing local artwork with a source thumbnail",
    apply_fn=apply,
)
