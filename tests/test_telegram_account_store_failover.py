"""Tests for Telegram account database failover, commit errors, and memory consistency."""

from contextlib import contextmanager
from datetime import datetime, timezone
import os
import tempfile
import unittest
from unittest.mock import patch

from resolver.providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    TelegramAccountRecord,
    create_account,
    delete_account,
    set_memory_only_mode,
    update_account,
    upsert_account,
)
from tests.test_telegram_account_store import (
    BaseTelegramAccountStoreTestCase,
    _FakeAccountStoreConn,
    _FakeAccountStoreDb,
)


class TestTelegramAccountStoreFailover(BaseTelegramAccountStoreTestCase):
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
