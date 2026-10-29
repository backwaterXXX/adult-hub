"""Index performer aliases by the canonical name the catalog joins on."""
from common.migrations.base import Migration


def apply(conn, **kwargs):
    conn.execute("CREATE INDEX IF NOT EXISTS idx_performer_aliases_canonical_lower "
                 "ON public.performer_aliases USING btree (lower(canonical))")


migration = Migration(56, "0056_performer_alias_canonical_index",
                      "Index performer aliases by canonical name", apply)
current_migration = Migration(8, "0008_performer_alias_canonical_index",
                              "Index performer aliases by canonical name", apply)
