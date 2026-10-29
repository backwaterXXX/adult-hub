"""The people catalog looks aliases up by canonical name, and that lookup is indexed.

Production measured 124 ms for one 50-row page of `/admin/api/v2/organize/people`
before migration 0056: the per-row alias subquery filtered all 3,628 alias rows
each time, so a page read the whole table once per row it returned.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT))
from pg_helper import PgTestCase

# Written the way resolver/admin_v2_organize.py writes it; the second test in
# this module fails if that route stops matching this expression.
ALIAS_LOOKUP = (
    "EXPLAIN SELECT COALESCE((SELECT jsonb_agg(pa.alias ORDER BY lower(pa.alias), pa.alias) "
    "FROM performer_aliases pa WHERE lower(pa.canonical) = lower(%s)), '[]'::jsonb)"
)
ALIAS_ROWS = 5_000


class TestPerformerAliasLookup(PgTestCase):
    def plan(self) -> str:
        with self.conn:
            self.conn.execute("INSERT INTO performer_aliases (alias, canonical) "
                              "SELECT 'alias ' || g, 'canonical ' || floor(g / 10.0)::int "
                              "FROM generate_series(1, %s) g", (ALIAS_ROWS,))
            self.conn.execute("ANALYZE performer_aliases")
        return "\n".join(row[0] for row in self.conn.execute(ALIAS_LOOKUP, ("canonical 7",)))

    def test_lookup_by_canonical_name_uses_the_index(self):
        plan = self.plan()

        self.assertNotIn("Seq Scan on performer_aliases", plan)
        self.assertIn("idx_performer_aliases_canonical_lower", plan)

    def test_the_people_route_still_asks_for_the_indexed_expression(self):
        route = (ROOT / "resolver" / "admin_v2_organize.py").read_text(encoding="utf-8")

        self.assertIn("lower(pa.canonical) = lower(p.performer_name)", route)


if __name__ == "__main__":
    unittest.main()
