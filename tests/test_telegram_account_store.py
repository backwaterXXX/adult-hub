"""Tests for multi-account Telegram storage, discovery, and channel access tracking."""

from contextlib import contextmanager
import os
import tempfile
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

from resolver.providers.telegram_account import validate_account_id
from resolver.providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    _MEMORY_CHANNEL_ACCESS,
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



class BaseTelegramAccountStoreTestCase(unittest.TestCase):
    def setUp(self):
        _MEMORY_ACCOUNTS.clear()
        _MEMORY_CHANNEL_ACCESS.clear()
        set_memory_only_mode(True)


    def tearDown(self):
        set_memory_only_mode(False)



class TestTelegramAccountStore(BaseTelegramAccountStoreTestCase):
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



if __name__ == "__main__":
    unittest.main()
