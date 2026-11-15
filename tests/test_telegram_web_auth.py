"""Automated test suite for Telegram web login manager lifecycle, state machine,
session promotion, and account boundary.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from resolver.providers.telegram_account import DEFAULT_ACCOUNT_ID, TelegramAccountContext
from resolver.providers.telegram_login import (
    ActiveLoginChallenge,
    TelegramLoginManager,
    mask_phone_number,
)
from telethon.errors import (
    FloodWaitError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)


class TestTelegramAccountBoundary(unittest.TestCase):
    """Test the internal TelegramAccountContext boundary preparing for future multi-account."""

    def test_default_account_identity(self):
        ctx = TelegramAccountContext()
        self.assertEqual(ctx.account_id, DEFAULT_ACCOUNT_ID)
        self.assertEqual(ctx.account_id, "default")

    def test_path_traversal_and_invalid_account_ids_rejected(self):
        for bad_id in (
            "../../etc/passwd",
            "../default",
            "account/one",
            "account\\two",
            "acc ount",
            "bad*char",
            "null\x00byte",
            "",
            "   ",
            "a" * 65,  # too long
        ):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError):
                    TelegramAccountContext(bad_id)

    def test_session_path_resolution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base_session = os.path.join(tmpdir, "telegram.session")
            with patch.dict(os.environ, {"TELEGRAM_SESSION_PATH": base_session}):
                default_ctx = TelegramAccountContext("default")
                self.assertEqual(default_ctx.get_session_path(), base_session)
                pending_path = default_ctx.get_pending_session_path()
                self.assertTrue(os.path.basename(pending_path).startswith("."))
                self.assertIn("pending", os.path.basename(pending_path))

                # Scoped secondary account with safe identifier
                other_ctx = TelegramAccountContext("backup_bot")
                other_session = other_ctx.get_session_path()
                self.assertIn("telegram.backup_bot.session", other_session)
                other_pending = other_ctx.get_pending_session_path()
                self.assertTrue(os.path.basename(other_pending).startswith("."))
                self.assertIn("backup_bot", other_pending)

    def test_credentials_check_without_exposure(self):
        ctx = TelegramAccountContext()
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(ctx.has_api_credentials())
            with self.assertRaises(ValueError):
                ctx.get_api_credentials()

        with patch.dict(os.environ, {"TELEGRAM_API_ID": "123456", "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef"}):
            self.assertTrue(ctx.has_api_credentials())
            api_id, api_hash = ctx.get_api_credentials()
            self.assertEqual(api_id, 123456)
            self.assertEqual(api_hash, "0123456789abcdef0123456789abcdef")



class TestPhoneMaskingAndRedaction(unittest.TestCase):
    """Test that sensitive phone numbers are masked and never leaked."""

    def test_phone_masking(self):
        self.assertEqual(mask_phone_number("+48123456789"), "+48 *** *** 789")
        self.assertEqual(mask_phone_number("+14155552671"), "+14 *** *** 671")
        # Short strings
        self.assertEqual(mask_phone_number("+1234"), "+1***4")



class TestTelegramLoginManagerLifecycle(unittest.IsolatedAsyncioTestCase):
    """Test transient in-memory challenge lifecycle, bounding, and session promotion."""

    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.session_path = os.path.join(self.tmpdir.name, "telegram.session")
        self.env_patcher = patch.dict(
            os.environ,
            {
                "TELEGRAM_SESSION_PATH": self.session_path,
                "TELEGRAM_API_ID": "123456",
                "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
                "TELEGRAM_ACCOUNT_STORE_MEMORY_ONLY": "1",
            },
        )
        self.env_patcher.start()
        self.manager = TelegramLoginManager()

    async def asyncTearDown(self):
        self.env_patcher.stop()
        self.tmpdir.cleanup()

    async def test_get_status_unconfigured(self):
        with patch.dict(os.environ, {"TELEGRAM_API_ID": "", "TELEGRAM_API_HASH": ""}):
            status = await self.manager.get_status()
            self.assertEqual(status["status"], "unconfigured")
            self.assertFalse(status["authorized"])
            self.assertFalse(status["api_configured"])

    async def test_get_status_ready_when_configured_and_no_session(self):
        status = await self.manager.get_status()
        self.assertEqual(status["status"], "ready")
        self.assertFalse(status["authorized"])
        self.assertTrue(status["api_configured"])

    async def test_get_status_check_error_is_not_reported_as_unauthorized(self):
        Path(self.session_path).write_bytes(b"sqlite_data")
        with patch("resolver.providers.telegram_login.is_session_authorized", new_callable=AsyncMock) as mock_auth:
            mock_auth.side_effect = RuntimeError("database is locked")
            status = await self.manager.get_status()
        self.assertEqual(status["status"], "check_failed")
        self.assertFalse(status["authorized"])
        self.assertTrue(Path(self.session_path).exists())
        # The failed check must not poison the cached verdict that later reads without a live check use.
        self.assertNotIn(self.manager._cached_auth.get("default"), (False,))

        with patch("resolver.providers.telegram_login.is_session_authorized", new_callable=AsyncMock) as mock_auth:
            mock_auth.return_value = True
            status = await self.manager.get_status()
        self.assertEqual(status["status"], "authorized")

    async def test_get_status_authorized_when_existing_session_valid(self):
        Path(self.session_path).write_bytes(b"sqlite_data")
        with patch("resolver.providers.telegram_login.is_session_authorized", new_callable=AsyncMock) as mock_auth:
            mock_auth.return_value = True
            status = await self.manager.get_status()
            self.assertEqual(status["status"], "authorized")
            self.assertTrue(status["authorized"])

    async def test_get_status_unauthorized_present_preserves_existing_session(self):
        Path(self.session_path).write_bytes(b"corrupted_or_stale_session_bytes")
        with patch("resolver.providers.telegram_login.is_session_authorized", new_callable=AsyncMock) as mock_auth:
            mock_auth.return_value = False
            status = await self.manager.get_status()
            self.assertEqual(status["status"], "unauthorized_session_present")
            self.assertFalse(status["authorized"])
            # Existing file must NOT be deleted
            self.assertTrue(os.path.exists(self.session_path))
            self.assertEqual(Path(self.session_path).read_bytes(), b"corrupted_or_stale_session_bytes")

    async def test_start_login_refuses_to_overwrite_existing_session(self):
        Path(self.session_path).write_bytes(b"existing_session")
        with patch("resolver.providers.telegram_login.is_session_authorized", new_callable=AsyncMock) as mock_auth:
            mock_auth.return_value = True
            res = await self.manager.start_login("+48123456789")
            self.assertFalse(res["ok"])
            self.assertEqual(res["code"], "already_authorized")

        with patch("resolver.providers.telegram_login.is_session_authorized", new_callable=AsyncMock) as mock_auth:
            mock_auth.return_value = False
            res = await self.manager.start_login("+48123456789")
            self.assertFalse(res["ok"])
            self.assertEqual(res["code"], "unauthorized_session_present")

    async def test_start_login_handles_flood_wait(self):
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        mock_client.send_code_request = AsyncMock(side_effect=FloodWaitError(request=None))
        mock_client.send_code_request.side_effect.seconds = 180
        mock_client.disconnect = AsyncMock()

        with patch("telethon.TelegramClient", return_value=mock_client):
            res = await self.manager.start_login("+48123456789")
            self.assertFalse(res["ok"])
            self.assertEqual(res["code"], "flood_wait")
            self.assertEqual(res["flood_wait_seconds"], 180)
            mock_client.disconnect.assert_awaited()

    async def test_start_login_success_and_status_in_progress(self):
        mock_sent = SimpleNamespace(phone_code_hash="fake_hash_123")
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        mock_client.send_code_request = AsyncMock(return_value=mock_sent)

        with patch("telethon.TelegramClient", return_value=mock_client):
            res = await self.manager.start_login("+48123456789")
            self.assertTrue(res["ok"])
            self.assertEqual(res["phone_mask"], "+48 *** *** 789")
            self.assertIn("expires_in_seconds", res)

            # Check status reflects in_progress
            status = await self.manager.get_status()
            self.assertEqual(status["status"], "in_progress")
            self.assertEqual(status["phone_mask"], "+48 *** *** 789")

    async def test_submit_code_invalid_increments_attempts_and_bounds_at_5(self):
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(side_effect=PhoneCodeInvalidError(request=None))
        mock_client.disconnect = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        challenge = ActiveLoginChallenge(
            account_id="default",
            phone="+48123456789",
            phone_code_hash="h1",
            client=mock_client,
            pending_path=os.path.join(self.tmpdir.name, ".telegram.pending.session"),
            target_path=self.session_path,
        )
        self.manager._challenges["default"] = challenge

        # Attempts 1 to 4 should return remaining attempts
        for attempt in range(1, 5):
            res = await self.manager.submit_code("11111")
            self.assertFalse(res["ok"])
            self.assertEqual(res["code"], "invalid_code")
            self.assertEqual(res["remaining_attempts"], 5 - attempt)

        # 5th attempt should trigger max_attempts and cancel challenge
        res5 = await self.manager.submit_code("11111")
        self.assertFalse(res5["ok"])
        self.assertEqual(res5["code"], "max_attempts")
        self.assertNotIn("default", self.manager._challenges)

    async def test_submit_code_requires_2fa_transitions_state(self):
        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock(side_effect=SessionPasswordNeededError(request=None))

        challenge = ActiveLoginChallenge(
            account_id="default",
            phone="+48123456789",
            phone_code_hash="h1",
            client=mock_client,
            pending_path=os.path.join(self.tmpdir.name, ".telegram.pending.session"),
            target_path=self.session_path,
        )
        self.manager._challenges["default"] = challenge

        res = await self.manager.submit_code("12345")
        self.assertTrue(res["ok"])
        self.assertEqual(res["state"], "password_needed")
        self.assertEqual(challenge.state, "password_needed")

    async def test_submit_code_valid_promotes_session_atomically_with_mode_0600(self):
        pending_path = os.path.join(self.tmpdir.name, ".telegram.pending.session")
        Path(pending_path).write_bytes(b"authorized_session_payload")

        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.disconnect = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        challenge = ActiveLoginChallenge(
            account_id="default",
            phone="+48123456789",
            phone_code_hash="h1",
            client=mock_client,
            pending_path=pending_path,
            target_path=self.session_path,
        )
        self.manager._challenges["default"] = challenge

        with patch("resolver.providers.telegram.reset_telegram_client") as mock_reset:
            res = await self.manager.submit_code("12345")
            self.assertTrue(res["ok"])
            self.assertEqual(res["state"], "authorized")

            # Verify atomic promotion: pending removed, target exists
            self.assertFalse(os.path.exists(pending_path))
            self.assertTrue(os.path.exists(self.session_path))
            self.assertEqual(Path(self.session_path).read_bytes(), b"authorized_session_payload")

            # Check file permissions are 0o600
            file_mode = stat.S_IMODE(os.stat(self.session_path).st_mode)
            self.assertEqual(file_mode, 0o600)

            # Check reset_telegram_client was notified
            mock_reset.assert_called_once()

    async def test_submit_password_valid_promotes_session(self):
        pending_path = os.path.join(self.tmpdir.name, ".telegram.pending.session")
        Path(pending_path).write_bytes(b"2fa_authorized_session_payload")

        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.disconnect = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        challenge = ActiveLoginChallenge(
            account_id="default",
            phone="+48123456789",
            phone_code_hash="h1",
            client=mock_client,
            pending_path=pending_path,
            target_path=self.session_path,
        )
        challenge.state = "password_needed"
        self.manager._challenges["default"] = challenge

        with patch("resolver.providers.telegram.reset_telegram_client"):
            res = await self.manager.submit_password("correct_2fa_password")
            self.assertTrue(res["ok"])
            self.assertEqual(res["state"], "authorized")
            self.assertTrue(os.path.exists(self.session_path))
            self.assertEqual(Path(self.session_path).read_bytes(), b"2fa_authorized_session_payload")
            file_mode = stat.S_IMODE(os.stat(self.session_path).st_mode)
            self.assertEqual(file_mode, 0o600)

    async def test_submit_password_preserves_surrounding_whitespace(self):
        pending_path = os.path.join(self.tmpdir.name, ".telegram.pending.session")
        Path(pending_path).write_bytes(b"2fa_session")

        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.disconnect = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        challenge = ActiveLoginChallenge(
            account_id="default",
            phone="+48123456789",
            phone_code_hash="h1",
            client=mock_client,
            pending_path=pending_path,
            target_path=self.session_path,
        )
        challenge.state = "password_needed"
        self.manager._challenges["default"] = challenge

        pwd_with_spaces = "  spaced_secret_password  "
        with patch("resolver.providers.telegram.reset_telegram_client"):
            res = await self.manager.submit_password(pwd_with_spaces)
            self.assertTrue(res["ok"])
            mock_client.sign_in.assert_awaited_once_with(password=pwd_with_spaces)

    async def test_resend_code_caps_resends_and_preserves_lifetime_attempts(self):
        mock_sent = SimpleNamespace(phone_code_hash="new_hash")
        mock_client = MagicMock()
        mock_client.send_code_request = AsyncMock(return_value=mock_sent)
        mock_client.sign_in = AsyncMock(side_effect=PhoneCodeInvalidError(request=None))
        mock_client.disconnect = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        challenge = ActiveLoginChallenge(
            account_id="default",
            phone="+48123456789",
            phone_code_hash="h1",
            client=mock_client,
            pending_path=os.path.join(self.tmpdir.name, ".telegram.pending.session"),
            target_path=self.session_path,
            ttl_seconds=600.0,
            max_attempts=5,
            max_resends=3,
        )
        self.manager._challenges["default"] = challenge

        # Fail code twice -> attempts = 2
        await self.manager.submit_code("00000")
        await self.manager.submit_code("00000")
        self.assertEqual(challenge.attempts, 2)

        # 1st resend -> attempts must NOT be reset!
        r1 = await self.manager.resend_code("default")
        self.assertTrue(r1["ok"])
        self.assertEqual(challenge.resends, 1)
        self.assertEqual(challenge.attempts, 2)
        self.assertEqual(r1["remaining_attempts"], 3)
        self.assertEqual(r1["resends_remaining"], 2)

        # 2nd resend
        r2 = await self.manager.resend_code("default")
        self.assertTrue(r2["ok"])
        self.assertEqual(challenge.resends, 2)
        self.assertEqual(challenge.attempts, 2)

        # 3rd resend (last allowed)
        r3 = await self.manager.resend_code("default")
        self.assertTrue(r3["ok"])
        self.assertEqual(challenge.resends, 3)
        self.assertEqual(challenge.attempts, 2)

        # 4th resend must fail with max_resends_exceeded
        r4 = await self.manager.resend_code("default")
        self.assertFalse(r4["ok"])
        self.assertEqual(r4["code"], "max_resends_exceeded")

        # Now fail 3 more code attempts -> total 5 attempts triggers max_attempts
        await self.manager.submit_code("00000")
        await self.manager.submit_code("00000")
        r_final = await self.manager.submit_code("00000")
        self.assertFalse(r_final["ok"])
        self.assertEqual(r_final["code"], "max_attempts")

    async def test_resend_code_handles_flood_wait(self):
        mock_client = MagicMock()
        mock_client.send_code_request = AsyncMock(side_effect=FloodWaitError(request=None))
        mock_client.send_code_request.side_effect.seconds = 45

        challenge = ActiveLoginChallenge(
            account_id="default",
            phone="+48123456789",
            phone_code_hash="h1",
            client=mock_client,
            pending_path=os.path.join(self.tmpdir.name, ".telegram.pending.session"),
            target_path=self.session_path,
        )
        self.manager._challenges["default"] = challenge

        res = await self.manager.resend_code("default")
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "flood_wait")
        self.assertEqual(res["flood_wait_seconds"], 45)

    async def test_start_login_invalid_phone_returns_400_code(self):
        for bad_phone in ("12345", "not_a_phone", "+1", "+abcdefghijk"):
            with self.subTest(bad_phone=bad_phone):
                res = await self.manager.start_login(bad_phone)
                self.assertFalse(res["ok"])
                self.assertEqual(res["code"], "invalid_phone")

    async def test_start_login_timeout_handled_cleanly(self):
        mock_client = MagicMock()
        mock_client.connect = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_client.disconnect = AsyncMock()

        with patch("telethon.TelegramClient", return_value=mock_client):
            res = await self.manager.start_login("+48123456789")
            self.assertFalse(res["ok"])
            self.assertEqual(res["code"], "timeout")

    async def test_concurrent_cleanup_promotion_race_safety(self):
        pending_path = os.path.join(self.tmpdir.name, ".telegram.pending.session")
        Path(pending_path).write_bytes(b"promoted_bytes")

        mock_client = MagicMock()
        mock_client.sign_in = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=True)
        mock_client.disconnect = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        challenge = ActiveLoginChallenge(
            account_id="default",
            phone="+48123456789",
            phone_code_hash="h1",
            client=mock_client,
            pending_path=pending_path,
            target_path=self.session_path,
        )
        self.manager._challenges["default"] = challenge

        # When promotion succeeds, challenge.state is authorized.
        with patch("resolver.providers.telegram.reset_telegram_client"):
            submit_res = await self.manager.submit_code("12345")
            self.assertTrue(submit_res["ok"])
            self.assertEqual(submit_res["state"], "authorized")

        # Concurrent or subsequent start_login must refuse to overwrite promoted session
        start_res = await self.manager.start_login("+48123456789")
        self.assertFalse(start_res["ok"])
        self.assertEqual(start_res["code"], "already_authorized")
        self.assertTrue(os.path.exists(self.session_path))
        self.assertEqual(Path(self.session_path).read_bytes(), b"promoted_bytes")

    async def test_reset_telegram_client_disconnects(self):
        from resolver.providers import telegram
        mock_old = MagicMock()
        mock_old.disconnect = AsyncMock()
        mock_old.is_connected = MagicMock(return_value=True)
        telegram._client = mock_old
        telegram._client_lock = asyncio.Lock()

        await telegram.reset_telegram_client()
        mock_old.disconnect.assert_awaited_once()
        self.assertIsNone(telegram._client)
        self.assertIsNone(telegram._client_lock)

    async def test_get_status_does_not_open_telegram_connection_when_check_live_false(self):
        Path(self.session_path).write_bytes(b"session_bytes")
        with patch("resolver.providers.telegram_login.is_session_authorized", new_callable=AsyncMock) as mock_live:
            res = await self.manager.get_status(check_live=False)
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "session_file_present")
            mock_live.assert_not_called()

    async def test_cancel_login_cleans_up_pending_session_file(self):
        pending_path = os.path.join(self.tmpdir.name, ".telegram.pending.session")
        Path(pending_path).write_bytes(b"pending_to_clean")

        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        challenge = ActiveLoginChallenge(
            account_id="default",
            phone="+48123456789",
            phone_code_hash="h1",
            client=mock_client,
            pending_path=pending_path,
            target_path=self.session_path,
        )
        self.manager._challenges["default"] = challenge

        res = await self.manager.cancel_login("default")
        self.assertTrue(res["ok"])
        self.assertNotIn("default", self.manager._challenges)
        self.assertFalse(os.path.exists(pending_path))

    async def test_repeated_start_preserves_attempts_across_cancel(self):
        mock_sent = SimpleNamespace(phone_code_hash="h1")
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        mock_client.send_code_request = AsyncMock(return_value=mock_sent)
        mock_client.sign_in = AsyncMock(side_effect=PhoneCodeInvalidError(request=None))
        mock_client.disconnect = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        with patch("telethon.TelegramClient", return_value=mock_client):
            # 1. Start login
            res1 = await self.manager.start_login("+48123456789")
            self.assertTrue(res1["ok"])

            # Fail code twice -> attempts = 2
            await self.manager.submit_code("00000")
            await self.manager.submit_code("00000")

            # 2. Cancel login
            await self.manager.cancel_login("default")

            # 3. Start login again -> should inherit attempts = 2
            res2 = await self.manager.start_login("+48123456789")
            self.assertTrue(res2["ok"])
            active = await self.manager._get_valid_challenge("default")
            self.assertEqual(active.attempts, 2)

            # 3 more failed attempts should trigger max_attempts (5 total)
            await self.manager.submit_code("00000")
            await self.manager.submit_code("00000")
            res_max = await self.manager.submit_code("00000")
            self.assertFalse(res_max["ok"])
            self.assertEqual(res_max["code"], "max_attempts")

    async def test_repeated_starts_trigger_account_cooldown(self):
        mock_sent = SimpleNamespace(phone_code_hash="h1")
        mock_client = MagicMock()
        mock_client.connect = AsyncMock()
        mock_client.is_user_authorized = AsyncMock(return_value=False)
        mock_client.send_code_request = AsyncMock(return_value=mock_sent)
        mock_client.disconnect = AsyncMock()

        with patch("telethon.TelegramClient", return_value=mock_client):
            for _ in range(5):
                res = await self.manager.start_login("+48123456789")
                self.assertTrue(res["ok"])

            # 6th start exceeds MAX_ACCOUNT_STARTS and returns flood_wait
            res6 = await self.manager.start_login("+48123456789")
            self.assertFalse(res6["ok"])
            self.assertEqual(res6["code"], "flood_wait")
            self.assertIn("flood_wait_seconds", res6)

    async def test_disconnect_never_holds_manager_lock(self):
        mock_client = MagicMock()
        mock_client.is_connected = MagicMock(return_value=True)

        lock_state_during_disconnect = None

        async def fake_disconnect():
            nonlocal lock_state_during_disconnect
            lock_state_during_disconnect = self.manager._manager_lock.locked()

        mock_client.disconnect = AsyncMock(side_effect=fake_disconnect)

        challenge = ActiveLoginChallenge(
            account_id="default",
            phone="+48123456789",
            phone_code_hash="h1",
            client=mock_client,
            pending_path=os.path.join(self.tmpdir.name, ".telegram.pending.session"),
            target_path=self.session_path,
        )
        self.manager._challenges["default"] = challenge

        await self.manager.cancel_login("default")
        self.assertFalse(lock_state_during_disconnect)



if __name__ == "__main__":
    unittest.main()
