"""Transient, bounded, account-scoped Telegram web login challenge manager."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Dict

from common.config import get_telegram_login_timeout
from resolver.providers.telegram_account import DEFAULT_ACCOUNT_ID, TelegramAccountContext
from resolver.providers.telegram_authorization import invalidate as invalidate_account_authorization
from resolver.providers.telegram_login_flow import (
    ACCOUNT_COOLDOWN_SECONDS,
    DISCONNECT_TIMEOUT_SECONDS,
    LOGIN_RESEND_TTL_SECONDS,
    LOGIN_TTL_SECONDS,
    MAX_ACCOUNT_STARTS,
    MAX_ATTEMPTS,
    MAX_RESENDS,
    ActiveLoginChallenge,
    TelegramLoginFlowMixin,
    mask_phone_number,
)
from resolver.providers.telegram_session import (
    _cleanup_pending_session,
    _promote_pending_session,
    has_session_auth_key,
    is_session_authorized,
    validate_phone_number,
)

logger = logging.getLogger(__name__)

UNAUTHORIZED_BACKUP_MARKER = ".unauthorized-backup-"
SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

__all__ = [
    "ACCOUNT_COOLDOWN_SECONDS",
    "ActiveLoginChallenge",
    "DEFAULT_ACCOUNT_ID",
    "DISCONNECT_TIMEOUT_SECONDS",
    "LOGIN_RESEND_TTL_SECONDS",
    "LOGIN_TTL_SECONDS",
    "MAX_ACCOUNT_STARTS",
    "MAX_ATTEMPTS",
    "MAX_RESENDS",
    "SQLITE_SIDECAR_SUFFIXES",
    "TelegramLoginFlowMixin",
    "TelegramLoginManager",
    "UNAUTHORIZED_BACKUP_MARKER",
    "_cleanup_pending_session",
    "_fast_local_auth_check",
    "_move_session_aside",
    "_promote_pending_session",
    "get_telegram_login_manager",
    "has_session_auth_key",
    "is_session_authorized",
    "mask_phone_number",
    "validate_phone_number",
]


def _move_session_aside(target_path: str) -> str:
    """Rename a session file and its SQLite sidecars to one free timestamped backup name.

    Nothing is deleted or overwritten: a candidate name is used only when neither the
    main backup nor any of its sidecars exists. If a rename fails, the files already
    moved are renamed back. Returns the backup file name without its directory.
    """
    directory, base = os.path.split(target_path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_base = None
    for index in range(1, 1000):
        candidate = f"{base}{UNAUTHORIZED_BACKUP_MARKER}{stamp}" if index == 1 else (
            f"{base}{UNAUTHORIZED_BACKUP_MARKER}{stamp}-{index}"
        )
        names = [candidate, *(candidate + suffix for suffix in SQLITE_SIDECAR_SUFFIXES)]
        if not any(os.path.lexists(os.path.join(directory, name)) for name in names):
            backup_base = candidate
            break
    if backup_base is None:
        raise OSError("No free backup file name was found.")

    # Sidecars move first and the main file last, so a failure leaves the canonical name in place.
    moves = [
        (target_path + suffix, os.path.join(directory, backup_base + suffix))
        for suffix in SQLITE_SIDECAR_SUFFIXES
        if os.path.lexists(target_path + suffix)
    ]
    moves.append((target_path, os.path.join(directory, backup_base)))

    done: list[tuple[str, str]] = []
    try:
        for source, destination in moves:
            os.replace(source, destination)
            done.append((source, destination))
    except OSError:
        for source, destination in reversed(done):
            try:
                os.replace(destination, source)
            except OSError:
                logger.error("Could not restore session file %s after a failed move.", os.path.basename(source))
        raise
    return backup_base


def _fast_local_auth_check(account_id: str, target_path: str) -> bool:
    try:
        from resolver.providers.telegram_router import get_telegram_router
        if account_id in get_telegram_router()._clients:
            return True
    except Exception:
        pass
    return has_session_auth_key(target_path)


class TelegramLoginManager(TelegramLoginFlowMixin):
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
        if state is None or now >= state.get("reset_at", 0):
            state = {
                "failed_attempts": 0,
                "resends": 0,
                "starts": 0,
                "cooldown_until": 0.0,
                "reset_at": now + ACCOUNT_COOLDOWN_SECONDS,
            }
            self._account_limits[account_id] = state
        return state

    def _record_failed_attempt(self, account_id: str) -> int:
        state = self._get_account_limit_state(account_id)
        state["failed_attempts"] += 1
        return state["failed_attempts"]

    def _record_resend(self, account_id: str) -> int:
        state = self._get_account_limit_state(account_id)
        state["resends"] += 1
        return state["resends"]

    def _set_flood_wait(self, account_id: str, seconds: int) -> None:
        state = self._get_account_limit_state(account_id)
        state["cooldown_until"] = max(state["cooldown_until"], time.time() + float(seconds))

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
            if cached is None and _fast_local_auth_check(ctx.account_id, target_path):
                cached = True
                self._cached_auth[ctx.account_id] = True

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
            login_timeout = get_telegram_login_timeout()
            authorized = await asyncio.wait_for(
                is_session_authorized(target_path, api_id, api_hash, raise_on_error=True),
                timeout=login_timeout,
            )
        except ValueError:
            # Not a SQLite session file, so Telegram cannot accept it as a session.
            authorized = False
        except Exception as exc:
            # The check could not finish (network, locked database). That is not a verdict,
            # so the cached result stays as it was.
            logger.info("Live session status could not complete for account %s (%s)", ctx.account_id, type(exc).__name__)
            return {
                "ok": True,
                "account_id": ctx.account_id,
                "status": "check_failed",
                "authorized": False,
                "has_session_file": True,
                "api_configured": True,
                "message": "Could not verify the Telegram session right now. The session file was not changed. Try again in a moment.",
            }
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
                "Use Replace unauthorized session to move it to a backup, then log in again."
            ),
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

    async def replace_unauthorized_session(self, account_id: str = DEFAULT_ACCOUNT_ID) -> dict:
        """Move an unauthorized session file to a backup so a new login can start.

        The live check runs again inside the account lock, and the file moves only when
        Telegram reports the session as not authorized. Cached auth state is never used
        for this decision. Nothing is deleted: the old file and its sidecars are renamed.
        """
        try:
            ctx = TelegramAccountContext(account_id)
        except ValueError as exc:
            return {"ok": False, "code": "invalid_account_id", "error": str(exc)}

        from resolver.providers.telegram_account_store import get_account

        record = get_account(ctx.account_id)
        if record is None:
            return {"ok": False, "code": "not_found", "error": f"Account '{ctx.account_id}' not found."}
        if not record.enabled:
            return {
                "ok": False,
                "code": "account_disabled",
                "error": "Account is disabled. Enable it before replacing its session.",
            }

        target_path = ctx.get_session_path()
        account_lock = self._get_account_lock(ctx.account_id)
        async with account_lock:
            async with self._manager_lock:
                if ctx.account_id in self._challenges:
                    return {
                        "ok": False,
                        "code": "login_in_progress",
                        "error": "A Telegram login is in progress for this account. Cancel it before replacing the session.",
                    }

            if not (os.path.isfile(target_path) and os.path.getsize(target_path) > 0):
                return {
                    "ok": False,
                    "code": "no_session_file",
                    "error": "No session file exists for this account. Start login instead.",
                }
            if not ctx.has_api_credentials():
                return {
                    "ok": False,
                    "code": "check_failed",
                    "error": "Telegram API credentials are missing, so the session could not be checked. The file was not changed.",
                }

            try:
                api_id, api_hash = ctx.get_api_credentials()
                authorized = await asyncio.wait_for(
                    is_session_authorized(target_path, api_id, api_hash, raise_on_error=True),
                    timeout=get_telegram_login_timeout(),
                )
            except Exception as exc:
                logger.warning("Live session check failed for account %s (%s)", ctx.account_id, type(exc).__name__)
                return {
                    "ok": False,
                    "code": "check_failed",
                    "error": "Could not verify the current session with Telegram. The file was not changed. Try again later.",
                }

            self._cached_auth[ctx.account_id] = bool(authorized)
            if authorized:
                return {
                    "ok": False,
                    "code": "session_authorized",
                    "error": "The Telegram session is authorized, so it will not be replaced.",
                }

            try:
                backup_name = _move_session_aside(target_path)
            except OSError as exc:
                logger.error("Could not move unauthorized session for account %s (%s)", ctx.account_id, type(exc).__name__)
                return {
                    "ok": False,
                    "code": "move_failed",
                    "error": "The session file could not be moved to a backup. It was left in place.",
                }

            self._cached_auth.pop(ctx.account_id, None)
            invalidate_account_authorization(ctx.account_id)
            logger.info("Moved unauthorized session for account %s to backup %s", ctx.account_id, backup_name)

            await self._reset_clients_after_session_move(ctx.account_id)

        return {
            "ok": True,
            "account_id": ctx.account_id,
            "backup_file": backup_name,
            "message": "The unauthorized session was kept as a backup. Log in to authenticate this account.",
        }

    async def _reset_clients_after_session_move(self, account_id: str) -> None:
        """Drop any Telegram client that still points at the moved session file."""
        try:
            from resolver.providers.telegram_router import get_telegram_router
            await get_telegram_router().reset_account_client(account_id)
        except Exception as exc:
            logger.warning("Could not reset Telegram router client for account %s (%s)",
                           account_id, type(exc).__name__)
        if account_id == DEFAULT_ACCOUNT_ID:
            try:
                from resolver.providers.telegram import reset_telegram_client
                res = reset_telegram_client("default")
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.warning("Could not reset default Telegram client after session move (%s)",
                                type(exc).__name__)

    async def _finalize_authorization(self, challenge: ActiveLoginChallenge) -> None:
        account_lock = self._get_account_lock(challenge.account_id)
        async with account_lock:
            await self._safe_disconnect(challenge.client)

            _promote_pending_session(challenge.pending_path, challenge.target_path)

            # Persist authorized account to storage without overwriting existing label or disabling state
            try:
                from resolver.providers.telegram_account_store import upsert_account
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
                invalidate_account_authorization(challenge.account_id)
                raise RuntimeError(f"Failed to persist authorized account to database: {store_err}") from store_err

            challenge.state = "authorized"
            self._cached_auth[challenge.account_id] = True
            invalidate_account_authorization(challenge.account_id)
            self._clear_account_limits(challenge.account_id)

            async with self._manager_lock:
                self._challenges.pop(challenge.account_id, None)

            # Reset only the target account client in the router pool
            try:
                from resolver.providers.telegram_router import get_telegram_router
                await get_telegram_router().reset_account_client(challenge.account_id)
            except Exception as exc:
                logger.warning("Could not reset Telegram router client after login for account %s (%s)",
                               challenge.account_id, type(exc).__name__)
            if challenge.account_id == "default":
                try:
                    from resolver.providers.telegram import reset_telegram_client
                    res = reset_telegram_client("default")
                    if asyncio.iscoroutine(res):
                        await res
                except Exception as exc:
                    logger.warning("Could not reset default Telegram client after login (%s)",
                                   type(exc).__name__)


# Singleton instance for resolver process
_GLOBAL_LOGIN_MANAGER = TelegramLoginManager()


def get_telegram_login_manager() -> TelegramLoginManager:
    return _GLOBAL_LOGIN_MANAGER
