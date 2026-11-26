"""The catalog total is cached briefly so paging does not re-count every page."""

from __future__ import annotations

import unittest
from unittest import mock

from resolver import client_catalog
from resolver.client_catalog import query_catalog_performers
from tests.pg_helper import PgTestCase


class _CountingConnection:
    """Connection double that counts how often the count query actually runs."""

    def __init__(self, total: int = 7, dbname: str = "adulthub"):
        self.total = total
        self.executions = 0
        self.info = mock.Mock(dbname=dbname)

    def execute(self, _sql, _params=None):
        self.executions += 1
        return self

    def fetchone(self):
        return (self.total,)


class CatalogCountCacheTest(unittest.TestCase):
    def setUp(self):
        client_catalog._COUNT_CACHE.clear()
        self.addCleanup(client_catalog._COUNT_CACHE.clear)

    def test_repeated_queries_reuse_one_count(self):
        conn = _CountingConnection()
        for _ in range(5):
            total = client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v", [])

        self.assertEqual(total, 7)
        self.assertEqual(conn.executions, 1)

    def test_different_filters_are_counted_separately(self):
        conn = _CountingConnection()
        client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v WHERE q=%s", ["a"])
        client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v WHERE q=%s", ["b"])

        self.assertEqual(conn.executions, 2)

    def test_list_parameters_are_part_of_the_key(self):
        conn = _CountingConnection()
        client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v WHERE id = ANY(%s)",
                                     [["a", "b"]])
        client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v WHERE id = ANY(%s)",
                                     [["a", "b"]])
        client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v WHERE id = ANY(%s)",
                                     [["a", "c"]])

        self.assertEqual(conn.executions, 2)

    def test_different_databases_do_not_share_totals(self):
        first = _CountingConnection(dbname="adulthub")
        second = _CountingConnection(dbname="adult_hub_test")
        client_catalog._catalog_total(first, "SELECT count(*) FROM videos v", [])
        client_catalog._catalog_total(second, "SELECT count(*) FROM videos v", [])

        self.assertEqual(first.executions, 1)
        self.assertEqual(second.executions, 1)

    def test_expired_entries_are_recounted(self):
        conn = _CountingConnection()
        with mock.patch.object(client_catalog, "_COUNT_CACHE_TTL_SECONDS", 0.01):
            client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v", [])
            client_catalog._COUNT_CACHE.update({
                key: (value[0] - 1, value[1])
                for key, value in client_catalog._COUNT_CACHE.items()
            })
            client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v", [])

        self.assertEqual(conn.executions, 2)

    def test_zero_ttl_disables_the_cache(self):
        conn = _CountingConnection()
        with mock.patch.object(client_catalog, "_COUNT_CACHE_TTL_SECONDS", 0):
            client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v", [])
            client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v", [])

        self.assertEqual(conn.executions, 2)

    def test_cache_is_bounded(self):
        conn = _CountingConnection()
        with mock.patch.object(client_catalog, "_COUNT_CACHE_MAX_ENTRIES", 2):
            for index in range(5):
                client_catalog._catalog_total(
                    conn, f"SELECT count(*) FROM videos v -- {index}", [])

        self.assertLessEqual(len(client_catalog._COUNT_CACHE), 2)
        self.assertEqual(conn.executions, 5)


    def test_personalized_totals_are_never_cached(self):
        conn = _CountingConnection()
        for _ in range(3):
            client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v WHERE fav=%s",
                                          ["user-1"], cache=False)

        self.assertEqual(conn.executions, 3)


    def test_clear_helper_drops_every_cached_total(self):
        conn = _CountingConnection()
        client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v", [])
        client_catalog.clear_catalog_count_cache()
        client_catalog._catalog_total(conn, "SELECT count(*) FROM videos v", [])

        self.assertEqual(conn.executions, 2)


class _PerformerCountRecorder:
    """Connection double counting how often the performer total is computed."""

    def __init__(self, conn):
        self._conn = conn
        self.count_queries = 0

    def execute(self, sql, params=None):
        if "COUNT(DISTINCT perf" in sql:
            self.count_queries += 1
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class PerformerTotalCachePgTest(PgTestCase):
    def test_legacy_performer_total_is_counted_once_per_ttl(self):
        """The materialised list skips this query, so check the fallback path."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id,provider,title,duration,playable,performers) VALUES "
                "('telegram:-100123:1:1','telegram','One',900,1,'[\"Anna\"]'::jsonb),"
                "('telegram:-100123:2:2','telegram','Two',900,1,'[\"Bella\"]'::jsonb)"
            )

        recorded = _PerformerCountRecorder(self.conn)
        with mock.patch.object(client_catalog, "PERFORMER_STATS_TABLE", "performer_stats_absent"):
            first = query_catalog_performers(recorded, limit=10)
            second = query_catalog_performers(recorded, limit=10)

        self.assertEqual(first.total_count, 2)
        self.assertEqual(second.total_count, 2)
        self.assertEqual(recorded.count_queries, 1)


if __name__ == "__main__":
    unittest.main()
