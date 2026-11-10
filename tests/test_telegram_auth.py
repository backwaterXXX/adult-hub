"""Unit tests for Stage 3 Telegram authentication helpers and lifecycle."""

import asyncio
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


def _make_telethon_stubs():
    """Create stub module for telethon and telethon.errors."""
    telethon_mod = types.ModuleType("telethon")
    errors_mod = types.ModuleType("telethon.errors")
    tl_mod = types.ModuleType("telethon.tl")
    tl_types_mod = types.ModuleType("telethon.tl.types")
    tl_funcs_mod = types.ModuleType("telethon.tl.functions")

    class SessionPasswordNeededError(Exception):
        pass

    class PhoneCodeInvalidError(Exception):
        pass

    class PhoneCodeExpiredError(Exception):
        pass

    class PasswordHashInvalidError(Exception):
        pass

    class PhoneNumberInvalidError(Exception):
        pass

    class ApiIdInvalidError(Exception):
        pass

    errors_mod.SessionPasswordNeededError = SessionPasswordNeededError
    errors_mod.PhoneCodeInvalidError = PhoneCodeInvalidError
    errors_mod.PhoneCodeExpiredError = PhoneCodeExpiredError
    errors_mod.PasswordHashInvalidError = PasswordHashInvalidError
    errors_mod.PhoneNumberInvalidError = PhoneNumberInvalidError
    errors_mod.ApiIdInvalidError = ApiIdInvalidError

    tl_mod.types = tl_types_mod
    tl_mod.functions = tl_funcs_mod
    telethon_mod.errors = errors_mod
    telethon_mod.tl = tl_mod
    return telethon_mod, errors_mod


# Bootstrap stubs if telethon is not present in runtime environment
_SYS_TELETHON_STUBBED = False
try:
    import telethon  # noqa: F401
except ImportError:
    _stub_tl, _ = _make_telethon_stubs()
    sys.modules["telethon"] = _stub_tl
    sys.modules["telethon.errors"] = _stub_tl.errors
    sys.modules["telethon.tl"] = _stub_tl.tl
    sys.modules["telethon.tl.types"] = _stub_tl.tl.types
    sys.modules["telethon.tl.functions"] = _stub_tl.tl.functions
    _SYS_TELETHON_STUBBED = True

from resolver.providers.telegram_session import (
    get_pending_session_file_path,
    get_sqlite_session_file_path,
    interactive_login,
    is_session_authorized,
    validate_api_credentials,
    validate_phone_number,
)
from resolver.providers.telegram_auth import main as cli_main

# Fake session contents that pass the SQLite header check before Telethon is mocked.
SQLITE_FIXTURE = b"SQLite format 3\x00"


class TestTelegramValidation(unittest.TestCase):
    def test_validate_api_credentials_valid(self):
        api_id, api_hash = validate_api_credentials(123456, "abcdef0123456789abcdef0123456789")
        self.assertEqual(api_id, 123456)
        self.assertEqual(api_hash, "abcdef0123456789abcdef0123456789")

        # String representation of ID and uppercase hash
        api_id2, api_hash2 = validate_api_credentials("  987654 ", "ABCDEF0123456789ABCDEF0123456789")
        self.assertEqual(api_id2, 987654)
        self.assertEqual(api_hash2, "ABCDEF0123456789ABCDEF0123456789")

    def test_validate_api_credentials_invalid(self):
        for bad_id in (None, "", "not-a-number", -1, 0):
            with self.subTest(bad_id=bad_id):
                with self.assertRaises(ValueError):
                    validate_api_credentials(bad_id, "abcdef0123456789abcdef0123456789")

        for bad_hash in (None, "", "short", "not_a_hex_string_with_symbols!!!"):
            with self.subTest(bad_hash=bad_hash):
                with self.assertRaises(ValueError):
                    validate_api_credentials(123456, bad_hash)

    def test_validate_phone_number_valid(self):
        self.assertEqual(validate_phone_number("+48123456789"), "+48123456789")
        self.assertEqual(validate_phone_number(" +48 123-456-789 "), "+48123456789")
        self.assertEqual(validate_phone_number("+14155552671"), "+14155552671")

    def test_validate_phone_number_invalid(self):
        for bad_phone in (None, "", "123456789", "+48", "+abc1234567", "+12345678901234567"):
            with self.subTest(bad_phone=bad_phone):
                with self.assertRaises(ValueError):
                    validate_phone_number(bad_phone)

    def test_session_paths(self):
        target = get_sqlite_session_file_path("/data/telegram.session")
        self.assertEqual(target, "/data/telegram.session")
        target2 = get_sqlite_session_file_path("/data/telegram")
        self.assertEqual(target2, "/data/telegram.session")

        pending = get_pending_session_file_path("/data/telegram.session")
        self.assertEqual(pending, "/data/.telegram.session.pending.session")


def _make_telethon_stubs():
    """Create stub module for telethon and telethon.errors."""
    telethon_mod = types.ModuleType("telethon")
    errors_mod = types.ModuleType("telethon.errors")

    class SessionPasswordNeededError(Exception):
        pass

    class PhoneCodeInvalidError(Exception):
        pass

    class PhoneCodeExpiredError(Exception):
        pass

    class PasswordHashInvalidError(Exception):
        pass

    class PhoneNumberInvalidError(Exception):
        pass

    class ApiIdInvalidError(Exception):
        pass

    errors_mod.SessionPasswordNeededError = SessionPasswordNeededError
    errors_mod.PhoneCodeInvalidError = PhoneCodeInvalidError
    errors_mod.PhoneCodeExpiredError = PhoneCodeExpiredError
    errors_mod.PasswordHashInvalidError = PasswordHashInvalidError
    errors_mod.PhoneNumberInvalidError = PhoneNumberInvalidError
    errors_mod.ApiIdInvalidError = ApiIdInvalidError

    telethon_mod.errors = errors_mod
    return telethon_mod, errors_mod


class TestTelegramSessionAuth(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="adulthub_tg_test_")
        self.session_path = Path(self.temp_dir.name) / "telegram.session"
        self.telethon_mod, self.errors_mod = _make_telethon_stubs()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_is_session_authorized_missing_file_never_creates_file(self):
        """Non-existent session file returns False without touching the filesystem."""
        res = asyncio.run(is_session_authorized(str(self.session_path), 123456, "abcdef0123456789abcdef0123456789"))
        self.assertFalse(res)
        self.assertFalse(self.session_path.exists())

    def test_is_session_authorized_corrupt_file_raises_before_any_client_is_opened(self):
        """A file without the SQLite header is rejected before Telethon opens it, so no SQLite
        handle can be left behind. Callers already treat the exception as not authorized."""
        self.session_path.write_bytes(b"corrupt sqlite header")
        self.telethon_mod.TelegramClient = MagicMock()

        with patch.dict(sys.modules, {"telethon": self.telethon_mod, "telethon.errors": self.errors_mod}):
            with self.assertRaises(ValueError):
                asyncio.run(is_session_authorized(str(self.session_path), 123456, "abcdef0123456789abcdef0123456789"))

        self.telethon_mod.TelegramClient.assert_not_called()

    def test_is_session_authorized_valid_authorized_session(self):
        """Valid session returns True and disconnects client."""
        self.session_path.write_bytes(SQLITE_FIXTURE + b"valid_session_bytes")
        mock_client = AsyncMock()
        mock_client.is_user_authorized.return_value = True
        self.telethon_mod.TelegramClient = MagicMock(return_value=mock_client)

        with patch.dict(sys.modules, {"telethon": self.telethon_mod, "telethon.errors": self.errors_mod}):
            res = asyncio.run(is_session_authorized(str(self.session_path), 123456, "abcdef0123456789abcdef0123456789"))

        self.assertTrue(res)
        mock_client.connect.assert_awaited_once()
        mock_client.disconnect.assert_awaited_once()

    def test_interactive_login_already_authorized_skips_login(self):
        """Existing authorized target session returns already_authorized without prompting."""
        self.session_path.write_bytes(SQLITE_FIXTURE + b"existing_authorized_bytes")
        mock_client = AsyncMock()
        mock_client.is_user_authorized.return_value = True
        self.telethon_mod.TelegramClient = MagicMock(return_value=mock_client)

        prompt_mock = MagicMock()
        with patch.dict(sys.modules, {"telethon": self.telethon_mod, "telethon.errors": self.errors_mod}):
            res = asyncio.run(
                interactive_login(
                    str(self.session_path),
                    123456,
                    "abcdef0123456789abcdef0123456789",
                    phone="+48123456789",
                    prompt_fn=prompt_mock,
                )
            )

        self.assertEqual(res, "already_authorized")
        prompt_mock.assert_not_called()
        # Pre-existing bytes preserved
        self.assertEqual(self.session_path.read_bytes(), SQLITE_FIXTURE + b"existing_authorized_bytes")

    def test_protection_of_pre_existing_session_bytes(self):
        """Target session file is protected: unauthorized/corrupt existing session is never overwritten."""
        original_bytes = SQLITE_FIXTURE + b"pre_existing_unauthorized_database_bytes"
        self.session_path.write_bytes(original_bytes)

        mock_client = AsyncMock()
        mock_client.is_user_authorized.return_value = False
        self.telethon_mod.TelegramClient = MagicMock(return_value=mock_client)

        with patch.dict(sys.modules, {"telethon": self.telethon_mod, "telethon.errors": self.errors_mod}):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(
                    interactive_login(
                        str(self.session_path),
                        123456,
                        "abcdef0123456789abcdef0123456789",
                        phone="+48123456789",
                    )
                )

        self.assertIn("present but not authorized", str(ctx.exception))
        # Ensure bytes are strictly unchanged
        self.assertEqual(self.session_path.read_bytes(), original_bytes)

    def test_pending_authorized_session_reuse(self):
        """If a pending session was already authorized, reuse and promote it without re-auth."""
        pending_path = Path(get_pending_session_file_path(str(self.session_path)))
        pending_path.write_bytes(SQLITE_FIXTURE + b"pending_authorized_session")

        mock_client = AsyncMock()
        mock_client.is_user_authorized.return_value = True
        self.telethon_mod.TelegramClient = MagicMock(return_value=mock_client)

        prompt_mock = MagicMock()
        with patch.dict(sys.modules, {"telethon": self.telethon_mod, "telethon.errors": self.errors_mod}):
            res = asyncio.run(
                interactive_login(
                    str(self.session_path),
                    123456,
                    "abcdef0123456789abcdef0123456789",
                    prompt_fn=prompt_mock,
                )
            )

        self.assertEqual(res, "authorized")
        prompt_mock.assert_not_called()
        self.assertTrue(self.session_path.exists())
        self.assertEqual(self.session_path.read_bytes(), SQLITE_FIXTURE + b"pending_authorized_session")
        self.assertFalse(pending_path.exists())

    def test_first_login_code_success_promotes_with_mode_0600(self):
        """Fresh login sends code, preserves code hash, promotes target with mode 0600, and disconnects."""
        pending_path = Path(get_pending_session_file_path(str(self.session_path)))

        mock_sent_code = MagicMock()
        mock_sent_code.phone_code_hash = "hash_xyz_123"

        mock_client = AsyncMock()
        mock_client.is_user_authorized.side_effect = [False, True]
        mock_client.send_code_request.return_value = mock_sent_code

        def fake_sign_in(*args, **kwargs):
            # Simulate SQLite file creation upon sign in
            pending_path.write_bytes(SQLITE_FIXTURE + b"newly_authenticated_session")
            return AsyncMock()

        mock_client.sign_in.side_effect = fake_sign_in

        self.telethon_mod.TelegramClient = MagicMock(return_value=mock_client)

        prompts = ["+48123456789", "54321"]

        def prompt_fn(_):
            return prompts.pop(0)

        feedback_messages = []

        def feedback_fn(msg):
            feedback_messages.append(msg)

        with patch.dict(sys.modules, {"telethon": self.telethon_mod, "telethon.errors": self.errors_mod}):
            res = asyncio.run(
                interactive_login(
                    str(self.session_path),
                    123456,
                    "abcdef0123456789abcdef0123456789",
                    prompt_fn=prompt_fn,
                    feedback_fn=feedback_fn,
                )
            )

        self.assertEqual(res, "authorized")
        # Ensure phone_code_hash was passed to sign_in on same client
        mock_client.sign_in.assert_awaited_once_with(
            phone="+48123456789",
            code="54321",
            phone_code_hash="hash_xyz_123",
        )
        mock_client.disconnect.assert_awaited_once()

        # Target session promoted with 0600 mode
        self.assertTrue(self.session_path.exists())
        self.assertEqual(self.session_path.read_bytes(), SQLITE_FIXTURE + b"newly_authenticated_session")
        self.assertFalse(pending_path.exists())
        file_mode = stat.S_IMODE(self.session_path.stat().st_mode)
        self.assertEqual(file_mode, 0o600)

        # Confirm code is never printed in feedback
        feedback_str = " ".join(feedback_messages)
        self.assertNotIn("54321", feedback_str)

    def test_invalid_code_retry_path(self):
        """Invalid code triggers retry on same client; subsequent valid code succeeds."""
        pending_path = Path(get_pending_session_file_path(str(self.session_path)))

        mock_sent_code = MagicMock()
        mock_sent_code.phone_code_hash = "hash_retry"

        mock_client = AsyncMock()
        mock_client.is_user_authorized.side_effect = [False, True]
        mock_client.send_code_request.return_value = mock_sent_code

        # First sign_in raises PhoneCodeInvalidError, second succeeds
        sign_in_calls = []

        async def fake_sign_in(*args, **kwargs):
            sign_in_calls.append(kwargs.get("code"))
            if len(sign_in_calls) == 1:
                raise self.errors_mod.PhoneCodeInvalidError("Wrong code")
            pending_path.write_bytes(SQLITE_FIXTURE + b"session_after_retry")

        mock_client.sign_in.side_effect = fake_sign_in
        self.telethon_mod.TelegramClient = MagicMock(return_value=mock_client)

        prompts = ["00000", "99999"]

        def prompt_fn(_):
            return prompts.pop(0)

        with patch.dict(sys.modules, {"telethon": self.telethon_mod, "telethon.errors": self.errors_mod}):
            res = asyncio.run(
                interactive_login(
                    str(self.session_path),
                    123456,
                    "abcdef0123456789abcdef0123456789",
                    phone="+48123456789",
                    prompt_fn=prompt_fn,
                    feedback_fn=lambda _: None,
                )
            )

        self.assertEqual(res, "authorized")
        self.assertEqual(sign_in_calls, ["00000", "99999"])
        self.assertTrue(self.session_path.exists())
        self.assertEqual(self.session_path.read_bytes(), SQLITE_FIXTURE + b"session_after_retry")
        mock_client.disconnect.assert_awaited_once()

    def test_expired_code_resend_and_retry(self):
        """Expired code requests new code request on same client with updated hash."""
        pending_path = Path(get_pending_session_file_path(str(self.session_path)))

        code1 = MagicMock()
        code1.phone_code_hash = "hash_1"
        code2 = MagicMock()
        code2.phone_code_hash = "hash_2"

        mock_client = AsyncMock()
        mock_client.is_user_authorized.side_effect = [False, True]
        mock_client.send_code_request.side_effect = [code1, code2]

        sign_in_kwargs = []

        async def fake_sign_in(*args, **kwargs):
            sign_in_kwargs.append(kwargs.copy())
            if len(sign_in_kwargs) == 1:
                raise self.errors_mod.PhoneCodeExpiredError("Code expired")
            pending_path.write_bytes(SQLITE_FIXTURE + b"session_after_resend")

        mock_client.sign_in.side_effect = fake_sign_in
        self.telethon_mod.TelegramClient = MagicMock(return_value=mock_client)

        prompts = ["11111", "22222"]

        def prompt_fn(_):
            return prompts.pop(0)

        with patch.dict(sys.modules, {"telethon": self.telethon_mod, "telethon.errors": self.errors_mod}):
            res = asyncio.run(
                interactive_login(
                    str(self.session_path),
                    123456,
                    "abcdef0123456789abcdef0123456789",
                    phone="+48123456789",
                    prompt_fn=prompt_fn,
                    feedback_fn=lambda _: None,
                )
            )

        self.assertEqual(res, "authorized")
        self.assertEqual(mock_client.send_code_request.await_count, 2)
        self.assertEqual(sign_in_kwargs[0]["phone_code_hash"], "hash_1")
        self.assertEqual(sign_in_kwargs[1]["phone_code_hash"], "hash_2")
        self.assertTrue(self.session_path.exists())

    def test_two_factor_auth_flow_success(self):
        """2FA password challenge triggers masked prompt and secondary sign in on same client."""
        pending_path = Path(get_pending_session_file_path(str(self.session_path)))

        mock_sent_code = MagicMock()
        mock_sent_code.phone_code_hash = "hash_2fa"

        mock_client = AsyncMock()
        mock_client.is_user_authorized.side_effect = [False, True]
        mock_client.send_code_request.return_value = mock_sent_code

        async def fake_sign_in(*args, **kwargs):
            if "code" in kwargs:
                raise self.errors_mod.SessionPasswordNeededError("2FA required")
            if kwargs.get("password") == "my2fasecret":
                pending_path.write_bytes(SQLITE_FIXTURE + b"session_after_2fa")
                return
            raise self.errors_mod.PasswordHashInvalidError("Wrong 2FA")

        mock_client.sign_in.side_effect = fake_sign_in
        self.telethon_mod.TelegramClient = MagicMock(return_value=mock_client)

        pwd_prompts = []

        def pwd_prompt(msg):
            pwd_prompts.append(msg)
            return "my2fasecret"

        with patch.dict(sys.modules, {"telethon": self.telethon_mod, "telethon.errors": self.errors_mod}):
            res = asyncio.run(
                interactive_login(
                    str(self.session_path),
                    123456,
                    "abcdef0123456789abcdef0123456789",
                    phone="+48123456789",
                    prompt_fn=lambda _: "12345",
                    password_prompt_fn=pwd_prompt,
                    feedback_fn=lambda _: None,
                )
            )

        self.assertEqual(res, "authorized")
        self.assertEqual(len(pwd_prompts), 1)
        self.assertTrue(self.session_path.exists())
        self.assertEqual(self.session_path.read_bytes(), SQLITE_FIXTURE + b"session_after_2fa")

    def test_invalid_2fa_retry_path(self):
        """Invalid 2FA password allows retry; subsequent valid 2FA succeeds."""
        pending_path = Path(get_pending_session_file_path(str(self.session_path)))

        mock_sent_code = MagicMock()
        mock_sent_code.phone_code_hash = "hash_2fa_retry"

        mock_client = AsyncMock()
        mock_client.is_user_authorized.side_effect = [False, True]
        mock_client.send_code_request.return_value = mock_sent_code

        pwd_calls = []

        async def fake_sign_in(*args, **kwargs):
            if "code" in kwargs:
                raise self.errors_mod.SessionPasswordNeededError("2FA required")
            pwd = kwargs.get("password")
            pwd_calls.append(pwd)
            if pwd == "wrong_pwd":
                raise self.errors_mod.PasswordHashInvalidError("Bad password")
            pending_path.write_bytes(SQLITE_FIXTURE + b"session_2fa_ok")

        mock_client.sign_in.side_effect = fake_sign_in
        self.telethon_mod.TelegramClient = MagicMock(return_value=mock_client)

        pwds = ["wrong_pwd", "correct_pwd"]

        def pwd_prompt(_):
            return pwds.pop(0)

        with patch.dict(sys.modules, {"telethon": self.telethon_mod, "telethon.errors": self.errors_mod}):
            res = asyncio.run(
                interactive_login(
                    str(self.session_path),
                    123456,
                    "abcdef0123456789abcdef0123456789",
                    phone="+48123456789",
                    prompt_fn=lambda _: "12345",
                    password_prompt_fn=pwd_prompt,
                    feedback_fn=lambda _: None,
                )
            )

        self.assertEqual(res, "authorized")
        self.assertEqual(pwd_calls, ["wrong_pwd", "correct_pwd"])
        self.assertTrue(self.session_path.exists())

    def test_disconnect_and_cleanup_on_failure(self):
        """Failure to authenticate cleans up pending file, preserves target, and disconnects client."""
        pending_path = Path(get_pending_session_file_path(str(self.session_path)))
        pending_path.write_bytes(SQLITE_FIXTURE + b"temporary_garbage")

        mock_sent_code = MagicMock()
        mock_sent_code.phone_code_hash = "hash_fail"

        mock_client = AsyncMock()
        mock_client.is_user_authorized.return_value = False
        mock_client.send_code_request.return_value = mock_sent_code
        mock_client.sign_in.side_effect = self.errors_mod.PhoneCodeInvalidError("Invalid")

        self.telethon_mod.TelegramClient = MagicMock(return_value=mock_client)

        with patch.dict(sys.modules, {"telethon": self.telethon_mod, "telethon.errors": self.errors_mod}):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(
                    interactive_login(
                        str(self.session_path),
                        123456,
                        "abcdef0123456789abcdef0123456789",
                        phone="+48123456789",
                        prompt_fn=lambda _: "badcode",
                        feedback_fn=lambda _: None,
                        max_code_retries=2,
                    )
                )

        self.assertIn("maximum code attempts exceeded", str(ctx.exception))
        # Pending session removed
        self.assertFalse(pending_path.exists())
        # Target session was never created
        self.assertFalse(self.session_path.exists())
        # Client disconnect strictly awaited
        self.assertGreaterEqual(mock_client.disconnect.await_count, 1)


class TestTelegramAuthCli(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="adulthub_cli_")
        self.session_path = Path(self.temp_dir.name) / "telegram.session"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cli_check_authorized(self):
        with patch("resolver.providers.telegram_auth.is_session_authorized", new=AsyncMock(return_value=True)):
            ret = cli_main([
                "check",
                "--session-path", str(self.session_path),
                "--api-id", "123456",
                "--api-hash", "abcdef0123456789abcdef0123456789",
            ])
            self.assertEqual(ret, 0)

    def test_cli_check_not_authorized(self):
        with patch("resolver.providers.telegram_auth.is_session_authorized", new=AsyncMock(return_value=False)):
            ret = cli_main([
                "check",
                "--session-path", str(self.session_path),
                "--api-id", "123456",
                "--api-hash", "abcdef0123456789abcdef0123456789",
            ])
            self.assertEqual(ret, 1)

    def test_cli_auth_success(self):
        with patch("resolver.providers.telegram_auth.interactive_login", new=AsyncMock(return_value="authorized")):
            ret = cli_main([
                "auth",
                "--session-path", str(self.session_path),
                "--api-id", "123456",
                "--api-hash", "abcdef0123456789abcdef0123456789",
            ])
            self.assertEqual(ret, 0)

    def test_cli_auth_failure(self):
        with patch("resolver.providers.telegram_auth.interactive_login", new=AsyncMock(side_effect=RuntimeError("Auth failed"))):
            ret = cli_main([
                "auth",
                "--session-path", str(self.session_path),
                "--api-id", "123456",
                "--api-hash", "abcdef0123456789abcdef0123456789",
            ])
            self.assertEqual(ret, 1)


if __name__ == "__main__":
    unittest.main()
