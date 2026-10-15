"""Add StashDB and PBC external link columns to performer_bios and populate from catalog."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration


def apply(conn: Any, **kwargs) -> None:
    conn.execute("ALTER TABLE performer_bios ADD COLUMN IF NOT EXISTS stashdb_url TEXT DEFAULT ''")
    conn.execute("ALTER TABLE performer_bios ADD COLUMN IF NOT EXISTS pbc_url TEXT DEFAULT ''")
    conn.execute("""
        DO $$
        BEGIN
            IF to_regclass('performer_catalog') IS NOT NULL THEN
                UPDATE performer_bios b
                SET stashdb_url = 'https://stashdb.org/performers/' || c.source_id
                FROM performer_catalog c
                WHERE c.source = 'stashdb'
                  AND lower(c.name) = lower(b.name)
                  AND (b.stashdb_url IS NULL OR b.stashdb_url = '');
            END IF;
        END
        $$
    """)


migration = Migration(
    version=41,
    name="0041_performer_external_links",
    description="Persist StashDB and PBC profile links on performer_bios",
    apply_fn=apply,
)
