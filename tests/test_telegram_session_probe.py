"""The authorization probe must never write to the shared Telethon session file.

Two processes (resolver and sync) plus concurrent stream clients share one rollback-journal
SQLite file. A probe that opens it read-write collides with live clients and raises
OperationalError ("database is locked"), which used to make a working account look
unauthorized. These tests pin the read-only snapshot and the client it builds.
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from resolver.providers import telegram_session

try:
    import telethon  # noqa: F401
    TELETHON_AVAILABLE = True
except ImportError:  # pragma: no cover - the public suite stubs telethon for other modules
    TELETHON_AVAILABLE = False

API_ID = 123456
API_HASH = "0123456789abcdef0123456789abcdef"


class SessionFileFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "telegram.session")

    def write_session(self, auth_key=b"k" * 256):
        conn = sqlite3.connect(self.path)
        try:
            conn.execute(
                "CREATE TABLE sessions (dc_id integer primary key, server_address text, "
                "port integer, auth_key blob, takeout_id integer, tmp_auth_key blob)"
            )
            conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                (2, "149.154.167.51", 443, auth_key, None, None),
            )
            conn.commit()
        finally:
            conn.close()


class SessionSnapshotTests(SessionFileFixture):
    def test_snapshot_reads_the_session_row(self):
        self.write_session()

        snapshot = telegram_session.read_session_snapshot(self.path)

        self.assertEqual(snapshot["dc_id"], 2)
        self.assertEqual(snapshot["server_address"], "149.154.167.51")
        self.assertEqual(snapshot["port"], 443)
        self.assertEqual(len(snapshot["auth_key"]), 256)

    def test_snapshot_leaves_the_shared_file_untouched(self):
        self.write_session()
        before = (os.stat(self.path).st_mtime_ns, os.stat(self.path).st_size)

        telegram_session.read_session_snapshot(self.path)

        after = (os.stat(self.path).st_mtime_ns, os.stat(self.path).st_size)
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(f"{self.path}-journal"))

    def test_snapshot_is_none_without_a_session_row(self):
        sqlite3.connect(self.path).close()

        self.assertIsNone(telegram_session.read_session_snapshot(self.path))

    def test_snapshot_is_none_for_a_short_auth_key(self):
        self.write_session(auth_key=b"too-short")

        self.assertIsNone(telegram_session.read_session_snapshot(self.path))

    def test_snapshot_is_none_for_a_missing_file(self):
        self.assertIsNone(telegram_session.read_session_snapshot(self.path))


@unittest.skipUnless(TELETHON_AVAILABLE, "telethon is not installed")
class AuthorizationClientTests(SessionFileFixture):
    def test_probe_uses_an_in_memory_session_for_a_readable_snapshot(self):
        self.write_session()

        with patch("telethon.TelegramClient") as client_class:
            client_class.return_value = object()
            telegram_session._authorization_client(self.path, API_ID, API_HASH)

        first_argument = client_class.call_args[0][0]
        self.assertNotIsInstance(first_argument, str)
        self.assertEqual(first_argument.dc_id, 2)
        self.assertEqual(first_argument.server_address, "149.154.167.51")
        self.assertEqual(first_argument.port, 443)
        self.assertEqual(len(first_argument.auth_key.key), 256)

    def test_probe_falls_back_to_the_session_file_without_a_snapshot(self):
        sqlite3.connect(self.path).close()

        with patch("telethon.TelegramClient") as client_class:
            client_class.return_value = object()
            telegram_session._authorization_client(self.path, API_ID, API_HASH)

        self.assertEqual(client_class.call_args[0][0], self.path)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
