"""Tests for multi-account Telegram storage, discovery, and channel access tracking."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from resolver.providers.telegram_account import validate_account_id
from resolver.providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    _MEMORY_CHANNEL_ACCESS,
    TelegramAccountRecord,
    create_account,
    delete_account,
    get_accessible_accounts_for_channel,
    get_account,
    list_channel_access,
    set_channel_access,
    set_memory_only_mode,
    update_account,
    upsert_account,
)


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


if __name__ == "__main__":
    unittest.main()
