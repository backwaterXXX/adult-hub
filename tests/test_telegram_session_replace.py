"""DB-free tests for replacing an unauthorized Telegram session file.

Covers the manager action and the Admin v2 route: the live check gates the move, a
login in progress blocks it, the file and its SQLite sidecars move to a timestamped
backup that is never overwritten, and a later start_login reaches the normal flow.
Uses temporary directories, the memory-only account store and mocked Telegram calls.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import resolver.admin_v2_telegram_auth as admin_v2_telegram_auth
from resolver.providers.telegram_account import TelegramAccountContext
from resolver.providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    get_account,
    set_memory_only_mode,
    upsert_account,
)
from resolver.providers.telegram_login import ActiveLoginChallenge, TelegramLoginManager

ROOT = Path(__file__).resolve().parents[1]

ACCOUNT = "worker"
API_HASH = "0123456789abcdef0123456789abcdef"
SESSION_BYTES = b"unauthorized-session-bytes"
SIDECARS = ("-journal", "-wal", "-shm")
BACKUP_NAME = re.compile(r"^telegram\.worker\.session\.unauthorized-backup-\d{8}T\d{6}Z$")


class SessionFixtureMixin:
    """Temporary session directory, mocked env and a mocked live authorization check."""

    def _start_fixture(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.base_session = os.path.join(self.tmpdir.name, "telegram.session")
        self.session_path = os.path.join(self.tmpdir.name, "telegram.worker.session")
        self.env_patcher = patch.dict(
            os.environ,
            {
                "TELEGRAM_SESSION_PATH": self.base_session,
                "TELEGRAM_API_ID": "123456",
                "TELEGRAM_API_HASH": API_HASH,
            },
        )
        self.env_patcher.start()
        _MEMORY_ACCOUNTS.clear()
        set_memory_only_mode(True)
        upsert_account(ACCOUNT, label="Worker", enabled=True)
        self.check = AsyncMock(return_value=False)
        self.check_patcher = patch("resolver.providers.telegram_login.is_session_authorized", self.check)
        self.check_patcher.start()

    def _stop_fixture(self) -> None:
        self.check_patcher.stop()
        self.env_patcher.stop()
        set_memory_only_mode(False)
        _MEMORY_ACCOUNTS.clear()
        self.tmpdir.cleanup()

    def _write(self, path: str, content: bytes = SESSION_BYTES) -> None:
        Path(path).write_bytes(content)

    def _write_session_with_sidecars(self) -> None:
        self._write(self.session_path)
        for suffix in SIDECARS:
            self._write(self.session_path + suffix, b"sidecar" + suffix.encode())

    def _names(self) -> set[str]:
        return set(os.listdir(self.tmpdir.name))


class ReplaceUnauthorizedSessionManagerTests(SessionFixtureMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._start_fixture()
        self.manager = TelegramLoginManager()

    async def asyncTearDown(self) -> None:
        self._stop_fixture()

    async def test_refuses_authorized_session_and_keeps_file(self):
        self._write(self.session_path)
        self.check.return_value = True

        res = await self.manager.replace_unauthorized_session(ACCOUNT)

        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "session_authorized")
        self.check.assert_awaited_once()
        self.assertTrue(self.check.call_args.kwargs.get("raise_on_error"))
        self.assertEqual(Path(self.session_path).read_bytes(), SESSION_BYTES)
        self.assertEqual(self._names(), {"telegram.worker.session"})

    async def test_refuses_when_live_check_cannot_complete(self):
        self._write(self.session_path)
        self.check.side_effect = ConnectionError("network unreachable")

        res = await self.manager.replace_unauthorized_session(ACCOUNT)

        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "check_failed")
        self.assertEqual(Path(self.session_path).read_bytes(), SESSION_BYTES)
        self.assertEqual(self._names(), {"telegram.worker.session"})

    async def test_refuses_without_api_credentials_before_any_check(self):
        self._write(self.session_path)
        with patch.dict(os.environ, {"TELEGRAM_API_HASH": ""}):
            res = await self.manager.replace_unauthorized_session(ACCOUNT)

        self.assertEqual(res["code"], "check_failed")
        self.check.assert_not_awaited()
        self.assertEqual(self._names(), {"telegram.worker.session"})

    async def test_refuses_while_login_challenge_is_active(self):
        self._write(self.session_path)
        self.manager._challenges[ACCOUNT] = ActiveLoginChallenge(
            account_id=ACCOUNT,
            phone="+48123456789",
            phone_code_hash="hash",
            client=MagicMock(),
            pending_path=os.path.join(self.tmpdir.name, ".telegram.worker.session.pending.session"),
            target_path=self.session_path,
        )

        res = await self.manager.replace_unauthorized_session(ACCOUNT)

        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "login_in_progress")
        self.check.assert_not_awaited()
        self.assertEqual(self._names(), {"telegram.worker.session"})

    async def test_refuses_missing_file_unknown_and_disabled_accounts(self):
        res = await self.manager.replace_unauthorized_session(ACCOUNT)
        self.assertEqual(res["code"], "no_session_file")

        res = await self.manager.replace_unauthorized_session("not_registered")
        self.assertEqual(res["code"], "not_found")

        upsert_account("worker_off", label="Off", enabled=False)
        off_path = TelegramAccountContext("worker_off").get_session_path()
        self._write(off_path)
        res = await self.manager.replace_unauthorized_session("worker_off")
        self.assertEqual(res["code"], "account_disabled")
        self.assertTrue(os.path.isfile(off_path))
        self.check.assert_not_awaited()

    async def test_moves_session_and_sidecars_to_backup_without_deleting(self):
        self._write_session_with_sidecars()

        res = await self.manager.replace_unauthorized_session(ACCOUNT)

        self.assertTrue(res["ok"], res)
        self.assertRegex(res["backup_file"], BACKUP_NAME)
        self.assertNotIn("/", res["backup_file"])
        self.assertNotIn(self.tmpdir.name, str(res))
        backup = res["backup_file"]
        self.assertFalse(os.path.exists(self.session_path))
        self.assertEqual(Path(os.path.join(self.tmpdir.name, backup)).read_bytes(), SESSION_BYTES)
        for suffix in SIDECARS:
            self.assertFalse(os.path.exists(self.session_path + suffix))
            moved = Path(os.path.join(self.tmpdir.name, backup + suffix))
            self.assertEqual(moved.read_bytes(), b"sidecar" + suffix.encode())
        self.assertEqual(len(self._names()), 1 + len(SIDECARS))

    async def test_never_overwrites_existing_backup_or_its_sidecars(self):
        stamp = datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        first = "telegram.worker.session.unauthorized-backup-20250102T030405Z"
        self._write(os.path.join(self.tmpdir.name, first), b"earlier backup")
        self._write(os.path.join(self.tmpdir.name, first + "-wal"), b"earlier wal")
        self._write_session_with_sidecars()

        with patch("resolver.providers.telegram_login.datetime") as fake_datetime:
            fake_datetime.now.return_value = stamp
            res = await self.manager.replace_unauthorized_session(ACCOUNT)

        self.assertTrue(res["ok"], res)
        # The first name is taken (its -wal sidecar exists), so the next free name is used.
        self.assertEqual(res["backup_file"], first + "-2")
        self.assertEqual(Path(os.path.join(self.tmpdir.name, first)).read_bytes(), b"earlier backup")
        self.assertEqual(Path(os.path.join(self.tmpdir.name, first + "-wal")).read_bytes(), b"earlier wal")
        self.assertEqual(Path(os.path.join(self.tmpdir.name, first + "-2")).read_bytes(), SESSION_BYTES)
        self.assertEqual(Path(os.path.join(self.tmpdir.name, first + "-2-journal")).read_bytes(), b"sidecar-journal")

    async def test_status_is_fresh_after_replacement_and_start_login_reaches_normal_flow(self):
        self._write(self.session_path)
        self.check.return_value = False
        await self.manager.get_status(ACCOUNT)
        self.assertEqual(self.manager._cached_auth.get(ACCOUNT), False)

        res = await self.manager.replace_unauthorized_session(ACCOUNT)
        self.assertTrue(res["ok"], res)
        self.assertNotIn(ACCOUNT, self.manager._cached_auth)

        status = await self.manager.get_status(ACCOUNT)
        self.assertEqual(status["status"], "ready")

        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        mock_client.send_code_request = AsyncMock(return_value=SimpleNamespace(phone_code_hash="hash_1"))
        mock_client.disconnect = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=False)

        with patch("telethon.TelegramClient", return_value=mock_client):
            started = await self.manager.start_login("+48123456789", account_id=ACCOUNT)

        self.assertTrue(started["ok"], started)
        self.assertEqual(started["state"], "code_requested")
        status = await self.manager.get_status(ACCOUNT)
        self.assertEqual(status["status"], "in_progress")
        self.assertNotIn("telegram.worker.session", self._names())
        self.assertIn(res["backup_file"], self._names())


class ReplaceUnauthorizedSessionRouteTests(SessionFixtureMixin, unittest.TestCase):
    def setUp(self) -> None:
        self._start_fixture()
        self.manager = TelegramLoginManager()

        class RouteAdmin:
            ADMIN_PUBLIC = True
            JSONResponse = JSONResponse

            def _admin_blocked(self, request):
                return False

            def _write_token(self):
                return ""

            def _wake_worker(self, worker):
                return True

        app = FastAPI()
        with patch("resolver.admin_v2_telegram_auth.get_telegram_login_manager", return_value=self.manager):
            app.include_router(admin_v2_telegram_auth.build_router(RouteAdmin()))
        self.client = TestClient(app)
        self.url = f"/api/v2/telegram/accounts/{ACCOUNT}/session/replace-unauthorized"
        self.headers = {"Origin": "http://testserver"}

    def tearDown(self) -> None:
        self._stop_fixture()

    def test_write_guard_rejects_request_without_matching_origin(self):
        self._write(self.session_path)
        res = self.client.post(self.url)
        self.assertEqual(res.status_code, 403)
        self.check.assert_not_awaited()
        self.assertTrue(os.path.isfile(self.session_path))

    def test_refusals_map_to_status_codes(self):
        self._write(self.session_path)
        self.check.return_value = True
        res = self.client.post(self.url, headers=self.headers)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "session_authorized")
        self.assertTrue(os.path.isfile(self.session_path))

        self.check.side_effect = ConnectionError("down")
        res = self.client.post(self.url, headers=self.headers)
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["code"], "check_failed")
        self.assertTrue(os.path.isfile(self.session_path))

    def test_success_returns_backup_file_name_only(self):
        self._write_session_with_sidecars()
        res = self.client.post(self.url, headers=self.headers)

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["account_id"], ACCOUNT)
        self.assertRegex(body["backup_file"], BACKUP_NAME)
        self.assertNotIn(self.tmpdir.name, res.text)
        self.assertNotIn("session_bytes", res.text)
        self.assertNotIn(SESSION_BYTES.decode(), res.text)
        self.assertFalse(os.path.exists(self.session_path))
        self.assertIsNotNone(get_account(ACCOUNT))


if __name__ == "__main__":
    unittest.main()
