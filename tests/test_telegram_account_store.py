"""Tests for multi-account Telegram storage, discovery, and channel access tracking."""

import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from common import db
from resolver.providers.telegram_account import validate_account_id
from resolver.providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    _MEMORY_CHANNEL_ACCESS,
    _normalize_datetime,
    _write_connection,
    TelegramAccountRecord,
    create_account,
    delete_account,
    get_accessible_accounts_for_channel,
    get_account,
    list_accounts,
    list_channel_access,
    set_channel_access,
    set_memory_only_mode,
    update_account,
    upsert_account,
)


class _FakeAccountStoreCursor:
    def __init__(self, rows):
        self._rows = list(rows)
        self._idx = 0

    def fetchone(self):
        if self._idx < len(self._rows):
            row = self._rows[self._idx]
            self._idx += 1
            return row
        return None

    def fetchall(self):
        rows = self._rows[self._idx:]
        self._idx = len(self._rows)
        return rows


class _FakeAccountStoreDb:
    def __init__(self):
        # account_id -> (account_id, label, phone_mask, enabled, session_file, created_at, updated_at)
        self.committed_accounts: dict[str, tuple] = {}
        # (account_id, channel_id) -> (account_id, channel_id, access_status, last_checked_at, error_detail)
        self.committed_channel_access: dict[tuple[str, str], tuple] = {}

    @property
    def committed_rows(self) -> dict[str, tuple]:
        return self.committed_accounts


class _FakeAccountStoreConn:
    def __init__(self, fake_db: _FakeAccountStoreDb, *, fail_commit: bool = False):
        self._db = fake_db
        self.fail_commit = fail_commit
        self._uncommitted_accounts: dict[str, tuple] = dict(fake_db.committed_accounts)
        self._uncommitted_channel_access: dict[tuple[str, str], tuple] = dict(
            fake_db.committed_channel_access
        )
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def commit(self):
        if self.fail_commit:
            self.rollback()
            raise RuntimeError("Simulated DB commit failure")
        self._db.committed_accounts = dict(self._uncommitted_accounts)
        self._db.committed_channel_access = dict(self._uncommitted_channel_access)

    def rollback(self):
        self._uncommitted_accounts = dict(self._db.committed_accounts)
        self._uncommitted_channel_access = dict(self._db.committed_channel_access)

    def close(self):
        self.closed = True
        self.rollback()

    def _exec_insert_account(
        self, params_list: list[Any], sql_norm: str
    ) -> _FakeAccountStoreCursor:
        account_id = params_list[0]
        if "on conflict (account_id) do nothing" in sql_norm:
            if account_id in self._uncommitted_accounts:
                return _FakeAccountStoreCursor([])
            row = (
                params_list[0],
                params_list[1],
                "",
                params_list[2],
                params_list[3],
                params_list[4],
                params_list[5],
            )
        else:
            row = tuple(params_list)
        self._uncommitted_accounts[account_id] = row
        return _FakeAccountStoreCursor([(account_id,)])

    def _exec_update_account(
        self, params_list: list[Any], sql_norm: str
    ) -> _FakeAccountStoreCursor:
        account_id = params_list[-1]
        existing = self._uncommitted_accounts.get(account_id)
        if not existing:
            return _FakeAccountStoreCursor([])
        new_updated_at = params_list[0]
        new_label = existing[1]
        new_phone = existing[2]
        new_enabled = existing[3]
        idx = 1
        if "label = %s" in sql_norm:
            new_label = params_list[idx]
            idx += 1
        if "enabled = %s" in sql_norm:
            new_enabled = params_list[idx]
            idx += 1
        if "phone_mask = %s" in sql_norm:
            new_phone = params_list[idx]
            idx += 1
        updated_row = (
            account_id,
            new_label,
            new_phone,
            new_enabled,
            existing[4],
            existing[5],
            new_updated_at,
        )
        self._uncommitted_accounts[account_id] = updated_row
        return _FakeAccountStoreCursor([(account_id,)])

    def _exec_select_accessible(self, channel_id: str) -> _FakeAccountStoreCursor:
        matched = []
        for (aid, cid), access_row in sorted(self._uncommitted_channel_access.items()):
            if cid == channel_id and access_row[2] == "accessible":
                acc = self._uncommitted_accounts.get(aid)
                if acc and acc[3]:
                    matched.append((aid,))
        return _FakeAccountStoreCursor(matched)

    def _exec_select_channel_access(
        self, params_list: list[Any], sql_norm: str
    ) -> _FakeAccountStoreCursor:
        records = list(self._uncommitted_channel_access.values())
        if "where" in sql_norm:
            if "account_id = %s" in sql_norm and "channel_id = %s" in sql_norm:
                records = [
                    r for r in records if r[0] == params_list[0] and r[1] == params_list[1]
                ]
            elif "account_id = %s" in sql_norm:
                records = [r for r in records if r[0] == params_list[0]]
            elif "channel_id = %s" in sql_norm:
                records = [r for r in records if r[1] == params_list[0]]
        records.sort(key=lambda r: (r[1], r[0]))
        return _FakeAccountStoreCursor(records)

    def _exec_select_accounts(
        self, params_list: list[Any], sql_norm: str
    ) -> _FakeAccountStoreCursor:
        if "where account_id = %s" in sql_norm:
            account_id = params_list[0]
            row = self._uncommitted_accounts.get(account_id)
            return _FakeAccountStoreCursor([row] if row else [])
        all_ids = sorted(self._uncommitted_accounts.keys())
        rows = [self._uncommitted_accounts[aid] for aid in all_ids]
        return _FakeAccountStoreCursor(rows)

    def execute(self, sql: str, params=None):
        sql_norm = " ".join(sql.strip().lower().split())
        params_list = list(params or ())

        if "pg_tables" in sql_norm or "information_schema" in sql_norm:
            return _FakeAccountStoreCursor([(True,)])
        if sql_norm.startswith("insert into public.telegram_accounts"):
            return self._exec_insert_account(params_list, sql_norm)
        if sql_norm.startswith("update public.telegram_accounts"):
            return self._exec_update_account(params_list, sql_norm)
        if sql_norm.startswith("delete from public.telegram_accounts"):
            account_id = params_list[0]
            existed = self._uncommitted_accounts.pop(account_id, None)
            return _FakeAccountStoreCursor([(account_id,)] if existed else [])
        if sql_norm.startswith("insert into public.telegram_account_channels"):
            account_id, channel_id, status, last_checked, error_detail = params_list
            row = (account_id, channel_id, status, last_checked, error_detail)
            self._uncommitted_channel_access[(account_id, channel_id)] = row
            return _FakeAccountStoreCursor([(account_id, channel_id)])
        if sql_norm.startswith("select") and "join public.telegram_account_channels" in sql_norm:
            return self._exec_select_accessible(params_list[0])
        if sql_norm.startswith("select") and "from public.telegram_account_channels" in sql_norm:
            return self._exec_select_channel_access(params_list, sql_norm)
        if sql_norm.startswith("select") and "from public.telegram_accounts" in sql_norm:
            return self._exec_select_accounts(params_list, sql_norm)

        return _FakeAccountStoreCursor([])


class TestTelegramAccountStore(unittest.TestCase):
    def setUp(self):
        _MEMORY_ACCOUNTS.clear()
        _MEMORY_CHANNEL_ACCESS.clear()
        set_memory_only_mode(True)

    def tearDown(self):
        set_memory_only_mode(False)

    def test_validate_account_id_rules(self):
        self.assertEqual(validate_account_id("default"), "default")
        self.assertEqual(validate_account_id("bot_1"), "bot_1")
        self.assertEqual(validate_account_id("account-2"), "account-2")

        with self.assertRaises(ValueError):
            validate_account_id("")
        with self.assertRaises(ValueError):
            validate_account_id("../evil")
        with self.assertRaises(ValueError):
            validate_account_id("bad/path")
        with self.assertRaises(ValueError):
            validate_account_id("bad;id")
        with self.assertRaises(ValueError):
            validate_account_id("a" * 65)

    def test_upsert_and_get_account(self):
        rec = upsert_account("worker1", label="Backup Worker", phone_mask="+48 123 *** 789", enabled=True)
        self.assertEqual(rec.account_id, "worker1")
        self.assertEqual(rec.label, "Backup Worker")
        self.assertEqual(rec.phone_mask, "+48 123 *** 789")
        self.assertTrue(rec.enabled)

        fetched = get_account("worker1")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.account_id, "worker1")
        self.assertEqual(fetched.label, "Backup Worker")

    def test_update_account(self):
        upsert_account("worker2", label="Initial Name", enabled=True)
        updated = update_account("worker2", label="Renamed Worker", enabled=False)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.label, "Renamed Worker")
        self.assertFalse(updated.enabled)

    def test_channel_access_mapping(self):
        upsert_account("acc1", label="Account 1", enabled=True)
        upsert_account("acc2", label="Account 2", enabled=True)

        set_channel_access("acc1", "-100123456", "accessible")
        set_channel_access("acc2", "-100123456", "inaccessible", error_detail="ChannelPrivateError")

        access_records = list_channel_access(channel_id="-100123456")
        self.assertEqual(len(access_records), 2)

        accessible = get_accessible_accounts_for_channel("-100123456")
        self.assertEqual(accessible, ["acc1"])

    def test_delete_account_removes_session_file(self):
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            session_file = f.name
            f.write(b"mock_sqlite_data")

        self.assertTrue(os.path.isfile(session_file))
        upsert_account("to_delete", session_file=session_file)

        self.assertTrue(delete_account("to_delete"))
        self.assertIsNone(get_account("to_delete"))
        self.assertFalse(os.path.isfile(session_file))

    def test_delete_account_db_failure_preserves_session_file(self):
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            session_file = f.name
            f.write(b"precious_session_data")

        try:
            upsert_account("fail_delete", session_file=session_file, enabled=True)

            class BrokenConn:
                def execute(self, sql, params=None):
                    raise RuntimeError("Simulated DB connection drop")

            broken_conn = BrokenConn()
            with self.assertRaises(RuntimeError):
                delete_account("fail_delete", conn=broken_conn)

            # Session file MUST remain untouched on disk!
            self.assertTrue(os.path.isfile(session_file))
            with open(session_file, "rb") as f:
                self.assertEqual(f.read(), b"precious_session_data")
        finally:
            if os.path.isfile(session_file):
                os.remove(session_file)

    def test_relogin_preserves_custom_label_and_disabled_state(self):
        # User customized label and explicitly disabled the account
        upsert_account("acc_relogin", label="My Custom Work Bot", enabled=False)

        # Login manager finalization calls upsert_account without label/enabled
        upsert_account("acc_relogin", phone_mask="+48 999 *** 000")

        acc = get_account("acc_relogin")
        self.assertEqual(acc.label, "My Custom Work Bot")
        self.assertFalse(acc.enabled)
        self.assertEqual(acc.phone_mask, "+48 999 *** 000")

    def test_atomic_create_account_duplicate_conflict(self):
        created = create_account("acc_atomic", label="First Creation", enabled=True)
        self.assertIsNotNone(created)
        self.assertEqual(created.account_id, "acc_atomic")

        # Duplicate create returns None without modifying existing row
        dup = create_account("acc_atomic", label="Attempted Overwrite", enabled=False)
        self.assertIsNone(dup)

        acc = get_account("acc_atomic")
        self.assertEqual(acc.label, "First Creation")
        self.assertTrue(acc.enabled)

    def test_to_dict_omits_session_file_by_default(self):
        rec = upsert_account("acc_privacy", session_file="/private/server/data/acc.session")
        d = rec.to_dict()
        self.assertNotIn("session_file", d)
        self.assertEqual(d["account_id"], "acc_privacy")

        d_internal = rec.to_dict(include_session_file=True)
        self.assertIn("session_file", d_internal)
        self.assertEqual(d_internal["session_file"], "/private/server/data/acc.session")

    def test_db_unavailable_delete_account_refuses_and_does_not_unlink(self):
        set_memory_only_mode(False)
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            session_file = f.name
            f.write(b"precious_data")

        try:
            _MEMORY_ACCOUNTS["acc_db_fail"] = TelegramAccountRecord(
                account_id="acc_db_fail",
                session_file=session_file,
            )
            with patch("resolver.providers.telegram_account_store._is_db_available", return_value=False):
                with self.assertRaises(RuntimeError) as ctx:
                    delete_account("acc_db_fail")
                self.assertIn("Database is unavailable", str(ctx.exception))

            # Session file MUST NOT be unlinked on DB failure
            self.assertTrue(os.path.isfile(session_file))
            # Account record must not be deleted from memory
            self.assertIn("acc_db_fail", _MEMORY_ACCOUNTS)
        finally:
            if os.path.isfile(session_file):
                os.remove(session_file)

    def test_db_unavailable_create_account_fails_without_memory_mutation(self):
        set_memory_only_mode(False)
        with patch("resolver.providers.telegram_account_store._is_db_available", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                create_account("acc_fail_create", label="Fail Create")
            self.assertIn("Database is unavailable", str(ctx.exception))
        self.assertNotIn("acc_fail_create", _MEMORY_ACCOUNTS)

    def test_db_unavailable_upsert_account_fails_without_memory_mutation(self):
        set_memory_only_mode(False)
        with patch("resolver.providers.telegram_account_store._is_db_available", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                upsert_account("acc_fail_upsert", label="Fail Upsert")
            self.assertIn("Database is unavailable", str(ctx.exception))
        self.assertNotIn("acc_fail_upsert", _MEMORY_ACCOUNTS)

    def test_db_unavailable_update_account_fails_without_memory_mutation(self):
        set_memory_only_mode(False)
        with patch("resolver.providers.telegram_account_store._is_db_available", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                update_account("acc_fail_update", label="Fail Update")
            self.assertIn("Database is unavailable", str(ctx.exception))
        self.assertNotIn("acc_fail_update", _MEMORY_ACCOUNTS)

    def test_delete_account_refuses_when_client_is_still_connected(self):
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            session_file = f.name
            f.write(b"data")

        try:
            upsert_account("acc_connected", session_file=session_file)
            mock_client = MagicMock()
            mock_client.is_connected = MagicMock(return_value=True)

            mock_router = MagicMock()
            mock_router._clients = {"acc_connected": mock_client}

            with patch("resolver.providers.telegram_router.get_telegram_router", return_value=mock_router):
                with self.assertRaises(RuntimeError) as ctx:
                    delete_account("acc_connected")
                self.assertIn("client is still connected", str(ctx.exception))

            # Must NOT delete session file
            self.assertTrue(os.path.isfile(session_file))
            self.assertIn("acc_connected", _MEMORY_ACCOUNTS)
        finally:
            if os.path.isfile(session_file):
                os.remove(session_file)

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

    def test_legacy_auto_registration_persists_across_calls_without_re_upserting(self):
        set_memory_only_mode(False)
        fake_db = _FakeAccountStoreDb()

        @contextmanager
        def fake_db_connection(connect_timeout=3):
            conn = _FakeAccountStoreConn(fake_db)
            try:
                yield conn
            finally:
                conn.close()

        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            session_file = f.name
            f.write(b"mock_legacy_session_bytes")

        try:
            with (
                patch(
                    "resolver.providers.telegram_account_store.db.connection",
                    fake_db_connection,
                ),
                patch(
                    "resolver.providers.telegram_account.TelegramAccountContext.get_session_path",
                    return_value=session_file,
                ),
            ):
                # 1. First read: table is empty, auto-registers legacy session and commits to DB
                first_read = list_accounts()
                self.assertEqual(len(first_read), 1)
                self.assertEqual(first_read[0].account_id, "default")
                self.assertEqual(first_read[0].session_file, session_file)
                self.assertIn("default", fake_db.committed_rows)

                # Clear in-memory fallback cache to ensure second read must query DB table
                _MEMORY_ACCOUNTS.clear()

                # 2. Second read: table read returns the persisted committed row,
                # normal table read succeeds and upsert_account is NOT invoked
                with patch(
                    "resolver.providers.telegram_account_store.upsert_account",
                    wraps=upsert_account,
                ) as spy_upsert:
                    second_read = list_accounts()
                    spy_upsert.assert_not_called()

                self.assertEqual(len(second_read), 1)
                self.assertEqual(second_read[0].account_id, "default")
                self.assertEqual(second_read[0].label, "Default Account")
                self.assertEqual(second_read[0].session_file, session_file)
        finally:
            if os.path.isfile(session_file):
                os.remove(session_file)

    def test_update_account_commit_failure_raises_and_preserves_memory_state(self):
        set_memory_only_mode(False)
        fake_db = _FakeAccountStoreDb()
        now_ts = datetime.now(timezone.utc).timestamp()
        fake_db.committed_accounts["acc_up_fail"] = (
            "acc_up_fail",
            "Initial Label",
            "+48 100",
            True,
            "/tmp/up_fail.session",
            now_ts,
            now_ts,
        )
        _MEMORY_ACCOUNTS["acc_up_fail"] = TelegramAccountRecord(
            account_id="acc_up_fail",
            label="Initial Label",
            phone_mask="+48 100",
            enabled=True,
            session_file="/tmp/up_fail.session",
        )

        @contextmanager
        def fake_db_connection_fail_commit(connect_timeout=3):
            conn = _FakeAccountStoreConn(fake_db, fail_commit=True)
            try:
                yield conn
            finally:
                conn.close()

        with patch(
            "resolver.providers.telegram_account_store.db.connection",
            fake_db_connection_fail_commit,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                update_account("acc_up_fail", label="Mutated Label", enabled=False)
            self.assertIn("Simulated DB commit failure", str(ctx.exception))

        # Memory cache must NOT have been updated
        mem = _MEMORY_ACCOUNTS["acc_up_fail"]
        self.assertEqual(mem.label, "Initial Label")
        self.assertTrue(mem.enabled)

        # Committed fake DB row must NOT have been updated
        db_row = fake_db.committed_accounts["acc_up_fail"]
        self.assertEqual(db_row[1], "Initial Label")
        self.assertTrue(db_row[3])

    def test_delete_account_commit_failure_raises_and_preserves_session_file_and_memory(self):
        set_memory_only_mode(False)
        fake_db = _FakeAccountStoreDb()

        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            session_file = f.name
            f.write(b"uncommitted_delete_protected_bytes")

        now_ts = datetime.now(timezone.utc).timestamp()
        fake_db.committed_accounts["acc_del_fail"] = (
            "acc_del_fail",
            "Delete Fail Account",
            "",
            True,
            session_file,
            now_ts,
            now_ts,
        )
        _MEMORY_ACCOUNTS["acc_del_fail"] = TelegramAccountRecord(
            account_id="acc_del_fail",
            session_file=session_file,
        )

        @contextmanager
        def fake_db_connection_fail_commit(connect_timeout=3):
            conn = _FakeAccountStoreConn(fake_db, fail_commit=True)
            try:
                yield conn
            finally:
                conn.close()

        try:
            with patch(
                "resolver.providers.telegram_account_store.db.connection",
                fake_db_connection_fail_commit,
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    delete_account("acc_del_fail")
                self.assertIn("Simulated DB commit failure", str(ctx.exception))

            # Disk session file MUST be preserved
            self.assertTrue(os.path.isfile(session_file))
            with open(session_file, "rb") as f:
                self.assertEqual(f.read(), b"uncommitted_delete_protected_bytes")

            # Memory account MUST still exist
            self.assertIn("acc_del_fail", _MEMORY_ACCOUNTS)

            # DB row MUST still exist in committed state
            self.assertIn("acc_del_fail", fake_db.committed_accounts)
        finally:
            if os.path.isfile(session_file):
                os.remove(session_file)

    def test_create_account_commit_failure_raises_and_does_not_mutate_memory(self):
        set_memory_only_mode(False)
        fake_db = _FakeAccountStoreDb()

        @contextmanager
        def fake_db_connection_fail_commit(connect_timeout=3):
            conn = _FakeAccountStoreConn(fake_db, fail_commit=True)
            try:
                yield conn
            finally:
                conn.close()

        with patch(
            "resolver.providers.telegram_account_store.db.connection",
            fake_db_connection_fail_commit,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                create_account("acc_cr_fail", label="Create Fail")
            self.assertIn("Simulated DB commit failure", str(ctx.exception))

        self.assertNotIn("acc_cr_fail", _MEMORY_ACCOUNTS)
        self.assertNotIn("acc_cr_fail", fake_db.committed_accounts)

    def test_upsert_account_commit_failure_raises_and_does_not_mutate_memory(self):
        set_memory_only_mode(False)
        fake_db = _FakeAccountStoreDb()

        @contextmanager
        def fake_db_connection_fail_commit(connect_timeout=3):
            conn = _FakeAccountStoreConn(fake_db, fail_commit=True)
            try:
                yield conn
            finally:
                conn.close()

        with patch(
            "resolver.providers.telegram_account_store.db.connection",
            fake_db_connection_fail_commit,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                upsert_account("acc_up_fail2", label="Upsert Fail")
            self.assertIn("Simulated DB commit failure", str(ctx.exception))

        self.assertNotIn("acc_up_fail2", _MEMORY_ACCOUNTS)
        self.assertNotIn("acc_up_fail2", fake_db.committed_accounts)

    def test_fake_persistence_update_delete_and_channel_access_mutations(self):
        set_memory_only_mode(False)
        fake_db = _FakeAccountStoreDb()

        @contextmanager
        def fake_db_connection(connect_timeout=3):
            conn = _FakeAccountStoreConn(fake_db)
            try:
                yield conn
            finally:
                conn.close()

        with patch(
            "resolver.providers.telegram_account_store.db.connection",
            fake_db_connection,
        ):
            # 1. create_account: persists across connection boundaries
            created = create_account("worker_mut", label="Original Worker", enabled=True)
            self.assertIsNotNone(created)
            self.assertEqual(created.account_id, "worker_mut")
            self.assertIn("worker_mut", fake_db.committed_accounts)

            # Clear memory cache: next read must fetch from fake DB
            _MEMORY_ACCOUNTS.clear()
            fetched = get_account("worker_mut")
            self.assertIsNotNone(fetched)
            self.assertEqual(fetched.label, "Original Worker")
            self.assertTrue(fetched.enabled)

            # 2. update_account: updates persist across connection boundaries
            updated = update_account(
                "worker_mut", label="Renamed Worker", enabled=False, phone_mask="+48 123"
            )
            self.assertIsNotNone(updated)
            self.assertEqual(updated.label, "Renamed Worker")
            self.assertFalse(updated.enabled)
            self.assertEqual(updated.phone_mask, "+48 123")

            _MEMORY_ACCOUNTS.clear()
            fetched_after_update = get_account("worker_mut")
            self.assertIsNotNone(fetched_after_update)
            self.assertEqual(fetched_after_update.label, "Renamed Worker")
            self.assertFalse(fetched_after_update.enabled)
            self.assertEqual(fetched_after_update.phone_mask, "+48 123")

            # 3. set_channel_access: persists across connection boundaries
            set_channel_access("worker_mut", "-100987654", "accessible")
            _MEMORY_CHANNEL_ACCESS.clear()
            channel_records = list_channel_access(channel_id="-100987654")
            self.assertEqual(len(channel_records), 1)
            self.assertEqual(channel_records[0].account_id, "worker_mut")
            self.assertEqual(channel_records[0].access_status, "accessible")

            # worker_mut is disabled, so accessible accounts should be empty
            self.assertEqual(get_accessible_accounts_for_channel("-100987654"), [])

            # Re-enable worker_mut
            update_account("worker_mut", enabled=True)
            self.assertEqual(get_accessible_accounts_for_channel("-100987654"), ["worker_mut"])

            # 4. delete_account: persists deletion across connection boundaries
            self.assertTrue(delete_account("worker_mut"))
            self.assertNotIn("worker_mut", fake_db.committed_accounts)

            _MEMORY_ACCOUNTS.clear()
            self.assertIsNone(get_account("worker_mut"))

    def test_fake_persistence_mutation_failure_rollback(self):
        set_memory_only_mode(False)
        fake_db = _FakeAccountStoreDb()
        now_ts = datetime.now(timezone.utc).timestamp()
        fake_db.committed_accounts["acc_rb_seed"] = (
            "acc_rb_seed",
            "Committed Seed",
            "+48 000",
            True,
            "/tmp/seed.session",
            now_ts,
            now_ts,
        )

        @contextmanager
        def fake_db_connection_fail_commit(connect_timeout=3):
            conn = _FakeAccountStoreConn(fake_db, fail_commit=True)
            try:
                yield conn
            finally:
                conn.close()

        with patch(
            "resolver.providers.telegram_account_store.db.connection",
            fake_db_connection_fail_commit,
        ):
            # Attempt update with commit failure
            with self.assertRaises(RuntimeError):
                update_account("acc_rb_seed", label="Should Not Persist", enabled=False)

            # Assert rollback: committed state in fake_db is unchanged
            seed_row = fake_db.committed_accounts["acc_rb_seed"]
            self.assertEqual(seed_row[1], "Committed Seed")
            self.assertTrue(seed_row[3])

            # Attempt delete with commit failure
            with self.assertRaises(RuntimeError):
                delete_account("acc_rb_seed")

            # Assert rollback: account still exists in committed fake_db
            self.assertIn("acc_rb_seed", fake_db.committed_accounts)


if __name__ == "__main__":
    unittest.main()
