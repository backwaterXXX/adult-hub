"""Tests for Telegram account datetime normalization, timestamp adaptation, and write connections."""

from contextlib import contextmanager
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

from common import db
from resolver.providers.telegram_account_store import (
    _normalize_datetime,
    _write_connection,
    create_account,
    delete_account,
    get_account,
    list_accounts,
    list_channel_access,
    set_channel_access,
    update_account,
    upsert_account,
)
from tests.test_telegram_account_store import BaseTelegramAccountStoreTestCase


class TestTelegramAccountStoreTimestamps(BaseTelegramAccountStoreTestCase):
    def test_db_create_account_uses_epoch_seconds_and_adapts_to_timestamp(self):
        raw_conn = MagicMock()
        raw_cursor = MagicMock()
        raw_conn.cursor.return_value = raw_cursor
        desc_col = MagicMock()
        desc_col.name = "account_id"
        raw_cursor.description = [desc_col]
        raw_cursor.fetchone.return_value = ("acc_db_create",)

        recording_conn = db.Connection(raw_conn)
        rec = create_account("acc_db_create", label="DB Create Account", conn=recording_conn)

        self.assertIsNotNone(rec)
        self.assertIsInstance(rec.created_at, datetime)
        self.assertIsInstance(rec.updated_at, datetime)

        raw_cursor.execute.assert_called_once()
        sql, params = raw_cursor.execute.call_args[0]

        # Assert the adapted INSERT SQL uses to_timestamp(%s) for timestamp fields
        self.assertIn("to_timestamp(%s)", sql)
        self.assertEqual(sql.count("to_timestamp(%s)"), 2)

        # Assert the timestamp parameters passed to DB are numeric epoch seconds, not datetime objects
        created_at_param = params[-2]
        updated_at_param = params[-1]
        self.assertIsInstance(created_at_param, (int, float))
        self.assertIsInstance(updated_at_param, (int, float))
        self.assertNotIsInstance(created_at_param, datetime)
        self.assertNotIsInstance(updated_at_param, datetime)
        self.assertGreater(created_at_param, 0)
        self.assertGreater(updated_at_param, 0)


    def test_db_upsert_account_uses_epoch_seconds_and_adapts_to_timestamp(self):
        raw_conn = MagicMock()
        raw_cursor = MagicMock()
        raw_conn.cursor.return_value = raw_cursor
        desc_col = MagicMock()
        desc_col.name = "account_id"
        raw_cursor.description = [desc_col]
        raw_cursor.fetchone.return_value = None

        recording_conn = db.Connection(raw_conn)
        rec = upsert_account("acc_db_upsert", label="DB Upsert Account", conn=recording_conn)

        self.assertIsNotNone(rec)
        self.assertIsInstance(rec.created_at, datetime)
        self.assertIsInstance(rec.updated_at, datetime)

        # First call is SELECT to check existing account; second call is INSERT ... ON CONFLICT
        self.assertEqual(raw_cursor.execute.call_count, 2)
        sql, params = raw_cursor.execute.call_args_list[1][0]

        # Assert the adapted INSERT SQL uses to_timestamp(%s) for timestamp fields
        self.assertIn("to_timestamp(%s)", sql)
        self.assertEqual(sql.count("to_timestamp(%s)"), 2)

        # Assert the timestamp parameters passed to DB are numeric epoch seconds, not datetime objects
        created_at_param = params[-2]
        updated_at_param = params[-1]
        self.assertIsInstance(created_at_param, (int, float))
        self.assertIsInstance(updated_at_param, (int, float))
        self.assertNotIsInstance(created_at_param, datetime)
        self.assertNotIsInstance(updated_at_param, datetime)
        self.assertGreater(created_at_param, 0)
        self.assertGreater(updated_at_param, 0)


    def test_db_update_account_uses_epoch_seconds_and_adapts_to_timestamp(self):
        raw_conn = MagicMock()
        raw_cursor = MagicMock()
        raw_conn.cursor.return_value = raw_cursor

        cols = ["account_id", "label", "phone_mask", "enabled", "session_file", "created_at", "updated_at"]
        col_mocks = []
        for c in cols:
            m = MagicMock()
            m.name = c
            col_mocks.append(m)
        raw_cursor.description = col_mocks

        # First fetchone: UPDATE ... RETURNING account_id -> ("acc_db_update",)
        # Second fetchone: get_account SELECT query -> full account tuple
        raw_cursor.fetchone.side_effect = [
            ("acc_db_update",),
            ("acc_db_update", "Updated Label", "+48 111", True, "/tmp/session", 1700000000, 1700003600),
        ]

        recording_conn = db.Connection(raw_conn)
        rec = update_account("acc_db_update", label="Updated Label", conn=recording_conn)

        self.assertIsNotNone(rec)
        self.assertIsInstance(rec.created_at, datetime)
        self.assertIsInstance(rec.updated_at, datetime)
        self.assertEqual(rec.account_id, "acc_db_update")
        self.assertEqual(rec.label, "Updated Label")

        self.assertEqual(raw_cursor.execute.call_count, 2)
        sql, params = raw_cursor.execute.call_args_list[0][0]

        # Assert the adapted UPDATE SQL uses to_timestamp(%s) for updated_at
        self.assertIn("updated_at = to_timestamp(%s)", sql)

        # Assert the updated_at parameter passed to DB is numeric epoch seconds, not a datetime object
        updated_at_param = params[0]
        self.assertIsInstance(updated_at_param, (int, float))
        self.assertNotIsInstance(updated_at_param, datetime)
        self.assertGreater(updated_at_param, 0)


    def test_normalize_datetime_helper(self):
        self.assertIsNone(_normalize_datetime(None))

        dt = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_normalize_datetime(dt), dt)

        epoch_int = 1700000000
        expected = datetime.fromtimestamp(epoch_int, timezone.utc)
        self.assertEqual(_normalize_datetime(epoch_int), expected)

        epoch_float = 1700000000.5
        expected_float = datetime.fromtimestamp(epoch_float, timezone.utc)
        self.assertEqual(_normalize_datetime(epoch_float), expected_float)

        self.assertIsNone(_normalize_datetime(True))
        self.assertIsNone(_normalize_datetime(False))
        self.assertIsNone(_normalize_datetime("not-a-timestamp"))


    def test_get_account_normalizes_epoch_integers_to_datetime(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.execute.return_value = mock_cursor
        mock_cursor.fetchone.return_value = (
            "acc_mock_get",
            "Mock Get Label",
            "+48 111 222 333",
            True,
            "/tmp/mock_get.session",
            1700000000,
            1700003600,
        )

        rec = get_account("acc_mock_get", conn=mock_conn)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.account_id, "acc_mock_get")
        self.assertEqual(rec.label, "Mock Get Label")
        self.assertEqual(rec.phone_mask, "+48 111 222 333")
        self.assertTrue(rec.enabled)
        self.assertEqual(rec.session_file, "/tmp/mock_get.session")

        self.assertIsInstance(rec.created_at, datetime)
        self.assertEqual(rec.created_at, datetime.fromtimestamp(1700000000, timezone.utc))
        self.assertIsInstance(rec.updated_at, datetime)
        self.assertEqual(rec.updated_at, datetime.fromtimestamp(1700003600, timezone.utc))

        d = rec.to_dict()
        self.assertEqual(d["account_id"], "acc_mock_get")
        self.assertEqual(d["label"], "Mock Get Label")
        self.assertEqual(d["phone_mask"], "+48 111 222 333")
        self.assertTrue(d["enabled"])
        self.assertEqual(d["created_at"], rec.created_at.isoformat())
        self.assertEqual(d["updated_at"], rec.updated_at.isoformat())
        self.assertNotIn("session_file", d)

        d_full = rec.to_dict(include_session_file=True)
        self.assertEqual(d_full["session_file"], "/tmp/mock_get.session")


    def test_list_accounts_normalizes_epoch_integers_to_datetime(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.execute.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [
            (
                "acc_list_1",
                "List Label 1",
                "+48 100",
                True,
                "/tmp/1.session",
                1700000000,
                1700003600,
            ),
            (
                "acc_list_2",
                "List Label 2",
                "",
                False,
                "",
                1700010000,
                1700015000,
            ),
        ]

        accounts = list_accounts(conn=mock_conn)
        self.assertEqual(len(accounts), 2)

        rec1, rec2 = accounts
        self.assertEqual(rec1.account_id, "acc_list_1")
        self.assertEqual(rec1.label, "List Label 1")
        self.assertEqual(rec1.phone_mask, "+48 100")
        self.assertTrue(rec1.enabled)
        self.assertEqual(rec1.session_file, "/tmp/1.session")
        self.assertIsInstance(rec1.created_at, datetime)
        self.assertEqual(rec1.created_at, datetime.fromtimestamp(1700000000, timezone.utc))
        self.assertIsInstance(rec1.updated_at, datetime)
        self.assertEqual(rec1.updated_at, datetime.fromtimestamp(1700003600, timezone.utc))
        d1 = rec1.to_dict()
        self.assertEqual(d1["created_at"], rec1.created_at.isoformat())
        self.assertEqual(d1["updated_at"], rec1.updated_at.isoformat())

        self.assertEqual(rec2.account_id, "acc_list_2")
        self.assertEqual(rec2.label, "List Label 2")
        self.assertEqual(rec2.phone_mask, "")
        self.assertFalse(rec2.enabled)
        self.assertEqual(rec2.session_file, "")
        self.assertIsInstance(rec2.created_at, datetime)
        self.assertEqual(rec2.created_at, datetime.fromtimestamp(1700010000, timezone.utc))
        self.assertIsInstance(rec2.updated_at, datetime)
        self.assertEqual(rec2.updated_at, datetime.fromtimestamp(1700015000, timezone.utc))
        d2 = rec2.to_dict()
        self.assertEqual(d2["created_at"], rec2.created_at.isoformat())
        self.assertEqual(d2["updated_at"], rec2.updated_at.isoformat())


    def test_list_channel_access_normalizes_epoch_integers_to_datetime(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.execute.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [
            ("acc_1", "-100123", "accessible", 1700002000, ""),
            ("acc_2", "-100123", "temporary_error", 1700004000, "Timeout"),
        ]

        access_records = list_channel_access(channel_id="-100123", conn=mock_conn)
        self.assertEqual(len(access_records), 2)

        rec1, rec2 = access_records
        self.assertEqual(rec1.account_id, "acc_1")
        self.assertEqual(rec1.channel_id, "-100123")
        self.assertEqual(rec1.access_status, "accessible")
        self.assertEqual(rec1.error_detail, "")
        self.assertIsInstance(rec1.last_checked_at, datetime)
        self.assertEqual(rec1.last_checked_at, datetime.fromtimestamp(1700002000, timezone.utc))
        d1 = rec1.to_dict()
        self.assertEqual(d1["last_checked_at"], rec1.last_checked_at.isoformat())

        self.assertEqual(rec2.account_id, "acc_2")
        self.assertEqual(rec2.channel_id, "-100123")
        self.assertEqual(rec2.access_status, "temporary_error")
        self.assertEqual(rec2.error_detail, "Timeout")
        self.assertIsInstance(rec2.last_checked_at, datetime)
        self.assertEqual(rec2.last_checked_at, datetime.fromtimestamp(1700004000, timezone.utc))
        d2 = rec2.to_dict()
        self.assertEqual(d2["last_checked_at"], rec2.last_checked_at.isoformat())


    def test_upsert_account_preserves_datetime_from_existing_db_row(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.execute.return_value = mock_cursor
        # When get_account queries DB inside _resolve_effective_account_fields
        mock_cursor.fetchone.return_value = (
            "acc_upsert_dt",
            "Existing Label",
            "+48 999",
            True,
            "/tmp/existing.session",
            1700000000,
            1700001000,
        )

        rec = upsert_account("acc_upsert_dt", label="Updated Label", conn=mock_conn)
        self.assertEqual(rec.account_id, "acc_upsert_dt")
        self.assertEqual(rec.label, "Updated Label")
        self.assertEqual(rec.phone_mask, "+48 999")
        self.assertTrue(rec.enabled)
        self.assertIsInstance(rec.created_at, datetime)
        self.assertEqual(rec.created_at, datetime.fromtimestamp(1700000000, timezone.utc))
        self.assertIsInstance(rec.updated_at, datetime)

        d = rec.to_dict()
        self.assertEqual(d["created_at"], rec.created_at.isoformat())
        self.assertEqual(d["updated_at"], rec.updated_at.isoformat())


    def test_write_connection_commits_and_closes_on_success(self):
        events: list[str] = []

        class TrackingRawConn:
            closed = False

            def commit(self):
                events.append("commit")

            def rollback(self):
                events.append("rollback")

            def close(self):
                events.append("close")

        fake_conn = db.Connection(TrackingRawConn())

        @contextmanager
        def fake_db_connection(connect_timeout=3):
            try:
                yield fake_conn
            finally:
                fake_conn.close()

        with patch("resolver.providers.telegram_account_store.db.connection", fake_db_connection):
            with _write_connection() as c:
                events.append("body")
                self.assertIs(c, fake_conn)

        self.assertEqual(events, ["body", "commit", "close"])


    def test_write_connection_rolls_back_and_closes_on_error(self):
        events: list[str] = []

        class TrackingRawConn:
            closed = False

            def commit(self):
                events.append("commit")

            def rollback(self):
                events.append("rollback")

            def close(self):
                events.append("close")

        fake_conn = db.Connection(TrackingRawConn())

        @contextmanager
        def fake_db_connection(connect_timeout=3):
            try:
                yield fake_conn
            finally:
                fake_conn.close()

        with patch("resolver.providers.telegram_account_store.db.connection", fake_db_connection):
            with self.assertRaises(RuntimeError) as ctx:
                with _write_connection():
                    events.append("body")
                    raise RuntimeError("simulated write failure")

        self.assertEqual(str(ctx.exception), "simulated write failure")
        self.assertEqual(events, ["body", "rollback", "close"])


    def test_caller_provided_connection_is_not_committed(self):
        class TrackingProvidedConn:
            def __init__(self):
                self.calls: list[str] = []

            def execute(self, sql, params=None):
                self.calls.append("execute")
                mock_cursor = MagicMock()
                mock_cursor.fetchone.return_value = (
                    "acc_provided", "Provided Account", "", True, "", 1700000000, 1700000000
                )
                return mock_cursor

            def commit(self):
                self.calls.append("commit")

            def rollback(self):
                self.calls.append("rollback")

            def close(self):
                self.calls.append("close")

            def __enter__(self):
                self.calls.append("enter")
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                self.calls.append("exit")

        operations = [
            ("create_account", lambda c: create_account("acc_p1", label="P1", conn=c)),
            ("upsert_account", lambda c: upsert_account("acc_p2", label="P2", conn=c)),
            ("update_account", lambda c: update_account("acc_p3", label="P3", conn=c)),
            ("delete_account", lambda c: delete_account("acc_p4", conn=c)),
            (
                "set_channel_access",
                lambda c: set_channel_access("acc_p5", "-100", "accessible", conn=c),
            ),
        ]
        for action, fn in operations:
            provided = TrackingProvidedConn()
            fn(provided)
            self.assertIn("execute", provided.calls, f"execute was not called for {action}")
            self.assertNotIn("commit", provided.calls, f"commit was called for {action}")
            self.assertNotIn("rollback", provided.calls, f"rollback was called for {action}")
            self.assertNotIn("close", provided.calls, f"close was called for {action}")
            self.assertNotIn("enter", provided.calls, f"enter was called for {action}")
            self.assertNotIn("exit", provided.calls, f"exit was called for {action}")



if __name__ == "__main__":
    unittest.main()
