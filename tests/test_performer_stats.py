"""The client performer list reads a materialised table refreshed by sync."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from resolver import client_catalog
from resolver.client_catalog import (
    PerformerRef,
    performer_stats_state,
    query_catalog_performers,
    refresh_performer_stats,
)
from tests.pg_helper import PgTestCase


def _items(page) -> list[tuple[str, str, int]]:
    return [(item.id, item.name, item.video_count) for item in page.items]


class PerformerStatsTestCase(PgTestCase):
    def setUp(self):
        super().setUp()
        with self.conn:
            self.conn.execute(
                "INSERT INTO performer_catalog (id, source, source_id, name, active) VALUES "
                "('perf-anna', 'stashdb', 'anna', 'Anna Alpha', true),"
                "('perf-bella', 'stashdb', 'bella', 'Bella Beta', true)"
            )
            self.conn.execute(
                "INSERT INTO performer_photos (name, thumb) VALUES "
                "('Anna Alpha', 'thumbs/anna.jpg'),"
                "('Bella Beta', 'https://cdn.example.test/bella.webp')"
            )
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, duration, playable, performers) VALUES "
                "('telegram:-100123:1:1', 'telegram', 'One', 900, 1, '[\"Anna Alpha\"]'::jsonb),"
                "('telegram:-100123:2:2', 'telegram', 'Two', 900, 1, '[\"Anna Alpha\", \"Bella Beta\"]'::jsonb),"
                "('telegram:-100123:3:3', 'telegram', 'Three', 900, 1, '[\"Bella Beta\"]'::jsonb),"
                "('telegram:-100123:4:4', 'telegram', 'Four', 900, 1, '[\"Anna Alpha\"]'::jsonb)"
            )


class TestRefresh(PerformerStatsTestCase):
    def test_refresh_materialises_counts_names_and_local_photo_flag(self):
        self.assertEqual(refresh_performer_stats(self.conn), 2)

        rows = {
            row[0]: row
            for row in self.conn.execute(
                "SELECT canonical_id, canonical_name, search_names, video_count, has_photo "
                "FROM performer_stats"
            ).fetchall()
        }
        self.assertEqual(set(rows), {"perf-anna", "perf-bella"})
        anna = rows["perf-anna"]
        self.assertEqual(anna[1], "Anna Alpha")
        self.assertEqual(anna[2], ["anna alpha"])
        self.assertEqual(anna[3], 3)
        self.assertTrue(anna[4])
        # has_photo mirrors the existing photo_only filter: any stored thumbnail,
        # including an upstream URL that the artwork route will not proxy.
        self.assertTrue(rows["perf-bella"][4])

        state = performer_stats_state(self.conn)
        self.assertEqual(state["row_count"], 2)
        self.assertTrue(state["config_fingerprint"])

    def test_refresh_ignores_known_studio_and_channel_labels(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, duration, playable, performers) VALUES "
                "('telegram:-100123:5:5', 'telegram', 'Five', 900, 1, "
                "'[\"Naughty\", \"Anna Alpha\"]'::jsonb)"
            )

        refresh_performer_stats(self.conn)
        names = {
            row[0] for row in self.conn.execute(
                "SELECT canonical_name FROM performer_stats"
            ).fetchall()
        }
        self.assertEqual(names, {"Anna Alpha", "Bella Beta"})

    def test_refresh_replaces_previous_rows(self):
        refresh_performer_stats(self.conn)
        with self.conn:
            self.conn.execute("DELETE FROM videos WHERE id='telegram:-100123:3:3'")

        self.assertEqual(refresh_performer_stats(self.conn), 2)
        bella = self.conn.execute(
            "SELECT video_count FROM performer_stats WHERE canonical_id='perf-bella'"
        ).fetchone()
        self.assertEqual(bella[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM performer_stats").fetchone()[0], 2
        )


class TestListReadsTheTable(PerformerStatsTestCase):
    def setUp(self):
        super().setUp()
        refresh_performer_stats(self.conn)

    def test_default_page_is_name_sorted_with_local_photo_urls(self):
        page = query_catalog_performers(self.conn, limit=50)

        self.assertEqual(page.total_count, 2)
        self.assertEqual(_items(page), [
            ("perf-anna", "Anna Alpha", 3),
            ("perf-bella", "Bella Beta", 2),
        ])
        with_photo = {item.name: item.photo_url for item in page.items}
        self.assertEqual(with_photo["Anna Alpha"], "/api/v1/artwork/performer/perf-anna")
        self.assertIsNone(with_photo["Bella Beta"])

    def test_search_letter_photo_and_count_sort(self):
        self.assertEqual(_items(query_catalog_performers(self.conn, q="bella")),
                         [("perf-bella", "Bella Beta", 2)])
        self.assertEqual(_items(query_catalog_performers(self.conn, letter="a")),
                         [("perf-anna", "Anna Alpha", 3)])
        # photo_only keeps the legacy "has a stored thumbnail" meaning, so an
        # upstream-only thumbnail still qualifies even though photo_url is None.
        self.assertEqual(_items(query_catalog_performers(self.conn, photo_only=True)),
                         [("perf-anna", "Anna Alpha", 3), ("perf-bella", "Bella Beta", 2)])
        self.assertEqual(
            _items(query_catalog_performers(self.conn, sort="video_count", direction="asc")),
            [("perf-bella", "Bella Beta", 2), ("perf-anna", "Anna Alpha", 3)],
        )

    def test_pagination_reports_has_more(self):
        first = query_catalog_performers(self.conn, limit=1)
        second = query_catalog_performers(self.conn, limit=1, offset=1)

        self.assertEqual(first.total_count, 2)
        self.assertTrue(first.has_more)
        self.assertFalse(second.has_more)

    def test_changed_configuration_refreshes_before_the_page_is_served(self):
        with mock.patch.object(client_catalog, "_extract_disabled_channel_ids",
                               return_value={"-100123"}):
            page = query_catalog_performers(self.conn, limit=50)

        # The only channel is now disabled, so no performer has a visible video.
        self.assertEqual(page.total_count, 0)
        self.assertEqual(performer_stats_state(self.conn)["row_count"], 0)


class TestLegacyParity(PerformerStatsTestCase):
    def test_materialised_page_matches_the_legacy_query(self):
        refresh_performer_stats(self.conn)
        from_stats = query_catalog_performers(self.conn, limit=50)

        with mock.patch.object(client_catalog, "PERFORMER_STATS_TABLE", "performer_stats_absent"):
            legacy = query_catalog_performers(self.conn, limit=50)

        self.assertEqual(_items(from_stats), _items(legacy))
        self.assertEqual(from_stats.total_count, legacy.total_count)
        self.assertEqual(
            [item.photo_url for item in from_stats.items],
            [item.photo_url for item in legacy.items],
        )

    def test_legacy_query_still_serves_when_the_table_is_absent(self):
        with mock.patch.object(client_catalog, "PERFORMER_STATS_TABLE", "performer_stats_absent"):
            page = query_catalog_performers(self.conn, limit=50)

        self.assertEqual(page.total_count, 2)
        self.assertIsInstance(page.items[0], PerformerRef)



class TestSyncRefreshHook(unittest.TestCase):
    """The sync pass rebuilds the list and never fails because of it."""

    def _logger(self):
        events = []

        def record(kind):
            return lambda *args, **kwargs: events.append(
                (kind, " ".join(str(arg) for arg in args[1:]))
            )

        return mock.Mock(info=record("info"), warning=record("warning")), events

    def test_success_is_logged_and_failure_only_warns(self):
        from sync import sync_orchestration

        logger, events = self._logger()
        conn = mock.Mock()
        with mock.patch.object(sync_orchestration, "_core", mock.Mock(logger=logger)), \
                mock.patch("resolver.client_catalog.refresh_performer_stats", return_value=6699):
            sync_orchestration._refresh_catalog_caches(conn)
        self.assertEqual(events[0][0], "info")

        logger, events = self._logger()
        with mock.patch.object(sync_orchestration, "_core", mock.Mock(logger=logger)), \
                mock.patch("resolver.client_catalog.refresh_performer_stats",
                           side_effect=RuntimeError("database is gone")):
            sync_orchestration._refresh_catalog_caches(conn)
        self.assertEqual(events[0][0], "warning")
        self.assertIn("database is gone", events[0][1])


class TestColdRefreshSerialisation(unittest.TestCase):
    """A cold performer list is materialised once, not once per waiting request."""

    def test_concurrent_cold_reads_refresh_only_once(self):
        refreshed = threading.Event()
        calls: list[int] = []

        def state(_conn):
            return {"config_fingerprint": "fp"} if refreshed.is_set() else None

        def refresh(_conn):
            calls.append(1)
            refreshed.set()

        with mock.patch.object(client_catalog, "performer_stats_state", side_effect=state), \
                mock.patch.object(client_catalog, "_performer_stats_fingerprint", return_value="fp"), \
                mock.patch.object(client_catalog, "refresh_performer_stats", side_effect=refresh):
            threads = [threading.Thread(target=client_catalog._ensure_performer_stats, args=(None,))
                       for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(calls), 1)

    def test_a_failed_refresh_reports_no_state(self):
        with mock.patch.object(client_catalog, "performer_stats_state", return_value=None), \
                mock.patch.object(client_catalog, "_performer_stats_fingerprint", return_value="fp"), \
                mock.patch.object(client_catalog, "refresh_performer_stats",
                                  side_effect=RuntimeError("database is gone")):
            self.assertIsNone(client_catalog._ensure_performer_stats(None))


if __name__ == "__main__":
    unittest.main()
