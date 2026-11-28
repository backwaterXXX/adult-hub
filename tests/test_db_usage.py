"""Database usage report contract."""

from __future__ import annotations

import unittest

from common.db_usage import read_db_usage
from tests.pg_helper import PgTestCase


class DatabaseUsageTests(PgTestCase):
    def test_reports_database_and_largest_tables(self):
        report = read_db_usage(self.db_url, limit=5)
        self.assertTrue(report["available"])
        self.assertGreater(report["database_bytes"], 0)
        self.assertGreaterEqual(len(report["tables"]), 1)
        self.assertLessEqual(len(report["tables"]), 5)
        table = report["tables"][0]
        self.assertEqual(set(table), {
            "name", "total_bytes", "table_bytes", "index_bytes", "toast_bytes",
            "live_rows", "dead_rows",
        })
        self.assertTrue(any(row["name"] == "videos" for row in read_db_usage(self.db_url, limit=50)["tables"]))

    def test_missing_database_is_unavailable(self):
        self.assertFalse(read_db_usage(None)["available"])


if __name__ == "__main__":
    unittest.main()
