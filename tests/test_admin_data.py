"""Domain data access for the admin panel, independent of HTTP routing."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pg_helper import PgTestCase, TEST_URL


import resolver.admin_data as admin_data
from common import db
from tests.helpers.db_session import db_session


BAD_URL = "postgresql://127.0.0.1:1/missing"


class TestChannelLabels(unittest.TestCase):
    def test_maps_stable_ids_and_bare_numeric_form(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(
                "sources:\n"
                "  - provider: telegram\n"
                "    channels:\n"
                "      - id: '-100111'\n"
                "        studio: 'Alpha'\n",
                encoding="utf-8",
            )
            labels = admin_data.channel_labels(config)
        self.assertEqual(labels["-100111"], "Alpha")
        self.assertEqual(labels["111"], "Alpha")

    def test_invalid_or_telegram_free_config_returns_empty_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.yaml"
            invalid.write_text("sources: 'not-a-list'\n", encoding="utf-8")
            self.assertEqual(admin_data.channel_labels(invalid), {})
            other = Path(directory) / "other.yaml"
            other.write_text(
                "sources:\n  - provider: secondweb\n    address: 'http://example.com'\n",
                encoding="utf-8",
            )
            self.assertEqual(admin_data.channel_labels(other), {})


class TestConfiguredSources(unittest.TestCase):
    def test_lists_enabled_and_disabled_channels(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text(
                "sync:\n  interval_minutes: 60\n"
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Alpha'\n        enabled: true\n"
                "      - id: '-100222'\n        studio: 'Beta'\n        enabled: false\n",
                encoding="utf-8",
            )
            data = admin_data.configured_sources(config)
        self.assertTrue(data["available"])
        self.assertEqual(data["managed_revision"], 0)
        self.assertEqual(len(data["sources"]), 1)
        channels = {channel["id"]: channel for channel in data["sources"][0]["channels"]}
        self.assertTrue(channels["-100111"]["enabled"])
        self.assertFalse(channels["-100222"]["enabled"])
        self.assertEqual(channels["-100222"]["studio"], "Beta")

    def test_invalid_config_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("sources: 'nope'\n", encoding="utf-8")
            data = admin_data.configured_sources(config)
        self.assertFalse(data["available"])
        self.assertEqual(data["sources"], [])


class _FakeResult:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,)

    def fetchall(self):
        return []


class _FakeConnection:
    closed = False

    def execute(self, query, _params=()):
        normalized = " ".join(query.split())
        if "performer_photos" in normalized:
            self.photo_query = normalized
            return _FakeResult(3)
        if "performer_bios" in normalized:
            self.bio_query = normalized
            return _FakeResult(2)
        if "performer_aliases" in normalized:
            self.alias_query = normalized
            return _FakeResult(4)
        return self._stat_result(normalized)

    def _stat_result(self, normalized):
        if "pg_database_size" in normalized:
            return _FakeResult(1.0)
        if "count(*) FROM videos" in normalized or "count(*) FROM synced_files" in normalized:
            return _FakeResult(0)
        if "MAX(last_seen)" in normalized:
            return _FakeResult(None)
        if "FROM videos WHERE last_seen" in normalized:
            return _FakeResult(0)
        if "WHERE playable = 0" in normalized:
            return _FakeResult(0)
        raise AssertionError(f"Unexpected SQL: {normalized}")

    def close(self):
        self.closed = True


class TestPerformerQualityMetrics(unittest.TestCase):
    def test_counts_only_nonempty_profile_fields_and_recorded_aliases(self):
        connection = _FakeConnection()
        tables = {"videos", "synced_files", "performer_photos", "performer_bios", "performer_aliases"}
        with (
            patch.object(admin_data.db, "connect", return_value=connection),
            patch.object(admin_data.db, "table_names", return_value=tables),
            patch.object(admin_data.db, "table_columns", return_value={"playable"}),
            patch.object(admin_data, "channel_labels", return_value={}),
        ):
            result = admin_data.db_stats("postgresql://unused", "/unused/config.yaml")

        self.assertEqual((result["photos_total"], result["bios_total"], result["aliases_total"]), (3, 2, 4))
        whitespace = "chr(32) || chr(9) || chr(10) || chr(13)"
        self.assertIn(f"btrim(COALESCE(thumb, ''), {whitespace}) <> ''", connection.photo_query)
        self.assertIn(f"btrim(COALESCE(bio, ''), {whitespace}) <> ''", connection.bio_query)
        self.assertIn(f"btrim(COALESCE(alias, ''), {whitespace}) <> ''", connection.alias_query)
        self.assertIn(f"btrim(COALESCE(canonical, ''), {whitespace}) <> ''", connection.alias_query)
        self.assertTrue(connection.closed)


class TestUnavailableVideos(PgTestCase):
    def _insert_videos(self, rows):
        with db_session(TEST_URL) as conn:
            for row in rows:
                conn.execute(
                    "INSERT INTO videos (id, title, provider, studio, playable) VALUES (%s, %s, %s, %s, %s)",
                    row,
                )

    def _insert_dead(self, rows):
        with db_session(TEST_URL) as conn:
            for row in rows:
                conn.execute(
                    "INSERT INTO dead_streams (video_id, reason, detected_at) VALUES (%s, %s, %s)",
                    row,
                )

    def _counts(self):
        conn = db.connect(TEST_URL)
        try:
            videos = conn.execute("SELECT count(*) FROM videos").fetchone()[0]
            dead = conn.execute("SELECT count(*) FROM dead_streams").fetchone()[0]
        finally:
            conn.close()
        return (videos, dead)

    def test_reports_unplayable_and_dead_streams_read_only(self):
        self._insert_videos([
            ('a', 'A', 'p', 's', 1),
            ('b', 'B', 'p', 's', 0),
            ('c', 'C', 'p', 's', 1),
        ])
        self._insert_dead([('c', 'stream gone', 123)])
        before = self._counts()
        data = admin_data.unavailable_videos(TEST_URL, limit=10)
        self.assertTrue(data["available"])
        self.assertEqual(data["total"], 2)
        self.assertEqual({video["id"] for video in data["videos"]}, {"b", "c"})
        self.assertEqual(self._counts(), before)
        self.assertFalse(data["truncated"])

    def test_offset_and_truncation(self):
        self._insert_videos([(f"v{index}", f"V{index}", 'p', 's', 0) for index in range(3)])
        first = admin_data.unavailable_videos(TEST_URL, limit=1, offset=0)
        self.assertEqual(len(first["videos"]), 1)
        self.assertTrue(first["truncated"])
        last = admin_data.unavailable_videos(TEST_URL, limit=1, offset=2)
        self.assertEqual(len(last["videos"]), 1)
        self.assertEqual(last["videos"][0]["id"], "v2")
        self.assertFalse(last["truncated"])

    def test_missing_database_is_unavailable(self):
        data = admin_data.unavailable_videos(BAD_URL)
        self.assertFalse(data["available"])
        self.assertEqual(data["videos"], [])

    def test_query_error_returns_unavailable(self):
        """A database error during introspection returns available=False."""
        fake_conn = type("FakeConn", (), {})()
        fake_conn.row_factory = None
        fake_conn.close = lambda: None

        def fake_connect(url):
            return fake_conn

        def fake_table_names(conn):
            raise db.OperationalError("connection failed")

        def fake_table_columns(conn, table):
            return {"id", "title", "provider", "studio", "playable"}

        class FakeCursor:
            def fetchone(self):
                return (0,)

            def fetchall(self):
                return []

        def fake_execute(query, *args):
            if "count(*)" in query:
                return FakeCursor()
            return FakeCursor()

        with patch.object(admin_data.db, "connect", side_effect=fake_connect):
            with patch.object(admin_data.db, "table_names", side_effect=fake_table_names):
                with patch.object(admin_data.db, "table_columns", side_effect=fake_table_columns):
                    fake_conn.execute = fake_execute
                    data = admin_data.unavailable_videos(TEST_URL)
                    self.assertFalse(data["available"])
                    self.assertEqual(data["videos"], [])
                    self.assertEqual(data["total"], 0)

    def test_query_error_returns_unavailable_with_patch(self):
        """A database error during query returns available=False."""
        fake_conn = type("FakeConn", (), {})()
        fake_conn.row_factory = None
        fake_conn.close = lambda: None

        def fake_connect(url):
            return fake_conn

        def fake_table_names(conn):
            return ["videos"]

        def fake_table_columns(conn, table):
            return {"id", "title", "provider", "studio", "playable"}

        class FakeCursor:
            def fetchone(self):
                return (0,)

            def fetchall(self):
                raise db.ProgrammingError("syntax error")

        def fake_execute(query, *args):
            if "count(*)" in query:
                return FakeCursor()
            raise db.ProgrammingError("syntax error")

        with patch.object(admin_data.db, "connect", side_effect=fake_connect):
            with patch.object(admin_data.db, "table_names", side_effect=fake_table_names):
                with patch.object(admin_data.db, "table_columns", side_effect=fake_table_columns):
                    fake_conn.execute = fake_execute
                    data = admin_data.unavailable_videos(TEST_URL)
                    self.assertFalse(data["available"])
                    self.assertEqual(data["videos"], [])
                    self.assertEqual(data["total"], 0)

    def test_programming_error_returns_unavailable(self):
        """A ProgrammingError also returns available=False."""
        fake_conn = type("FakeConn", (), {})()
        fake_conn.row_factory = None
        fake_conn.close = lambda: None

        def fake_connect(url):
            return fake_conn

        def fake_table_names(conn):
            return ["videos"]

        def fake_table_columns(conn, table):
            return {"id", "title", "provider", "studio", "playable"}

        class FakeCursor:
            def fetchone(self):
                return (0,)

            def fetchall(self):
                raise db.ProgrammingError("syntax error")

        def fake_execute(query, *args):
            if "count(*)" in query:
                return FakeCursor()
            raise db.ProgrammingError("syntax error")

        with patch.object(admin_data.db, "connect", side_effect=fake_connect):
            with patch.object(admin_data.db, "table_names", side_effect=fake_table_names):
                with patch.object(admin_data.db, "table_columns", side_effect=fake_table_columns):
                    fake_conn.execute = fake_execute
                    data = admin_data.unavailable_videos(TEST_URL)
                    self.assertFalse(data["available"])
                    self.assertEqual(data["videos"], [])
                    self.assertEqual(data["total"], 0)


class TestQuarantineReport(PgTestCase):
    def test_database_error_returns_unavailable(self):
        """A database error returns available=False."""
        fake_conn = type("FakeConn", (), {})()
        fake_conn.row_factory = None
        fake_conn.close = lambda: None

        def fake_connect(url):
            return fake_conn

        class FakeCursor:
            def fetchone(self):
                return (0,)

            def fetchall(self):
                raise db.OperationalError("connection failed")

        def fake_execute(query, *args):
            if "count(*)" in query:
                return FakeCursor()
            raise db.OperationalError("connection failed")

        with patch.object(admin_data.db, "connect", side_effect=fake_connect):
            fake_conn.execute = fake_execute
            data = admin_data.quarantine_report(TEST_URL)
            self.assertFalse(data["available"])
            self.assertEqual(data["items"], [])
            self.assertEqual(data["total"], 0)

    def _seed(self):
        snapshot = json.dumps({
            "video": {"id": "v1", "title": "V1", "provider": "p"},
            "files": [{"source": "/secret/path.strm", "quarantine": "/hidden/x.strm"}],
        })
        with db_session(TEST_URL) as conn:
            conn.execute("INSERT INTO video_duplicate_decisions (loser_id, keeper_id, operation_id, state, created_at, snapshot) VALUES ('v1','v1','op1','active',10,%s)", (snapshot,))
            conn.execute("INSERT INTO video_duplicate_decisions (loser_id, keeper_id, operation_id, state, created_at, snapshot) VALUES ('v2','keeper','op2','active',20,%s)", (snapshot,))
            conn.execute("INSERT INTO video_duplicate_decisions (loser_id, keeper_id, operation_id, state, created_at, snapshot) VALUES ('v3','v3','op3','undone',30,%s)", (snapshot,))

    def test_lists_only_active_decisions_without_paths(self):
        self._seed()
        report = admin_data.quarantine_report(TEST_URL)
        self.assertEqual(report["total"], 2)
        modes = {item["id"]: item["mode"] for item in report["items"]}
        self.assertEqual(modes, {"v1": "quarantine", "v2": "hide"})
        self.assertEqual(report["items"][0]["files"], 1)
        self.assertNotIn("secret", json.dumps(report))
        self.assertNotIn("/hidden", json.dumps(report))

    def test_missing_database_and_empty_table_degrade_gracefully(self):
        self.assertFalse(admin_data.quarantine_report(BAD_URL)["available"])
        report = admin_data.quarantine_report(TEST_URL)
        self.assertTrue(report["available"])
        self.assertEqual(report["items"], [])

    def test_empty_library_is_empty(self):
        data = admin_data.unavailable_videos(TEST_URL)
        self.assertTrue(data["available"])
        self.assertEqual(data["total"], 0)


class TestBackupStatus(unittest.TestCase):
    def test_distinguishes_empty_inventory_from_unreadable_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = admin_data.backup_status(directory)
            self.assertTrue(empty["inventory_available"])
            self.assertFalse(empty["available"])
            self.assertEqual(empty["count"], 0)

            inaccessible = admin_data.backup_status(Path(directory) / "missing")
            self.assertFalse(inaccessible["inventory_available"])
            self.assertFalse(inaccessible["available"])

    def test_inventory_orders_only_regular_backup_files(self):
        with tempfile.TemporaryDirectory() as directory:
            older = Path(directory) / "adulthub-older.dump.gz"
            latest = Path(directory) / "adulthub-latest.dump.gz"
            ignored = Path(directory) / "notes.txt"
            older.write_bytes(b"old")
            latest.write_bytes(b"new")
            ignored.write_bytes(b"not a backup")
            (Path(directory) / "adulthub-link.dump.gz").symlink_to(latest)
            import os
            os.utime(older, (100, 100))
            os.utime(latest, (200, 200))

            status = admin_data.backup_status(directory)

        self.assertTrue(status["inventory_available"])
        self.assertTrue(status["available"])
        self.assertEqual(status["latest"]["name"], "adulthub-latest.dump.gz")
        self.assertEqual(status["count"], 2)


if __name__ == "__main__":
    unittest.main()
