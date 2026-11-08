"""Transient, bounded, account-scoped Telegram web login challenge manager."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

from .telegram_account import DEFAULT_ACCOUNT_ID, TelegramAccountContext
from .telegram_session import (
    _cleanup_pending_session,
    _promote_pending_session,
    is_session_authorized,
    validate_phone_number,
)

logger = logging.getLogger(__name__)

LOGIN_TTL_SECONDS = 600.0
LOGIN_RESEND_TTL_SECONDS = 300.0
MAX_ATTEMPTS = 5
MAX_RESENDS = 3
LOGIN_NETWORK_TIMEOUT_SECONDS = 25.0
DISCONNECT_TIMEOUT_SECONDS = 5.0
MAX_ACCOUNT_STARTS = 5
ACCOUNT_COOLDOWN_SECONDS = 300.0


def mask_phone_number(phone: str) -> str:
    """Format phone number with middle digits masked for safe display."""
    cleaned = "".join(phone.split())
    if len(cleaned) <= 6:
        return cleaned[:2] + "***" + cleaned[-1:]
    return f"{cleaned[:3]} *** *** {cleaned[-3:]}"


class ActiveLoginChallenge:
    """In-memory transient state for an ongoing Telegram login attempt."""

    def __init__(
        self,
        account_id: str,
        phone: str,
        phone_code_hash: str | None,
        client: Any,
        pending_path: str,
        target_path: str,
        ttl_seconds: float = LOGIN_TTL_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        max_resends: int = MAX_RESENDS,
        initial_attempts: int = 0,
        initial_resends: int = 0,
    ) -> None:
        self.account_id = account_id
        self.phone = phone
        self.phone_mask = mask_phone_number(phone)
        self.phone_code_hash = phone_code_hash
        self.client = client
        self.pending_path = pending_path
        self.target_path = target_path
        self.created_at = time.time()
        self.absolute_deadline = self.created_at + ttl_seconds
        self.expires_at = self.absolute_deadline
        self.max_attempts = max_attempts
        self.attempts = initial_attempts
        self.max_resends = max_resends
        self.resends = initial_resends
        self.state = "code_requested"  # code_requested | password_needed | authorized
        self.lock = asyncio.Lock()

    @property
    def is_expired(self) -> bool:
        now = time.time()
        return now >= self.expires_at or now >= self.absolute_deadline

    @property
    def remaining_seconds(self) -> int:
        now = time.time()
        return max(0, int(min(self.expires_at, self.absolute_deadline) - now))


class TelegramLoginManager:
    """Thread-safe manager for transient Telegram login challenges.

    Maintains the default account boundary so challenges are indexed by
    account identity, while keeping phone codes, passwords, and API credentials
    strictly out of persistent storage and logs.
    """

    def __init__(self) -> None:
        self._challenges: Dict[str, ActiveLoginChallenge] = {}
        self._cached_auth: Dict[str, bool] = {}
        self._manager_lock = asyncio.Lock()
        self._account_locks: Dict[str, asyncio.Lock] = {}
        self._account_limits: Dict[str, dict] = {}

    def _get_account_lock(self, account_id: str) -> asyncio.Lock:
        if account_id not in self._account_locks:
            self._account_locks[account_id] = asyncio.Lock()
        return self._account_locks[account_id]

    def _get_account_limit_state(self, account_id: str) -> dict:
        state = self._account_limits.get(account_id)
        now = time.time()
        if not state or (state.get("cooldown_until", 0) > 0 and now >= state["cooldown_until"]):
            state = {
                "failed_attempts": 0,
                "starts": 0,
                "resends": 0,
                "cooldown_until": 0.0,
            }
            self._account_limits[account_id] = state
        return state

    def _record_failed_attempt(self, account_id: str) -> int:
        state = self._get_account_limit_state(account_id)
        state["failed_attempts"] += 1
        if state["failed_attempts"] >= MAX_ATTEMPTS:
            state["cooldown_until"] = time.time() + ACCOUNT_COOLDOWN_SECONDS
        return state["failed_attempts"]

    def _record_resend(self, account_id: str) -> int:
        state = self._get_account_limit_state(account_id)
        state["resends"] += 1
        if state["resends"] >= MAX_RESENDS:
            state["cooldown_until"] = time.time() + ACCOUNT_COOLDOWN_SECONDS
        return state["resends"]

    def _set_flood_wait(self, account_id: str, seconds: int) -> None:
        state = self._get_account_limit_state(account_id)
        state["cooldown_until"] = max(state.get("cooldown_until", 0.0), time.time() + seconds)

    def _clear_account_limits(self, account_id: str) -> None:
        self._account_limits.pop(account_id, None)

    async def _get_session_file_status(
        self,
        ctx: TelegramAccountContext,
        target_path: str,
        has_creds: bool,
        check_live: bool,
    ) -> dict:
        if not has_creds:
            return {
                "ok": True,
                "account_id": ctx.account_id,
                "status": "unauthorized_session_present",
                "authorized": False,
                "has_session_file": True,
                "api_configured": False,
                "message": "Existing session file is present, but Telegram API credentials are missing from environment.",
            }

        cached = self._cached_auth.get(ctx.account_id)
        if not check_live:
            if cached is True:
                return {
                    "ok": True,
                    "account_id": ctx.account_id,
                    "status": "authorized",
                    "authorized": True,
                    "has_session_file": True,
                    "api_configured": True,
                    "message": "Telegram session is authorized (cached).",
                }
            elif cached is False:
                return {
                    "ok": True,
                    "account_id": ctx.account_id,
                    "status": "unauthorized_session_present",
                    "authorized": False,
                    "has_session_file": True,
                    "api_configured": True,
                    "message": "Existing Telegram session file is present but not authorized (cached).",
                }
            return {
                "ok": True,
                "account_id": ctx.account_id,
                "status": "session_file_present",
                "authorized": False,
                "has_session_file": True,
                "api_configured": True,
                "message": "Session file is present. Live verification can be requested via Telegram auth status.",
            }

        try:
            api_id, api_hash = ctx.get_api_credentials()
            authorized = await asyncio.wait_for(
                is_session_authorized(target_path, api_id, api_hash),
                timeout=LOGIN_NETWORK_TIMEOUT_SECONDS,
            )
            self._cached_auth[ctx.account_id] = bool(authorized)
            if authorized:
                return {
                    "ok": True,
                    "account_id": ctx.account_id,
                    "status": "authorized",
                    "authorized": True,
                    "has_session_file": True,
                    "api_configured": True,
                    "message": "Telegram session is authorized.",
                }
            return {
                "ok": True,
                "account_id": ctx.account_id,
                "status": "unauthorized_session_present",
                "authorized": False,
                "has_session_file": True,
                "api_configured": True,
                "message": (
                    "Existing Telegram session file is present but not authorized. "
                    "To protect existing session data, setup will not overwrite it. "
                    "Please back up or remove the invalid session file manually before re-authenticating."
                ),
            }
        except Exception as exc:
            logger.debug("Error checking existing session for %s: %s", target_path, exc)
            return {
                "ok": True,
                "account_id": ctx.account_id,
                "status": "unauthorized_session_present",
                "authorized": False,
                "has_session_file": True,
                "api_configured": True,
                "message": "Existing Telegram session file could not be verified.",
            }

    async def get_status(
        self,
        account_id: str = DEFAULT_ACCOUNT_ID,
        check_live: bool = True,
    ) -> dict:
        """Inspect the authorization status for an account without exposing secrets.

        Keeps status listing fast and local when check_live=False. Live MTProto
        connectivity probing is performed when check_live=True (default for auth status).
        """
        try:
            ctx = TelegramAccountContext(account_id)
        except ValueError as exc:
            return {
                "ok": False,
                "account_id": str(account_id),
                "code": "invalid_account_id",
                "error": str(exc),
            }

        target_path = ctx.get_session_path()
        has_creds = ctx.has_api_credentials()

        if os.path.isfile(target_path) and os.path.getsize(target_path) > 0:
            return await self._get_session_file_status(ctx, target_path, has_creds, check_live)

        if not has_creds:
            return {
                "ok": True,
                "account_id": ctx.account_id,
                "status": "unconfigured",
                "authorized": False,
                "has_session_file": False,
                "api_configured": False,
                "message": "TELEGRAM_API_ID and TELEGRAM_API_HASH must be configured in the environment.",
            }

        # Check in-flight challenge
        challenge_to_clean = None
        async with self._manager_lock:
            challenge = self._challenges.get(ctx.account_id)
            if challenge:
                if challenge.is_expired:
                    challenge_to_clean = self._challenges.pop(ctx.account_id, None)
                else:
                    return {
                        "ok": True,
                        "account_id": ctx.account_id,
                        "status": "in_progress",
                        "authorized": False,
                        "login_state": challenge.state,
                        "phone_mask": challenge.phone_mask,
                        "expires_in_seconds": challenge.remaining_seconds,
                        "has_session_file": False,
                        "api_configured": True,
                    }

        if challenge_to_clean:
            account_lock = self._get_account_lock(ctx.account_id)
            async with account_lock:
                await self._cleanup_challenge(challenge_to_clean)

        return {
            "ok": True,
            "account_id": ctx.account_id,
            "status": "ready",
            "authorized": False,
            "has_session_file": False,
            "api_configured": True,
            "message": "Ready to initiate Telegram authentication.",
        }

    async def _check_start_session_preconditions(
        self, ctx: TelegramAccountContext, target_path: str
    ) -> Optional[dict]:
        if os.path.isfile(target_path) and os.path.getsize(target_path) > 0:
            if ctx.has_api_credentials():
                try:
                    api_id, api_hash = ctx.get_api_credentials()
                    authorized = await asyncio.wait_for(
                        is_session_authorized(target_path, api_id, api_hash),
                        timeout=LOGIN_NETWORK_TIMEOUT_SECONDS,
                    )
                    self._cached_auth[ctx.account_id] = bool(authorized)
                    if authorized:
                        return {
                            "ok": False,
                            "code": "already_authorized",
                            "error": "Telegram session is already authorized. Re-authentication is not permitted.",
                        }
                except Exception as exc:
                    logger.debug("Precondition session check failed: %s", exc)
                    if self._cached_auth.get(ctx.account_id) is True:
                        return {
                            "ok": False,
                            "code": "already_authorized",
                            "error": "Telegram session is already authorized. Re-authentication is not permitted.",
                        }
            elif self._cached_auth.get(ctx.account_id) is True:
                return {
                    "ok": False,
                    "code": "already_authorized",
                    "error": "Telegram session is already authorized. Re-authentication is not permitted.",
                }
            return {
                "ok": False,
                "code": "unauthorized_session_present",
                "error": (
                    "Existing Telegram session file is present but unauthorized. "
                    "Setup will not overwrite it. Please back up or remove the session file manually."
                ),
            }
        return None

    async def _safe_disconnect(self, client: Any) -> None:
        try:
            if client is not None:
                if hasattr(client, "is_connected") and client.is_connected():
                    await asyncio.wait_for(client.disconnect(), timeout=DISCONNECT_TIMEOUT_SECONDS)
                elif hasattr(client, "disconnect"):
                    res = client.disconnect()
                    if asyncio.iscoroutine(res):
                        await asyncio.wait_for(res, timeout=DISCONNECT_TIMEOUT_SECONDS)
        except Exception:
            pass

    async def _safe_disconnect_and_clean(self, client: Any, pending_path: str) -> None:
        await self._safe_disconnect(client)
        _cleanup_pending_session(pending_path)

    async def _send_code_request(
        self,
        client: Any,
        ctx: TelegramAccountContext,
        normalized_phone: str,
        pending_path: str,
        target_path: str,
        initial_attempts: int = 0,
        initial_resends: int = 0,
    ) -> dict:
        from telethon.errors import (
            ApiIdInvalidError,
            FloodWaitError,
            PhoneNumberInvalidError,
        )

        try:
            await asyncio.wait_for(client.connect(), timeout=LOGIN_NETWORK_TIMEOUT_SECONDS)
            if await asyncio.wait_for(client.is_user_authorized(), timeout=LOGIN_NETWORK_TIMEOUT_SECONDS):
                await self._safe_disconnect(client)
                _promote_pending_session(pending_path, target_path)
                self._cached_auth[ctx.account_id] = True
                self._clear_account_limits(ctx.account_id)
                return {
                    "ok": True,
                    "account_id": ctx.account_id,
                    "state": "authorized",
                    "message": "Session was already authorized.",
                }

            sent_code = await asyncio.wait_for(
                client.send_code_request(normalized_phone),
                timeout=LOGIN_NETWORK_TIMEOUT_SECONDS,
            )
            phone_code_hash = getattr(sent_code, "phone_code_hash", None)

            challenge = ActiveLoginChallenge(
                account_id=ctx.account_id,
                phone=normalized_phone,
                phone_code_hash=phone_code_hash,
                client=client,
                pending_path=pending_path,
                target_path=target_path,
                initial_attempts=initial_attempts,
                initial_resends=initial_resends,
            )
            async with self._manager_lock:
                self._challenges[ctx.account_id] = challenge

            return {
                "ok": True,
                "account_id": ctx.account_id,
                "state": challenge.state,
                "phone_mask": challenge.phone_mask,
                "expires_in_seconds": challenge.remaining_seconds,
                "message": f"Verification code sent to {challenge.phone_mask}.",
            }

        except (asyncio.TimeoutError, TimeoutError):
            await self._safe_disconnect_and_clean(client, pending_path)
            return {"ok": False, "code": "timeout", "error": "Telegram server timed out while sending code."}

        except (PhoneNumberInvalidError, ApiIdInvalidError) as exc:
            await self._safe_disconnect_and_clean(client, pending_path)
            return {"ok": False, "code": "invalid_credentials", "error": f"Telegram login failed: {exc}"}

        except FloodWaitError as exc:
            self._set_flood_wait(ctx.account_id, exc.seconds)
            await self._safe_disconnect_and_clean(client, pending_path)
            return {
                "ok": False,
                "code": "flood_wait",
                "flood_wait_seconds": exc.seconds,
                "error": f"Telegram rate limit: please wait {exc.seconds} seconds before requesting a code.",
            }

        except Exception as exc:
            logger.warning("Unexpected error during Telegram code request: %s", exc, exc_info=True)
            await self._safe_disconnect_and_clean(client, pending_path)
            return {"ok": False, "code": "request_failed", "error": f"Could not request verification code: {exc}"}

    @staticmethod
    def _validate_start_login_params(
        account_id: str,
        phone: str,
    ) -> tuple[Optional[TelegramAccountContext], Optional[str], Optional[dict]]:
        try:
            ctx = TelegramAccountContext(account_id)
        except ValueError as exc:
            return None, None, {"ok": False, "code": "invalid_account_id", "error": str(exc)}

        try:
            normalized_phone = validate_phone_number(phone)
        except ValueError as exc:
            return None, None, {"ok": False, "code": "invalid_phone", "error": str(exc)}

        return ctx, normalized_phone, None

    def _check_account_start_rate_limit(self, limit_state: dict) -> Optional[dict]:
        now = time.time()
        if limit_state["cooldown_until"] > now:
            wait_sec = int(limit_state["cooldown_until"] - now)
            return {
                "ok": False,
                "code": "flood_wait",
                "flood_wait_seconds": wait_sec,
                "error": f"Authentication rate limit active. Please wait {wait_sec} seconds before trying again.",
            }

        if limit_state["starts"] >= MAX_ACCOUNT_STARTS:
            limit_state["cooldown_until"] = now + ACCOUNT_COOLDOWN_SECONDS
            wait_sec = int(ACCOUNT_COOLDOWN_SECONDS)
            return {
                "ok": False,
                "code": "flood_wait",
                "flood_wait_seconds": wait_sec,
                "error": f"Too many authentication starts. Please wait {wait_sec} seconds before trying again.",
            }
        return None

    @staticmethod
    def _resolve_telegram_client_factory(
        ctx: TelegramAccountContext,
    ) -> tuple[Any, Optional[int], Optional[str], Optional[dict]]:
        if not ctx.has_api_credentials():
            return None, None, None, {
                "ok": False,
                "code": "credentials_missing",
                "error": "Telegram API ID and API Hash are not configured in the server environment.",
            }

        try:
            api_id_int, api_hash_str = ctx.get_api_credentials()
        except Exception as exc:
            return None, None, None, {"ok": False, "code": "invalid_credentials", "error": str(exc)}

        try:
            from telethon import TelegramClient
        except ImportError as exc:
            logger.error("Telethon is not installed: %s", exc)
            return None, None, None, {
                "ok": False,
                "code": "telethon_missing",
                "error": "Telethon library is not installed in resolver runtime.",
            }

        return TelegramClient, api_id_int, api_hash_str, None

    async def _reset_existing_challenge(
        self, account_id: str, limit_state: dict
    ) -> Optional[dict]:
        # Serialize cleanup of existing challenge under manager lock, but disconnect outside
        async with self._manager_lock:
            existing = self._challenges.pop(account_id, None)

        if existing:
            async with existing.lock:
                if existing.state == "authorized":
                    return {
                        "ok": False,
                        "code": "already_authorized",
                        "error": "Telegram session is already authorized. Re-authentication is not permitted.",
                    }
                limit_state["failed_attempts"] = max(limit_state["failed_attempts"], existing.attempts)
                limit_state["resends"] = max(limit_state["resends"], existing.resends)
                await self._cleanup_challenge(existing)
        return None

    async def start_login(
        self,
        phone: str,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict:
        """Initiate the login challenge by requesting a verification code via MTProto."""
        ctx, normalized_phone, param_err = self._validate_start_login_params(account_id, phone)
        if param_err:
            return param_err

        target_path = ctx.get_session_path()
        pending_path = ctx.get_pending_session_path()

        account_lock = self._get_account_lock(ctx.account_id)
        async with account_lock:
            # Check preconditions under account_lock so concurrent promotion cannot race
            precondition_err = await self._check_start_session_preconditions(ctx, target_path)
            if precondition_err:
                return precondition_err

            # Check account limits and cooldown across restarts/cancellations
            limit_state = self._get_account_limit_state(ctx.account_id)
            rate_err = self._check_account_start_rate_limit(limit_state)
            if rate_err:
                return rate_err

            client_cls, api_id_int, api_hash_str, cred_err = self._resolve_telegram_client_factory(ctx)
            if cred_err:
                return cred_err

            existing_err = await self._reset_existing_challenge(ctx.account_id, limit_state)
            if existing_err:
                return existing_err

            limit_state["starts"] += 1
            _cleanup_pending_session(pending_path)

            pending_dir = os.path.dirname(pending_path)
            if pending_dir:
                os.makedirs(pending_dir, exist_ok=True)

            client = client_cls(pending_path, api_id_int, api_hash_str)
            # Perform network I/O OUTSIDE manager lock with explicit timeout
            return await self._send_code_request(
                client,
                ctx,
                normalized_phone,
                pending_path,
                target_path,
                initial_attempts=limit_state["failed_attempts"],
                initial_resends=limit_state["resends"],
            )

    async def _sign_in_with_code(
        self, challenge: ActiveLoginChallenge, code_str: str
    ) -> dict:
        from telethon.errors import (
            FloodWaitError,
            PhoneCodeExpiredError,
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )

        try:
            if challenge.phone_code_hash:
                await asyncio.wait_for(
                    challenge.client.sign_in(
                        phone=challenge.phone,
                        code=code_str,
                        phone_code_hash=challenge.phone_code_hash,
                    ),
                    timeout=LOGIN_NETWORK_TIMEOUT_SECONDS,
                )
            else:
                await asyncio.wait_for(
                    challenge.client.sign_in(
                        phone=challenge.phone,
                        code=code_str,
                    ),
                    timeout=LOGIN_NETWORK_TIMEOUT_SECONDS,
                )

            if await asyncio.wait_for(challenge.client.is_user_authorized(), timeout=LOGIN_NETWORK_TIMEOUT_SECONDS):
                await self._finalize_authorization(challenge)
                return {
                    "ok": True,
                    "account_id": challenge.account_id,
                    "state": "authorized",
                    "message": "Telegram authentication successful.",
                }
            return {"ok": False, "code": "not_authorized", "error": "Session unauthorized after code verification."}

        except (asyncio.TimeoutError, TimeoutError):
            return {"ok": False, "code": "timeout", "error": "Telegram server timed out during code verification."}

        except SessionPasswordNeededError:
            challenge.state = "password_needed"
            challenge.attempts = 0
            return {
                "ok": True,
                "account_id": challenge.account_id,
                "state": "password_needed",
                "message": "Two-factor authentication (2FA) password required.",
            }

        except PhoneCodeInvalidError:
            challenge.attempts += 1
            self._record_failed_attempt(challenge.account_id)
            if challenge.attempts >= challenge.max_attempts:
                await self.cancel_login(challenge.account_id)
                return {
                    "ok": False,
                    "code": "max_attempts",
                    "error": "Maximum verification attempts exceeded. Please restart login.",
                }
            remaining = challenge.max_attempts - challenge.attempts
            return {
                "ok": False,
                "code": "invalid_code",
                "remaining_attempts": remaining,
                "error": f"Invalid verification code. {remaining} attempt(s) remaining.",
            }

        except PhoneCodeExpiredError:
            return {
                "ok": False,
                "code": "code_expired",
                "error": "Verification code has expired. Please request a new code.",
            }

        except FloodWaitError as exc:
            self._set_flood_wait(challenge.account_id, exc.seconds)
            return {
                "ok": False,
                "code": "flood_wait",
                "flood_wait_seconds": exc.seconds,
                "error": f"Telegram rate limit: please wait {exc.seconds} seconds.",
            }

        except Exception as exc:
            logger.warning("Unexpected error during code verification: %s", exc)
            return {"ok": False, "code": "verification_error", "error": f"Code verification failed: {exc}"}

    async def submit_code(
        self,
        code: str,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict:
        """Verify the received code. Never logs or persists the code."""
        code_str = (code or "").strip()
        if not code_str:
            return {"ok": False, "code": "empty_code", "error": "Verification code cannot be empty."}

        challenge = await self._get_valid_challenge(account_id)
        if not challenge:
            return {
                "ok": False,
                "code": "challenge_expired",
                "error": "Login session has expired or was not started. Please request a new code.",
            }

        async with challenge.lock:
            if challenge.is_expired:
                await self.cancel_login(account_id)
                return {
                    "ok": False,
                    "code": "challenge_expired",
                    "error": "Login session has expired. Please request a new code.",
                }

            return await self._sign_in_with_code(challenge, code_str)

    async def submit_password(
        self,
        password: str,
        account_id: str = DEFAULT_ACCOUNT_ID,
    ) -> dict:
        """Verify the 2FA password. Never logs or persists the password.

        Preserves exact password bytes/characters (does NOT strip surrounding whitespace).
        Only rejects empty password input.
        """
        pwd_str = password if isinstance(password, str) else str(password or "")
        if not pwd_str:
            return {"ok": False, "code": "empty_password", "error": "2FA password cannot be empty."}

        challenge = await self._get_valid_challenge(account_id)
        if not challenge or challenge.state != "password_needed":
            return {
                "ok": False,
                "code": "invalid_state",
                "error": "No 2FA password challenge is pending. Please verify your phone code first.",
            }

        async with challenge.lock:
            if challenge.is_expired:
                await self.cancel_login(account_id)
                return {
                    "ok": False,
                    "code": "challenge_expired",
                    "error": "Login session has expired. Please restart login.",
                }

            from telethon.errors import (
                FloodWaitError,
                PasswordHashInvalidError,
            )

            try:
                await asyncio.wait_for(
                    challenge.client.sign_in(password=pwd_str),
                    timeout=LOGIN_NETWORK_TIMEOUT_SECONDS,
                )
                if await asyncio.wait_for(challenge.client.is_user_authorized(), timeout=LOGIN_NETWORK_TIMEOUT_SECONDS):
                    await self._finalize_authorization(challenge)
                    return {
                        "ok": True,
                        "account_id": challenge.account_id,
                        "state": "authorized",
                        "message": "Telegram 2FA authentication successful.",
                    }
                return {"ok": False, "code": "not_authorized", "error": "Session unauthorized after 2FA sign in."}

            except (asyncio.TimeoutError, TimeoutError):
                return {"ok": False, "code": "timeout", "error": "Telegram server timed out during 2FA verification."}

            except PasswordHashInvalidError:
                challenge.attempts += 1
                self._record_failed_attempt(challenge.account_id)
                if challenge.attempts >= challenge.max_attempts:
                    await self.cancel_login(account_id)
                    return {
                        "ok": False,
                        "code": "max_attempts",
                        "error": "Maximum 2FA password attempts exceeded. Please restart login.",
                    }
                remaining = challenge.max_attempts - challenge.attempts
                return {
                    "ok": False,
                    "code": "invalid_password",
                    "remaining_attempts": remaining,
                    "error": f"Invalid 2FA password. {remaining} attempt(s) remaining.",
                }

            except FloodWaitError as exc:
                self._set_flood_wait(challenge.account_id, exc.seconds)
                return {
                    "ok": False,
                    "code": "flood_wait",
                    "flood_wait_seconds": exc.seconds,
                    "error": f"Telegram rate limit: please wait {exc.seconds} seconds.",
                }

            except Exception as exc:
                logger.warning("Unexpected error during 2FA password sign in: %s", exc)
                return {"ok": False, "code": "verification_error", "error": f"2FA verification failed: {exc}"}

    async def resend_code(self, account_id: str = DEFAULT_ACCOUNT_ID) -> dict:
        """Resend a new verification code for the active challenge."""
        challenge = await self._get_valid_challenge(account_id)
        if not challenge:
            return {
                "ok": False,
                "code": "challenge_expired",
                "error": "Login session has expired or was not started. Please restart login.",
            }

        async with challenge.lock:
            if challenge.is_expired:
                await self.cancel_login(account_id)
                return {
                    "ok": False,
                    "code": "challenge_expired",
                    "error": "Login session has expired. Please restart login.",
                }

            if challenge.resends >= challenge.max_resends:
                return {
                    "ok": False,
                    "code": "max_resends_exceeded",
                    "error": "Maximum verification code resends exceeded. Please restart login.",
                }

            from telethon.errors import FloodWaitError

            try:
                sent_code = await asyncio.wait_for(
                    challenge.client.send_code_request(challenge.phone),
                    timeout=LOGIN_NETWORK_TIMEOUT_SECONDS,
                )
                challenge.phone_code_hash = getattr(sent_code, "phone_code_hash", None)
                challenge.resends += 1
                self._record_resend(challenge.account_id)
                # Bounded deadline extension: never exceed absolute_deadline!
                challenge.expires_at = min(time.time() + LOGIN_RESEND_TTL_SECONDS, challenge.absolute_deadline)
                # Note: DO NOT reset challenge.attempts; preserve lifetime attempts across resends!
                return {
                    "ok": True,
                    "account_id": challenge.account_id,
                    "expires_in_seconds": challenge.remaining_seconds,
                    "resends_remaining": challenge.max_resends - challenge.resends,
                    "remaining_attempts": challenge.max_attempts - challenge.attempts,
                    "message": f"A new verification code has been sent to {challenge.phone_mask}.",
                }
            except (asyncio.TimeoutError, TimeoutError):
                return {"ok": False, "code": "timeout", "error": "Telegram server timed out while resending code."}
            except FloodWaitError as exc:
                self._set_flood_wait(challenge.account_id, exc.seconds)
                return {
                    "ok": False,
                    "code": "flood_wait",
                    "flood_wait_seconds": exc.seconds,
                    "error": f"Telegram rate limit: please wait {exc.seconds} seconds before requesting another code.",
                }
            except Exception as exc:
                logger.warning("Failed to resend Telegram code: %s", exc)
                return {"ok": False, "code": "resend_failed", "error": f"Could not resend code: {exc}"}

    async def cancel_login(self, account_id: str = DEFAULT_ACCOUNT_ID) -> dict:
        """Cancel an in-flight challenge and safely clean up isolated pending files."""
        account_lock = self._get_account_lock(account_id)
        async with account_lock:
            async with self._manager_lock:
                challenge = self._challenges.pop(account_id, None)
            if challenge:
                limit_state = self._get_account_limit_state(account_id)
                limit_state["failed_attempts"] = max(limit_state["failed_attempts"], challenge.attempts)
                limit_state["resends"] = max(limit_state["resends"], challenge.resends)
                await self._cleanup_challenge(challenge)
                return {"ok": True, "message": "Login challenge cancelled and pending state cleaned up."}
            return {"ok": True, "message": "No active login challenge found."}

    async def _get_valid_challenge(self, account_id: str) -> Optional[ActiveLoginChallenge]:
        challenge_to_clean = None
        async with self._manager_lock:
            challenge = self._challenges.get(account_id)
            if not challenge:
                return None
            if challenge.is_expired:
                challenge_to_clean = self._challenges.pop(account_id, None)
                challenge = None

        if challenge_to_clean:
            account_lock = self._get_account_lock(account_id)
            async with account_lock:
                await self._cleanup_challenge(challenge_to_clean)
            return None
        return challenge

    async def _cleanup_challenge(self, challenge: ActiveLoginChallenge) -> None:
        await self._safe_disconnect(challenge.client)
        _cleanup_pending_session(challenge.pending_path)

    async def _finalize_authorization(self, challenge: ActiveLoginChallenge) -> None:
        account_lock = self._get_account_lock(challenge.account_id)
        async with account_lock:
            await self._safe_disconnect(challenge.client)

            _promote_pending_session(challenge.pending_path, challenge.target_path)

            # Persist authorized account to storage without overwriting existing label or disabling state
            try:
                from .telegram_account_store import upsert_account
                upsert_account(
                    challenge.account_id,
                    phone_mask=challenge.phone_mask,
                    session_file=challenge.target_path,
                )
            except Exception as store_err:
                logger.error("Could not persist authorized account to store: %s", store_err)
                if os.path.isfile(challenge.target_path):
                    try:
                        os.remove(challenge.target_path)
                    except OSError:
                        pass
                self._cached_auth.pop(challenge.account_id, None)
                raise RuntimeError(f"Failed to persist authorized account to database: {store_err}") from store_err

            challenge.state = "authorized"
            self._cached_auth[challenge.account_id] = True
            self._clear_account_limits(challenge.account_id)

            async with self._manager_lock:
                self._challenges.pop(challenge.account_id, None)

            # Reset only the target account client in the router pool
            try:
                from .telegram_router import get_telegram_router
                await get_telegram_router().reset_account_client(challenge.account_id)
            except Exception:
                pass
            if challenge.account_id == "default":
                try:
                    from .telegram import reset_telegram_client
                    res = reset_telegram_client("default")
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    pass


# Singleton instance for resolver process
_GLOBAL_LOGIN_MANAGER = TelegramLoginManager()


def get_telegram_login_manager() -> TelegramLoginManager:
    return _GLOBAL_LOGIN_MANAGER
