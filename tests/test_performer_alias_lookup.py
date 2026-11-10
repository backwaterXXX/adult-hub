"""The people catalog looks aliases up by canonical name, and that lookup is indexed.

Production measured 124 ms for one 50-row page of `/admin/api/v2/organize/people`
before migration 0056: the per-row alias filter read all 3,628 alias rows every
time, and searching by alias repeated that for every candidate row.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from pg_helper import PgTestCase

# The per-row shape of resolver/admin_v2_organize.py's alias search; the second
# test fails if that route stops matching the indexed expression.
ALIAS_PROBE = ("EXPLAIN SELECT EXISTS (SELECT 1 FROM performer_aliases pa "
               "WHERE lower(pa.canonical) = lower(%s) AND pa.alias ILIKE %s)")
ALIAS_ROWS = 5_000


class TestPerformerAliasLookup(PgTestCase):
    def plan(self) -> str:
        with self.conn:
            self.conn.execute("INSERT INTO performer_aliases (alias, canonical) "
                              "SELECT 'alias ' || g, 'canonical ' || floor(g / 10.0)::int "
                              "FROM generate_series(1, %s) g", (ALIAS_ROWS,))
            self.conn.execute("ANALYZE performer_aliases")
        return "\n".join(row[0] for row in self.conn.execute(ALIAS_PROBE, ("canonical 7", "alias 7%")))

    def test_lookup_by_canonical_name_uses_the_index(self):
        plan = self.plan()

        self.assertNotIn("Seq Scan on performer_aliases", plan)
        self.assertIn("idx_performer_aliases_canonical_lower", plan)

    def test_the_people_route_still_asks_for_the_indexed_expression(self):
        route = (ROOT / "resolver" / "admin_v2_organize.py").read_text(encoding="utf-8")

        self.assertIn("lower(pa.canonical) = lower(p.performer_name)", route)


if __name__ == "__main__":
    unittest.main()
