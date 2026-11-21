import os
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

from tests.pg_helper import PgTestCase
from sync.fansdb_catalog_sync import (
    KEY_SYNCED_AT,
    PROJECT_TIMEZONE,
    _updated_epoch,
    is_catalog_due,
    sync_catalog,
    sync_catalog_if_due,
    sync_fansdb_catalog,
    sync_fansdb_catalog_if_due,
)


class TestFansdbCatalogSync(PgTestCase):
    def test_updated_epoch_conversions(self):
        self.assertIsNone(_updated_epoch(None))
        self.assertIsNone(_updated_epoch(""))
        self.assertIsNone(_updated_epoch("not-a-date"))
        self.assertEqual(_updated_epoch(12345.0), 12345.0)
        self.assertEqual(_updated_epoch("2026-11-20T17:06:40Z"), 1795194400.0)

    def test_missing_api_key_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("FANSDB_API_KEY", None)
            os.environ.pop("FANS_API_KEY", None)
            with self.assertRaises(RuntimeError) as ctx:
                sync_catalog(self.conn)
            self.assertIn("FANSDB_API_KEY is required", str(ctx.exception))

    def test_sync_catalog_if_due_returns_none_when_unconfigured(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("FANSDB_API_KEY", None)
            os.environ.pop("FANS_API_KEY", None)
            result = sync_catalog_if_due(self.conn)
            self.assertIsNone(result)

    def test_sync_catalog_if_due_skips_when_recently_synced(self):
        fixed_dt = datetime(2026, 11, 21, 14, 0, 0, tzinfo=PROJECT_TIMEZONE)
        fixed_now = int(fixed_dt.timestamp())
        with patch.dict(os.environ, {"FANSDB_API_KEY": "fake-token"}), \
             patch("sync.fansdb_catalog_sync.time.time", return_value=fixed_now):
            self.conn.execute("INSERT INTO meta(key, value) VALUES('performer_catalog_fansdb_synced_at', %s)", (str(fixed_now - 3600),))
            self.conn.commit()

            result = sync_catalog_if_due(self.conn, interval_seconds=86400, now=fixed_now)
            self.assertIsNone(result)

    def test_sync_catalog_imports_records_and_aliases(self):
        sample_page = {
            "data": {
                "queryPerformers": {
                    "count": 2,
                    "performers": [
                        {
                            "id": "uuid-1",
                            "name": "Alpha Creator",
                            "aliases": ["AlphaPrime", "A-Creator"],
                            "gender": "MALE",
                            "updated": "2026-11-20T12:00:00Z",
                            "deleted": False,
                        },
                        {
                            "id": "uuid-2",
                            "name": "Beta Star",
                            "aliases": [],
                            "gender": "FEMALE",
                            "updated": "2026-11-19T10:00:00Z",
                            "deleted": True,
                        },
                    ],
                }
            }
        }

        def mock_post(payload, token=None):
            return sample_page

        people, aliases = sync_catalog(self.conn, token="test-token", post=mock_post)
        self.assertEqual(people, 2)
        # uuid-1 has name "Alpha Creator", aliases "AlphaOne", "Alpha One" -> normalized forms
        # uuid-2 has name "Beta Star"
        self.assertGreaterEqual(aliases, 3)

        # Check performer_catalog
        rows = self.conn.execute(
            "SELECT id, source, source_id, name, active FROM performer_catalog WHERE source='fansdb' ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], ("fansdb:uuid-1", "fansdb", "uuid-1", "Alpha Creator", True))
        self.assertEqual(rows[1], ("fansdb:uuid-2", "fansdb", "uuid-2", "Beta Star", False))

        # Check performer_catalog_aliases
        alias_rows = self.conn.execute(
            "SELECT alias, origin, approved FROM performer_catalog_aliases WHERE performer_id='fansdb:uuid-1' ORDER BY alias"
        ).fetchall()
        aliases_set = {r[0] for r in alias_rows}
        self.assertIn("Alpha Creator", aliases_set)
        self.assertIn("AlphaPrime", aliases_set)
        self.assertIn("A-Creator", aliases_set)
        for r in alias_rows:
            self.assertEqual(r[1], "fansdb")
            self.assertTrue(r[2])

    def test_sync_catalog_if_due_updates_meta_timestamp(self):
        sample_page = {
            "data": {
                "queryPerformers": {
                    "count": 1,
                    "performers": [
                        {
                            "id": "uuid-sync",
                            "name": "Sync Star",
                            "aliases": [],
                            "updated": "2026-11-20T12:00:00Z",
                            "deleted": False,
                        }
                    ],
                }
            }
        }
        fixed_dt = datetime(2026, 11, 21, 10, 0, 0, tzinfo=PROJECT_TIMEZONE)
        fixed_now = int(fixed_dt.timestamp())

        with patch("sync.fansdb_catalog_sync._post", return_value=sample_page), \
             patch("sync.fansdb_catalog_sync.time.time", return_value=fixed_now), \
             patch.dict(os.environ, {"FANSDB_API_KEY": "fake-token"}):
            result = sync_catalog_if_due(self.conn, now=fixed_now)
            self.assertIsNotNone(result)
            self.assertEqual(result, (1, 1))

            meta_row = self.conn.execute(
                "SELECT value FROM meta WHERE key='performer_catalog_fansdb_synced_at'"
            ).fetchone()
            self.assertIsNotNone(meta_row)
            self.assertEqual(int(meta_row[0]), fixed_now)

            # Subsequent call same day should return None
            second_result = sync_catalog_if_due(self.conn, now=fixed_now + 3600)
            self.assertIsNone(second_result)

    def test_aliases_are_same_functions(self):
        self.assertIs(sync_fansdb_catalog, sync_catalog)
        self.assertIs(sync_fansdb_catalog_if_due, sync_catalog_if_due)

    def test_pagination_validates_count_presence_and_validity(self):
        # Missing count
        with self.assertRaises(RuntimeError) as ctx:
            sync_catalog(self.conn, token="t", post=lambda *a, **k: {"data": {"queryPerformers": {"performers": []}}})
        self.assertIn("missing or invalid count", str(ctx.exception))

        # Non-int count
        with self.assertRaises(RuntimeError) as ctx:
            sync_catalog(self.conn, token="t", post=lambda *a, **k: {"data": {"queryPerformers": {"count": "abc", "performers": []}}})
        self.assertIn("missing or invalid count", str(ctx.exception))

        # Negative count
        with self.assertRaises(RuntimeError) as ctx:
            sync_catalog(self.conn, token="t", post=lambda *a, **k: {"data": {"queryPerformers": {"count": -5, "performers": []}}})
        self.assertIn("missing or invalid count", str(ctx.exception))

    def test_pagination_finite_bound_with_count(self):
        pages_called = []

        def mock_post(payload, token=None):
            page = payload["variables"]["page"]
            pages_called.append(page)
            if page == 1:
                return {
                    "data": {
                        "queryPerformers": {
                            "count": 3,
                            "performers": [
                                {"id": "p1", "name": "P One", "aliases": []},
                                {"id": "p2", "name": "P Two", "aliases": []},
                            ],
                        }
                    }
                }
            elif page == 2:
                return {
                    "data": {
                        "queryPerformers": {
                            "count": 3,
                            "performers": [
                                {"id": "p3", "name": "P Three", "aliases": []},
                            ],
                        }
                    }
                }
            raise AssertionError(f"Unexpected page requested: {page}")

        people, aliases = sync_catalog(self.conn, token="t", post=mock_post, per_page=2, pause=0)
        self.assertEqual(people, 3)
        self.assertEqual(pages_called, [1, 2])

    def test_pagination_rejects_malformed_records(self):
        # Record missing id
        bad_page = {
            "data": {
                "queryPerformers": {
                    "count": 1,
                    "performers": [{"name": "No ID"}],
                }
            }
        }
        with self.assertRaises(RuntimeError) as ctx:
            sync_catalog(self.conn, token="t", post=lambda *a, **k: bad_page, per_page=1)
        self.assertIn("malformed performer record", str(ctx.exception))

        # Record missing name
        bad_page2 = {
            "data": {
                "queryPerformers": {
                    "count": 1,
                    "performers": [{"id": "x1", "name": ""}],
                }
            }
        }
        with self.assertRaises(RuntimeError) as ctx:
            sync_catalog(self.conn, token="t", post=lambda *a, **k: bad_page2, per_page=1)
        self.assertIn("malformed performer record", str(ctx.exception))

    def test_pagination_rejects_repeated_performer_ids(self):
        page1 = {
            "data": {
                "queryPerformers": {
                    "count": 2,
                    "performers": [{"id": "p1", "name": "P One", "aliases": []}],
                }
            }
        }
        # Page 2 returns the exact same ID "p1"
        page2 = {
            "data": {
                "queryPerformers": {
                    "count": 2,
                    "performers": [{"id": "p1", "name": "P One Duplicate", "aliases": []}],
                }
            }
        }

        def mock_post(payload, token=None):
            return page1 if payload["variables"]["page"] == 1 else page2

        with self.assertRaises(RuntimeError) as ctx:
            sync_catalog(self.conn, token="t", post=mock_post, per_page=1, pause=0)
        self.assertIn("repeated performer id", str(ctx.exception))

    def test_pagination_rejects_incomplete_pages(self):
        # Count is 3, per_page is 2, but page 1 only returns 1 performer
        incomplete_page1 = {
            "data": {
                "queryPerformers": {
                    "count": 3,
                    "performers": [{"id": "p1", "name": "P One", "aliases": []}],
                }
            }
        }
        with self.assertRaises(RuntimeError) as ctx:
            sync_catalog(self.conn, token="t", post=lambda *a, **k: incomplete_page1, per_page=2)
        self.assertIn("incomplete page", str(ctx.exception))

    def test_diagnostic_max_pages_does_not_write_daily_synced_timestamp(self):
        sample_page = {
            "data": {
                "queryPerformers": {
                    "count": 10,
                    "performers": [
                        {"id": "diag-1", "name": "Diag One", "aliases": []},
                        {"id": "diag-2", "name": "Diag Two", "aliases": []},
                    ],
                }
            }
        }
        people, _ = sync_catalog(self.conn, token="t", post=lambda *a, **k: sample_page, per_page=2, max_pages=1)
        self.assertEqual(people, 2)

        meta_row = self.conn.execute(
            "SELECT value FROM meta WHERE key='performer_catalog_fansdb_synced_at'"
        ).fetchone()
        self.assertIsNone(meta_row)

    def test_failed_import_does_not_write_synced_timestamp(self):
        def failing_post(payload, token=None):
            if payload["variables"]["page"] == 1:
                return {
                    "data": {
                        "queryPerformers": {
                            "count": 4,
                            "performers": [
                                {"id": "f1", "name": "F1", "aliases": []},
                                {"id": "f2", "name": "F2", "aliases": []},
                            ],
                        }
                    }
                }
            raise RuntimeError("Network failure on page 2")

        with self.assertRaises(RuntimeError):
            sync_catalog(self.conn, token="t", post=failing_post, per_page=2, pause=0)

        meta_row = self.conn.execute(
            "SELECT value FROM meta WHERE key='performer_catalog_fansdb_synced_at'"
        ).fetchone()
        self.assertIsNone(meta_row)


class TestFansdbCatalogGatingUnit(unittest.TestCase):
    def test_seconds_jitter_does_not_skip_following_day(self):
        # Scheduled run yesterday at 03:00:05 Warsaw time
        # Scheduled run today at 03:00:01 Warsaw time (diff: 86396s < 86400s)
        from datetime import datetime
        last_dt = datetime(2026, 11, 20, 3, 0, 5, tzinfo=PROJECT_TIMEZONE)
        now_dt = datetime(2026, 11, 21, 3, 0, 1, tzinfo=PROJECT_TIMEZONE)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (str(int(last_dt.timestamp())),)

        self.assertTrue(is_catalog_due(mock_conn, now=now_dt.timestamp()))

    def test_repeat_same_day_is_prevented(self):
        from datetime import datetime
        last_dt = datetime(2026, 11, 21, 3, 0, 1, tzinfo=PROJECT_TIMEZONE)
        now_dt = datetime(2026, 11, 21, 3, 15, 0, tzinfo=PROJECT_TIMEZONE)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (str(int(last_dt.timestamp())),)

        self.assertFalse(is_catalog_due(mock_conn, now=now_dt.timestamp()))

    def test_dst_spring_forward_does_not_skip(self):
        # In Europe/Warsaw, March 29, 2026: clocks jump from 02:00 to 03:00 (23h day)
        from datetime import datetime
        last_dt = datetime(2026, 3, 28, 3, 30, 0, tzinfo=PROJECT_TIMEZONE)
        now_dt = datetime(2026, 3, 29, 3, 30, 0, tzinfo=PROJECT_TIMEZONE)
        self.assertEqual(now_dt.timestamp() - last_dt.timestamp(), 82800.0)

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (str(int(last_dt.timestamp())),)

        self.assertTrue(is_catalog_due(mock_conn, now=now_dt.timestamp()))

    def test_dst_fall_back_does_not_skip(self):
        # In Europe/Warsaw, October 25, 2026: clocks jump back 03:00 to 02:00 (25h day)
        from datetime import datetime
        last_dt = datetime(2026, 10, 24, 3, 30, 0, tzinfo=PROJECT_TIMEZONE)
        now_dt = datetime(2026, 10, 25, 3, 30, 0, tzinfo=PROJECT_TIMEZONE)
        self.assertEqual(now_dt.timestamp() - last_dt.timestamp(), 90000.0)

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (str(int(last_dt.timestamp())),)

        self.assertTrue(is_catalog_due(mock_conn, now=now_dt.timestamp()))

    def test_manual_job_and_daily_gating_interaction(self):
        from datetime import datetime
        mock_conn = MagicMock()
        meta_storage = {}

        def mock_execute(query, params=None):
            m = MagicMock()
            if "SELECT value FROM meta" in query:
                val = meta_storage.get(params[0]) if params else None
                m.fetchone.return_value = (val,) if val is not None else None
            elif "INSERT INTO meta" in query:
                meta_storage[params[0]] = str(params[1])
            return m

        mock_conn.execute.side_effect = mock_execute

        sample_page = {
            "data": {"queryPerformers": {"count": 1, "performers": [{"id": "m1", "name": "Star", "aliases": []}]}}
        }

        with patch("sync.fansdb_catalog_sync._post", return_value=sample_page), \
             patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}):
            # 1. Manual job runs sync_catalog
            run_time = datetime(2026, 11, 21, 14, 0, 0, tzinfo=PROJECT_TIMEZONE)
            with patch("time.time", return_value=run_time.timestamp()):
                res = sync_catalog(mock_conn, token="test-key")
                self.assertEqual(res, (1, 1))

            # Meta timestamp was updated
            self.assertEqual(meta_storage.get(KEY_SYNCED_AT), str(int(run_time.timestamp())))

            # 2. Later that same day, scheduled sync_catalog_if_due runs
            later_today = datetime(2026, 11, 21, 16, 0, 0, tzinfo=PROJECT_TIMEZONE)
            due_res = sync_catalog_if_due(mock_conn, now=later_today.timestamp())
            self.assertIsNone(due_res)

            # 3. Next day, scheduled sync_catalog_if_due runs and is due
            next_day = datetime(2026, 11, 22, 3, 0, 0, tzinfo=PROJECT_TIMEZONE)
            with patch("time.time", return_value=next_day.timestamp()):
                next_res = sync_catalog_if_due(mock_conn, now=next_day.timestamp())
                self.assertEqual(next_res, (1, 1))

    def test_stage_skipped_distinguished_from_successful_import(self):
        from sync.sync_orchestration import _StageRecorder, _run_fansdb_catalog_stage

        # A) Skipped due to not due (sync_fansdb_catalog_if_due returns None)
        recorder = _StageRecorder()
        recorder.open = MagicMock()
        recorder.close = MagicMock()
        mock_conn = MagicMock()

        with patch("sync.fansdb_catalog_sync.sync_fansdb_catalog_if_due", return_value=None):
            _run_fansdb_catalog_stage(recorder, mock_conn)
            recorder.open.assert_not_called()
            recorder.close.assert_not_called()

        # B) Successful catalog import (returns (10, 5))
        recorder = _StageRecorder()
        recorder.open = MagicMock(return_value=True)
        recorder.close = MagicMock()

        with patch("sync.fansdb_catalog_sync.sync_fansdb_catalog_if_due", return_value=(10, 5)):
            _run_fansdb_catalog_stage(recorder, mock_conn)
            recorder.open.assert_called_once_with("fansdb-performer-catalog")
            recorder.close.assert_called_once_with("fansdb-performer-catalog", [])

        # C) Error on fetch (raises exception)
        recorder = _StageRecorder()
        recorder.open = MagicMock(return_value=True)
        recorder.close = MagicMock()

        with patch("sync.fansdb_catalog_sync.sync_fansdb_catalog_if_due", side_effect=RuntimeError("FansDB timeout")):
            _run_fansdb_catalog_stage(recorder, mock_conn)
            recorder.open.assert_called_once_with("fansdb-performer-catalog")
            recorder.close.assert_called_once_with("fansdb-performer-catalog", ["RuntimeError: FansDB timeout"])
