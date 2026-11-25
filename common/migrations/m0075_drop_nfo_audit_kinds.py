"""Drop the retired NFO audit findings.

Local ``.nfo`` sidecars are no longer generated, validated or repaired, so the
audit kinds that described them can never be produced again. The rows they left
behind would stay open forever in the quality centre, so this migration deletes
them and narrows both kind constraints to the kinds the catalog still emits.
"""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration

_RETIRED_KINDS = (
    "missing_nfo",
    "invalid_nfo",
    "nfo_metadata_mismatch",
    "nfo_import_state_missing",
    "nfo_import_state_mismatch",
)

_AUDIT_KINDS = (
    "missing_stream",
    "catalog_reference_without_video",
    "unknown_mapping",
    "orphan_file",
    "missing_artwork",
    "missing_metadata",
)

_QUALITY_ACK_KINDS = ("missing_metadata", "missing_artwork")


def _kind_list(kinds: tuple[str, ...]) -> str:
    return ", ".join(f"'{kind}'" for kind in kinds)


def apply(conn: Any, **kwargs: Any) -> None:
    conn.execute(
        "DELETE FROM admin_quality_acknowledgements WHERE kind = ANY(%s)",
        (list(_RETIRED_KINDS),),
    )
    conn.execute(
        "DELETE FROM admin_audit_items WHERE kind = ANY(%s)",
        (list(_RETIRED_KINDS),),
    )
    conn.execute("ALTER TABLE admin_audit_items DROP CONSTRAINT IF EXISTS admin_audit_items_kind_check")
    conn.execute(
        "ALTER TABLE admin_audit_items ADD CONSTRAINT admin_audit_items_kind_check "
        f"CHECK (kind IN ({_kind_list(_AUDIT_KINDS)}))"
    )
    conn.execute(
        "ALTER TABLE admin_quality_acknowledgements DROP CONSTRAINT IF EXISTS "
        "admin_quality_acknowledgements_kind_check"
    )
    conn.execute(
        "ALTER TABLE admin_quality_acknowledgements ADD CONSTRAINT "
        "admin_quality_acknowledgements_kind_check "
        f"CHECK (kind IN ({_kind_list(_QUALITY_ACK_KINDS)}))"
    )


migration = Migration(
    75,
    "0075_drop_nfo_audit_kinds",
    "Drop the retired NFO audit findings and their kinds",
    apply,
)
