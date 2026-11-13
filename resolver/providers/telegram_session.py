"""Telegram account session setup and authenticated Telethon client access."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from typing import Any, Callable, Optional, Tuple

from common.config import DEFAULT_TELEGRAM_REQUEST_TIMEOUT_SECONDS, telegram_client_kwargs

logger = logging.getLogger(__name__)


def validate_api_credentials(api_id: Any, api_hash: Any) -> Tuple[int, str]:
    """Validate Telegram API ID and API Hash from my.telegram.org."""
    if api_id is None or str(api_id).strip() == "":
        raise ValueError("Telegram API ID cannot be empty.")
    try:
        api_id_int = int(str(api_id).strip())
        if api_id_int <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("Telegram API ID must be a positive integer.")

    if api_hash is None or not isinstance(api_hash, str) or not api_hash.strip():
        raise ValueError("Telegram API Hash cannot be empty.")
    api_hash_str = api_hash.strip()
    if len(api_hash_str) < 16 or not all(c in "0123456789abcdefABCDEF" for c in api_hash_str):
        raise ValueError("Telegram API Hash must be a valid hex string from my.telegram.org.")

    return api_id_int, api_hash_str


def validate_phone_number(phone: Any) -> str:
    """Validate and normalize international phone number."""
    if not phone or not isinstance(phone, str):
        raise ValueError("Phone number cannot be empty.")
    cleaned = "".join(phone.split()).replace("-", "")
    if not cleaned.startswith("+") or not cleaned[1:].isdigit() or len(cleaned) < 8 or len(cleaned) > 16:
        raise ValueError("Phone number must be in international format (e.g. +48123456789).")
    return cleaned


def get_sqlite_session_file_path(session_path: str) -> str:
    """Return the exact SQLite session filename on disk used by Telethon."""
    return session_path if session_path.endswith(".session") else f"{session_path}.session"


def get_pending_session_file_path(session_path: str) -> str:
    """Return an isolated pending session filename in the same directory."""
    target = get_sqlite_session_file_path(session_path)
    dirname, basename = os.path.split(target)
    return os.path.join(dirname, f".{basename}.pending.session")


_SQLITE_HEADER = b"SQLite format 3\x00"


def _has_sqlite_header(path: str) -> bool:
    try:
        with open(path, "rb") as stream:
            return stream.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


async def is_session_authorized(
    session_path: str,
    api_id: Any,
    api_hash: Any,
    *,
    raise_on_error: bool = False,
) -> bool:
    """Check if the given session file exists and is authorized.

    By default a network or Telegram error returns False. With raise_on_error=True the
    error propagates, so a caller can tell "not authorized" apart from "could not check".
    """
    actual_path = get_sqlite_session_file_path(session_path)
    if not os.path.isfile(actual_path) or os.path.getsize(actual_path) == 0:
        return False
    try:
        api_id_int, api_hash_str = validate_api_credentials(api_id, api_hash)
    except ValueError:
        return False

    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise RuntimeError(
            "Telethon is not installed. Add 'telethon>=1.36' to "
            "resolver/requirements.txt and install it first."
        ) from exc

    if not _has_sqlite_header(actual_path):
        # Telethon opens the file in its constructor. A corrupt file makes that constructor fail
        # after it has already opened a SQLite handle that nothing can close, so reject it here.
        # Callers see an exception, as they did before, and keep their cached-authorization rules.
        raise ValueError("Telegram session file is not a SQLite database.")

    client = TelegramClient(
        actual_path, api_id_int, api_hash_str,
        **telegram_client_kwargs(),
    )
    try:
        await asyncio.wait_for(
            client.connect(), timeout=DEFAULT_TELEGRAM_REQUEST_TIMEOUT_SECONDS
        )
        return bool(await asyncio.wait_for(
            client.is_user_authorized(), timeout=DEFAULT_TELEGRAM_REQUEST_TIMEOUT_SECONDS
        ))
    except Exception:
        if raise_on_error:
            raise
        logger.debug("Failed checking session authorization for %s", actual_path, exc_info=True)
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            logger.debug("Ignored exception in client.disconnect()", exc_info=True)


def _default_feedback(msg: str) -> None:
    sys.stdout.write(f"{msg}\n")
    sys.stdout.flush()


def _default_password_prompt(prompt: str) -> str:
    import getpass
    try:
        return getpass.getpass(prompt)
    except Exception:
        return input(prompt)


def _promote_pending_session(pending_path: str, target_path: str) -> None:
    if os.path.isfile(pending_path):
        os.replace(pending_path, target_path)
        try:
            os.chmod(target_path, 0o600)
        except OSError:
            pass


def _cleanup_pending_session(pending_path: str) -> None:
    if os.path.isfile(pending_path):
        try:
            os.remove(pending_path)
        except OSError:
            pass


async def _check_or_reuse_existing_session(
    target_path: str,
    pending_path: str,
    api_id_int: int,
    api_hash_str: str,
) -> Tuple[bool, Optional[str]]:
    """Check target and pending session paths for existing authorization."""
    if os.path.isfile(target_path):
        if await is_session_authorized(target_path, api_id_int, api_hash_str):
            logger.info("Session '%s' is already authorized.", target_path)
            return True, "already_authorized"
        raise RuntimeError(
            f"Existing Telegram session file '{target_path}' is present but not authorized. "
            "To protect existing session data, setup will not overwrite it. "
            "Please back up or remove the invalid session file manually before re-authenticating."
        )

    if os.path.isfile(pending_path):
        if await is_session_authorized(pending_path, api_id_int, api_hash_str):
            _promote_pending_session(pending_path, target_path)
            return True, "authorized"
        _cleanup_pending_session(pending_path)

    return False, None


async def _execute_code_request_and_verify(
    client: Any,
    phone: str,
    prompt_fn: Callable[[str], str],
    feedback_fn: Callable[[str], None],
    max_code_retries: int,
) -> bool:
    """Send code request and verify entered code. Returns True if 2FA password is required."""
    from telethon.errors import (
        ApiIdInvalidError,
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        PhoneNumberInvalidError,
        SessionPasswordNeededError,
    )

    try:
        sent_code = await client.send_code_request(phone)
    except (PhoneNumberInvalidError, ApiIdInvalidError) as exc:
        raise RuntimeError(f"Telegram login failed: {exc}") from exc

    phone_code_hash = getattr(sent_code, "phone_code_hash", None)
    feedback_fn(f"Verification code sent to {phone}. Please check your Telegram app or SMS.")

    code_attempts = 0
    while True:
        code = prompt_fn("Enter verification code: ").strip()
        code_attempts += 1
        try:
            if phone_code_hash:
                await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            else:
                await client.sign_in(phone=phone, code=code)
            return False
        except SessionPasswordNeededError:
            return True
        except PhoneCodeExpiredError:
            feedback_fn("Verification code expired. Requesting a new code...")
            sent_code = await client.send_code_request(phone)
            phone_code_hash = getattr(sent_code, "phone_code_hash", None)
            code_attempts = 0
            continue
        except PhoneCodeInvalidError:
            if code_attempts >= max_code_retries:
                raise RuntimeError("Verification failed: maximum code attempts exceeded.")
            remaining = max_code_retries - code_attempts
            feedback_fn(f"Invalid verification code. Please try again ({remaining} attempts remaining):")


async def _execute_2fa_sign_in(
    client: Any,
    password_prompt_fn: Callable[[str], str],
    feedback_fn: Callable[[str], None],
    max_password_retries: int,
) -> None:
    """Handle 2FA password prompt and sign in with retry loop."""
    from telethon.errors import PasswordHashInvalidError

    pwd_attempts = 0
    while True:
        password = password_prompt_fn("Enter Two-Factor Authentication (2FA) password: ").strip()
        pwd_attempts += 1
        try:
            await client.sign_in(password=password)
            return
        except PasswordHashInvalidError:
            if pwd_attempts >= max_password_retries:
                raise RuntimeError("Verification failed: maximum 2FA password attempts exceeded.")
            remaining = max_password_retries - pwd_attempts
            feedback_fn(f"Invalid 2FA password. Please try again ({remaining} attempts remaining):")


async def interactive_login(
    session_path: str,
    api_id: Any,
    api_hash: Any,
    phone: Optional[str] = None,
    *,
    prompt_fn: Optional[Callable[[str], str]] = None,
    password_prompt_fn: Optional[Callable[[str], str]] = None,
    feedback_fn: Optional[Callable[[str], None]] = None,
    max_code_retries: int = 3,
    max_password_retries: int = 3,
) -> str:
    """Perform connected interactive Telegram authentication and safely promote session."""
    api_id_int, api_hash_str = validate_api_credentials(api_id, api_hash)
    target_path = get_sqlite_session_file_path(session_path)
    pending_path = get_pending_session_file_path(session_path)

    done, status = await _check_or_reuse_existing_session(target_path, pending_path, api_id_int, api_hash_str)
    if done and status:
        return status

    pending_dir = os.path.dirname(pending_path)
    if pending_dir and not os.path.exists(pending_dir):
        os.makedirs(pending_dir, exist_ok=True)

    input_prompt = prompt_fn or input
    pwd_prompt = password_prompt_fn or _default_password_prompt
    notify_feedback = feedback_fn or _default_feedback

    if not phone:
        raw_phone = input_prompt("Enter Telegram phone number (with country code, e.g. +48123456789): ")
        phone = validate_phone_number(raw_phone)
    else:
        phone = validate_phone_number(phone)

    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise RuntimeError(
            "Telethon is not installed. Add 'telethon>=1.36' to "
            "resolver/requirements.txt and install it first."
        ) from exc

    client = TelegramClient(
        pending_path, api_id_int, api_hash_str,
        **telegram_client_kwargs(),
    )
    auth_succeeded = False
    try:
        await client.connect()
        if await client.is_user_authorized():
            auth_succeeded = True
            return "already_authorized"

        needs_2fa = await _execute_code_request_and_verify(
            client, phone, input_prompt, notify_feedback, max_code_retries
        )
        if needs_2fa:
            await _execute_2fa_sign_in(client, pwd_prompt, notify_feedback, max_password_retries)

        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized after sign in.")

        auth_succeeded = True
        return "authorized"
    finally:
        try:
            await client.disconnect()
        except Exception:
            logger.debug("Ignored exception in client.disconnect()", exc_info=True)

        if auth_succeeded:
            _promote_pending_session(pending_path, target_path)
        else:
            _cleanup_pending_session(pending_path)


async def require_client(session_state: dict, *, api_id_fn, api_hash_fn, session_path_fn):
    """Connect the singleton client and require an already authorized session."""
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise RuntimeError(
            "Telethon is not installed. Add 'telethon>=1.36' to "
            "resolver/requirements.txt and install it to use the "
            "telegram provider."
        ) from exc
    api_id = api_id_fn()
    api_hash = api_hash_fn()
    if session_state["lock"] is None:
        session_state["lock"] = asyncio.Lock()
    async with session_state["lock"]:
        if session_state["client"] is None:
            session_path = session_path_fn()
            # SQLiteSession creates a file on first connect. Refuse to create a
            # blank session during playback or an admin access check.
            if not os.path.isfile(session_path):
                raise RuntimeError("Telegram session file is missing. Log in first.")
            session_state["client"] = TelegramClient(
                session_path, api_id, api_hash,
                **telegram_client_kwargs(),
            )
        # Telethon can lose its transport while the resolver process remains
        # alive. Keep the singleton, but reconnect it before making requests.
        if not session_state["client"].is_connected():
            await session_state["client"].connect()
    try:
        authorized = await session_state["client"].is_user_authorized()
    except Exception as exc:
        raise RuntimeError(
            "Telegram session check failed. Ensure the session file is "
            "present and logged in (see providers.telegram.login)."
        ) from exc
    if not authorized:
        raise RuntimeError(
            "Telegram session is not authorized. Log in first via "
            "providers.telegram.login(phone) (code from SMS), then retry."
        )
    return session_state["client"]


async def login(
    phone: str,
    code: Optional[str] = None,
    password: Optional[str] = None,
    *,
    api_id_fn,
    api_hash_fn,
    session_path_fn,
) -> str:
    """Perform Telegram's two-step SMS login and disconnect the temporary client."""
    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
    except ImportError as exc:
        raise RuntimeError(
            "Telethon is not installed. Add 'telethon>=1.36' to "
            "resolver/requirements.txt and install it first."
        ) from exc
    session_path = session_path_fn()
    if not phone and code is None:
        if not os.path.isfile(get_sqlite_session_file_path(session_path)):
            return "not_authorized"
    client = TelegramClient(
        session_path, api_id_fn(), api_hash_fn(),
        **telegram_client_kwargs(),
    )
    await client.connect()
    try:
        if await client.is_user_authorized():
            return "already_authorized"
        if not phone:
            return "not_authorized"
        if code is None:
            await client.send_code_request(phone)
            return f"code_sent:{phone}"
        try:
            await client.sign_in(phone, code)
        except SessionPasswordNeededError:
            if not password:
                raise RuntimeError(
                    "Two-factor password required: call login() again "
                    "with password set."
                ) from None
            await client.sign_in(password=password)
        return "authorized"
    finally:
        try:
            await client.disconnect()
        except Exception:
            logger.debug("Ignored exception in login", exc_info=True)
