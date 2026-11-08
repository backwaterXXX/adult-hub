"""Automatic least-loaded eligible-account router and client pool for Telegram MTProto."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Dict, Optional, Tuple

from .telegram_account import TelegramAccountContext, validate_account_id
from .telegram_account_store import (
    get_account,
    list_accounts,
    list_channel_access,
    set_channel_access,
)
from .telegram_config import _entity_ref

logger = logging.getLogger(__name__)

ACCESS_ERROR_NAMES = {
    "ChannelPrivateError",
    "ChatAdminRequiredError",
    "UserNotParticipantError",
    "ChannelInvalidError",
}

NETWORK_ERROR_NAMES = frozenset({
    "TimedOutError",
    "TimeoutError",
    "ServerError",
    "BadGatewayError",
    "ServiceUnavailableError",
    "InternalServerError",
    "ConnectionResetError",
    "BrokenPipeError",
    "ConnectionError",
    "NetworkError",
})

CHANNEL_ACCESS_RECHECK_SECONDS = float(
    os.getenv("TELEGRAM_CHANNEL_RECHECK_SECONDS", 3600.0)
)


class TelegramAccountRouter:
    """Manages active Telethon clients per account, routing requests fairly

    Maintains:
    - Long-lived Telethon clients pinned to the current running event loop.
    - Least-loaded account selection across concurrent streaming and history jobs.
    - Strict account affinity during streaming (never switches account mid-stream).
    - Per-account cooldown when FloodWaitError occurs (never evades waits by retrying on another account).
    - Account-isolated channel access status tracking.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock: Callable[[], float] = clock
        self._clients: Dict[str, Any] = {}
        self._client_locks: Dict[str, asyncio.Lock] = {}
        self._active_leases: Dict[str, int] = {}
        self._cooldown_until: Dict[str, float] = {}
        self._pool_lock = asyncio.Lock()
        self._client_factory: Optional[Callable[..., Any]] = None  # for testing/mocking

    def _get_account_lock(self, account_id: str) -> asyncio.Lock:
        if account_id not in self._client_locks:
            self._client_locks[account_id] = asyncio.Lock()
        return self._client_locks[account_id]

    def record_flood_wait(self, account_id: str, seconds: int) -> None:
        """Record FloodWait wait time on an account.

        Never retry on another account to bypass Telegram flood limits.
        """
        valid_id = validate_account_id(account_id)
        wait_seconds = max(1, int(seconds))
        cooldown = self._clock() + wait_seconds
        self._cooldown_until[valid_id] = max(self._cooldown_until.get(valid_id, 0.0), cooldown)
        logger.warning(
            "Account %s in FloodWait cooldown for %d seconds. Honoring wait without bypass.",
            valid_id,
            wait_seconds,
        )

    def is_account_cooling_down(self, account_id: str) -> bool:
        valid_id = validate_account_id(account_id)
        return self._clock() < self._cooldown_until.get(valid_id, 0.0)

    def get_cooldown_remaining(self, account_id: str) -> int:
        valid_id = validate_account_id(account_id)
        remaining = self._cooldown_until.get(valid_id, 0.0) - self._clock()
        return max(0, int(remaining))

    def clear_cooldown(self, account_id: str) -> None:
        valid_id = validate_account_id(account_id)
        self._cooldown_until.pop(valid_id, None)

    async def _init_and_connect_client(
        self, valid_id: str, session_path: str, ctx: TelegramAccountContext
    ) -> Any:
        if self._client_factory is not None:
            api_id, api_hash = 12345, "0123456789abcdef0123456789abcdef"
            client = self._client_factory(session_path, api_id, api_hash)
        else:
            if not ctx.has_api_credentials():
                raise RuntimeError("Telegram API credentials missing from environment.")
            api_id, api_hash = ctx.get_api_credentials()
            from telethon import TelegramClient
            client = TelegramClient(session_path, api_id, api_hash)

        if hasattr(client, "connect"):
            res = client.connect()
            if asyncio.iscoroutine(res):
                await res

        if hasattr(client, "is_user_authorized"):
            auth_res = client.is_user_authorized()
            authorized = await auth_res if asyncio.iscoroutine(auth_res) else auth_res
            if not authorized:
                raise RuntimeError(f"Telegram session for account '{valid_id}' is not authorized.")
        try:
            object.__setattr__(client, "_account_id", valid_id)
        except Exception:
            try:
                setattr(client, "_account_id", valid_id)
            except Exception:
                pass
        return client

    def _validate_affinity_account(
        self, account_id: str, channel_id: Optional[str] = None
    ) -> str:
        valid_id = validate_account_id(account_id)
        acc = get_account(valid_id)
        if not acc:
            raise RuntimeError(f"Affinity account '{valid_id}' not found.")
        if not acc.enabled:
            raise RuntimeError(f"Affinity account '{valid_id}' is disabled.")
        if self.is_account_cooling_down(valid_id):
            remaining = self.get_cooldown_remaining(valid_id)
            raise RuntimeError(
                f"Affinity account '{valid_id}' is in FloodWait cooldown (wait {remaining}s)."
            )
        session_path = (
            acc.session_file
            if acc.session_file
            else TelegramAccountContext(valid_id).get_session_path()
        )
        if not os.path.isfile(session_path) or os.path.getsize(session_path) == 0:
            raise RuntimeError(
                f"Telegram session file is missing for affinity account '{valid_id}'. Log in first."
            )
        if channel_id:
            clean_channel = str(channel_id).strip()
            records = list_channel_access(account_id=valid_id, channel_id=clean_channel)
            if records:
                status = records[0].access_status
                if status in ("inaccessible", "no_access"):
                    last_checked = records[0].last_checked_at
                    now_ts = datetime.now(timezone.utc).timestamp()
                    recheck_expired = (
                        last_checked is not None
                        and (now_ts - last_checked.timestamp()) > CHANNEL_ACCESS_RECHECK_SECONDS
                    )
                    if not recheck_expired:
                        raise RuntimeError(
                            f"Affinity account '{valid_id}' has confirmed no access to channel {clean_channel}."
                        )
        return valid_id

    async def get_client_for_account(self, account_id: str) -> Any:
        """Obtain or initialize the long-lived Telethon client for account_id."""
        valid_id = validate_account_id(account_id)
        lock = self._get_account_lock(valid_id)
        async with lock:
            client = self._clients.get(valid_id)
            if client is not None:
                if hasattr(client, "is_connected") and not client.is_connected():
                    await client.connect()
                return client

            acc = get_account(valid_id)
            session_path = (
                acc.session_file
                if acc and acc.session_file
                else TelegramAccountContext(valid_id).get_session_path()
            )
            if not os.path.isfile(session_path) or os.path.getsize(session_path) == 0:
                raise RuntimeError(
                    f"Telegram session file is missing for account '{valid_id}'. Log in first."
                )

            ctx = TelegramAccountContext(valid_id, session_path=session_path)
            client = await self._init_and_connect_client(valid_id, session_path, ctx)
            self._clients[valid_id] = client
            return client

    def _is_candidate_eligible(
        self,
        acc,
        excluded: set[str],
        access_map: dict,
        now_ts: float,
    ) -> bool:
        a_id = acc.account_id
        if a_id in excluded or not acc.enabled or self.is_account_cooling_down(a_id):
            return False
        session_path = acc.session_file if acc.session_file else TelegramAccountContext(a_id).get_session_path()
        if not os.path.isfile(session_path) or os.path.getsize(session_path) == 0:
            return False
        acc_rec = access_map.get(a_id)
        if acc_rec and acc_rec.access_status in ("inaccessible", "no_access"):
            last_checked = acc_rec.last_checked_at
            if last_checked and (now_ts - last_checked.timestamp()) < CHANNEL_ACCESS_RECHECK_SECONDS:
                return False
        return True

    async def select_eligible_account(
        self, channel_id: str, *, exclude_accounts: Optional[set[str]] = None
    ) -> str:
        """Select least-loaded eligible account with access to channel_id.

        Considers:
        - Enabled accounts in database
        - Active session file on disk
        - Not in FloodWait cooldown
        - Known channel accessibility status with bounded recheck policy
        """
        accounts = list_accounts()
        if not accounts:
            raise RuntimeError("No Telegram accounts configured. Add or log in to an account.")

        excluded = exclude_accounts or set()
        clean_channel = str(channel_id).strip()

        # Gather access status records
        access_map = {
            rec.account_id: rec
            for rec in list_channel_access(channel_id=clean_channel)
        }

        now_ts = datetime.now(timezone.utc).timestamp()
        candidates = [
            acc.account_id
            for acc in accounts
            if self._is_candidate_eligible(acc, excluded, access_map, now_ts)
        ]

        if not candidates:
            # Check if any account is in cooldown to give accurate error message
            cooldowns = [
                f"{a.account_id} (wait {self.get_cooldown_remaining(a.account_id)}s)"
                for a in accounts
                if self.is_account_cooling_down(a.account_id)
            ]
            if cooldowns:
                raise RuntimeError(
                    f"All eligible Telegram accounts for channel {clean_channel} are cooling down: {', '.join(cooldowns)}"
                )
            raise RuntimeError(
                f"No eligible Telegram accounts available with access to channel {clean_channel}."
            )

        # Prioritize accounts with verified 'accessible' status
        verified = [
            a_id for a_id in candidates
            if access_map.get(a_id) and access_map[a_id].access_status == "accessible"
        ]
        pool = verified if verified else candidates

        # Least-loaded selection (minimum active stream leases)
        selected = min(pool, key=lambda a_id: self._active_leases.get(a_id, 0))
        return selected

    async def select_eligible_account_general(self) -> str:
        """Select least-loaded enabled account not in cooldown when no channel is specified."""
        accounts = list_accounts()
        if not accounts:
            raise RuntimeError("No Telegram accounts configured. Add or log in to an account.")
        candidates = [
            acc.account_id
            for acc in accounts
            if acc.enabled and not self.is_account_cooling_down(acc.account_id)
        ]
        if not candidates:
            cooldowns = [
                f"{a.account_id} (wait {self.get_cooldown_remaining(a.account_id)}s)"
                for a in accounts
                if self.is_account_cooling_down(a.account_id)
            ]
            if cooldowns:
                raise RuntimeError(
                    f"All enabled Telegram accounts are cooling down: {', '.join(cooldowns)}"
                )
            raise RuntimeError("No enabled Telegram accounts available.")
        return min(candidates, key=lambda a_id: self._active_leases.get(a_id, 0))

    async def acquire_client_lease(
        self, *, affinity_account: Optional[str] = None
    ) -> Tuple[str, Any, Callable[[], None]]:
        """Lease a client without channel scoping."""
        if affinity_account:
            account_id = self._validate_affinity_account(affinity_account)
        else:
            account_id = await self.select_eligible_account_general()

        client = await self.get_client_for_account(account_id)
        async with self._pool_lock:
            self._active_leases[account_id] = self._active_leases.get(account_id, 0) + 1

        released = False

        def _release():
            nonlocal released
            if not released:
                released = True
                asyncio.create_task(self._release_lease(account_id))

        return account_id, client, _release

    @asynccontextmanager
    async def channel_lease(
        self, channel_id: str, *, affinity_account: Optional[str] = None
    ) -> AsyncIterator[Tuple[str, Any]]:
        """Context manager leasing a client for channel_id and recording failures."""
        account_id, client, release = await self.acquire_channel_lease(
            channel_id, affinity_account=affinity_account
        )
        try:
            yield account_id, client
        except Exception as exc:
            self.handle_request_exception(account_id, channel_id, exc)
            raise
        finally:
            release()

    @asynccontextmanager
    async def client_lease(
        self, *, affinity_account: Optional[str] = None
    ) -> AsyncIterator[Tuple[str, Any]]:
        """Context manager leasing a client without channel restriction."""
        account_id, client, release = await self.acquire_client_lease(
            affinity_account=affinity_account
        )
        try:
            yield account_id, client
        finally:
            release()

    async def acquire_channel_lease(
        self, channel_id: str, *, affinity_account: Optional[str] = None
    ) -> Tuple[str, Any, Callable[[], None]]:
        """Lease a client with account affinity and least-loaded tracking.

        If affinity_account is provided (e.g. streaming continuation), verifies
        and pins that specific account after validating eligibility and cooldown.
        Returns: (account_id, client, release_fn)
        """
        clean_channel = str(channel_id).strip()
        if affinity_account:
            account_id = self._validate_affinity_account(affinity_account, channel_id=clean_channel)
        else:
            account_id = await self.select_eligible_account(clean_channel)

        client = await self.get_client_for_account(account_id)

        async with self._pool_lock:
            self._active_leases[account_id] = self._active_leases.get(account_id, 0) + 1

        released = False

        def _release():
            nonlocal released
            if not released:
                released = True
                asyncio.create_task(self._release_lease(account_id))

        return account_id, client, _release

    async def _release_lease(self, account_id: str) -> None:
        async with self._pool_lock:
            current = self._active_leases.get(account_id, 0)
            if current > 0:
                self._active_leases[account_id] = current - 1

    def handle_request_exception(
        self, account_id: str, channel_id: str, exc: BaseException
    ) -> None:
        """Classify exceptions and update router state without evading flood waits."""
        valid_id = validate_account_id(account_id)
        clean_channel = str(channel_id).strip()
        exc_type = type(exc).__name__

        # Check FloodWait
        seconds = getattr(exc, "seconds", None)
        if "floodwait" in exc_type.casefold() or seconds is not None:
            wait_time = int(seconds) if seconds else 30
            self.record_flood_wait(valid_id, wait_time)
            set_channel_access(valid_id, clean_channel, "flood_wait", str(exc))
            return

        # Check Access / Permissions
        if exc_type in ACCESS_ERROR_NAMES:
            logger.warning(
                "Channel %s is inaccessible for account %s: %s",
                clean_channel,
                valid_id,
                exc,
            )
            set_channel_access(valid_id, clean_channel, "no_access", str(exc))
            return

        # Check Network / Transport errors
        if isinstance(exc, (OSError, asyncio.TimeoutError)) or exc_type in NETWORK_ERROR_NAMES:
            logger.debug(
                "Channel %s network/transient error for account %s: %s",
                clean_channel,
                valid_id,
                exc,
            )
            set_channel_access(valid_id, clean_channel, "temporary_error", str(exc))
            return

        # Local validation errors (ValueError, TypeError), expired/invalid file references,
        # and other non-network exceptions must NOT alter channel access visibility!
        logger.debug(
            "Channel %s local or reference exception for account %s (%s: %s), access status unchanged",
            clean_channel,
            valid_id,
            exc_type,
            exc,
        )

    async def verify_channel_access(self, account_id: str, channel_id: str) -> str:
        """Verify read access for an account against a channel using metadata only (no media download)."""
        valid_id = validate_account_id(account_id)
        clean_channel = str(channel_id).strip()
        try:
            client = await self.get_client_for_account(valid_id)
            entity = await client.get_entity(_entity_ref(clean_channel))
            await client.get_messages(entity, limit=1)
            set_channel_access(valid_id, clean_channel, "accessible", "")
            return "accessible"
        except Exception as exc:
            self.handle_request_exception(valid_id, clean_channel, exc)
            exc_type = type(exc).__name__
            if "floodwait" in exc_type.casefold() or getattr(exc, "seconds", None) is not None:
                return "flood_wait"
            if exc_type in ACCESS_ERROR_NAMES:
                return "no_access"
            return "temporary_error"

    async def reset_account_client(self, account_id: str) -> None:
        valid_id = validate_account_id(account_id)
        lock = self._get_account_lock(valid_id)
        async with lock:
            old_client = self._clients.pop(valid_id, None)
            if old_client is not None:
                try:
                    if hasattr(old_client, "is_connected") and old_client.is_connected():
                        await asyncio.wait_for(old_client.disconnect(), timeout=5.0)
                    elif hasattr(old_client, "disconnect"):
                        res = old_client.disconnect()
                        if asyncio.iscoroutine(res):
                            await asyncio.wait_for(res, timeout=5.0)
                except Exception as exc:
                    logger.debug("Error disconnecting Telegram client %s: %s", valid_id, exc)

    async def disconnect_all(self) -> None:
        """Disconnect all pooled clients gracefully on shutdown."""
        async with self._pool_lock:
            account_ids = list(self._clients.keys())
        for a_id in account_ids:
            await self.reset_account_client(a_id)


_GLOBAL_ROUTER: Optional[TelegramAccountRouter] = None


def get_telegram_router() -> TelegramAccountRouter:
    global _GLOBAL_ROUTER
    if _GLOBAL_ROUTER is None:
        _GLOBAL_ROUTER = TelegramAccountRouter()
    return _GLOBAL_ROUTER


async def reset_telegram_router() -> None:
    """Disconnect and clear all clients in the global router."""
    global _GLOBAL_ROUTER
    if _GLOBAL_ROUTER is not None:
        await _GLOBAL_ROUTER.disconnect_all()
        _GLOBAL_ROUTER = None
