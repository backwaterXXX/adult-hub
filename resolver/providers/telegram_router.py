"""Automatic least-loaded eligible-account router and client pool for Telegram MTProto."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Dict, Optional, Tuple

from resolver.providers.telegram_account import TelegramAccountContext, validate_account_id
from resolver.providers.telegram_account_store import (
    get_account,
    list_accounts,
    list_channel_access,
    set_channel_access,
)
from resolver.providers.telegram_config import _entity_ref

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

_RAW_MAX_LEASES = os.getenv("TELEGRAM_MAX_CONCURRENT_LEASES_PER_ACCOUNT", "20").strip()
try:
    _parsed_leases = int(_RAW_MAX_LEASES)
    if _parsed_leases <= 0:
        logger.warning(
            "Invalid TELEGRAM_MAX_CONCURRENT_LEASES_PER_ACCOUNT=%r; must be a positive integer. Falling back to 20.",
            _RAW_MAX_LEASES,
        )
        DEFAULT_MAX_LEASES_PER_ACCOUNT: int = 20
    else:
        DEFAULT_MAX_LEASES_PER_ACCOUNT = _parsed_leases
except ValueError:
    logger.warning(
        "Invalid TELEGRAM_MAX_CONCURRENT_LEASES_PER_ACCOUNT=%r; must be an integer. Falling back to 20.",
        _RAW_MAX_LEASES,
    )
    DEFAULT_MAX_LEASES_PER_ACCOUNT = 20


def _redact_error_detail(detail: str) -> str:
    """Scrub tokens, absolute paths, and phone numbers from error detail."""
    if not detail:
        return ""
    # Redact URLs
    text = re.sub(r"https?://[^\s\]\[()<>\"']+", "[link redacted]", detail)
    # Redact Telegram bot tokens (e.g. 123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11)
    text = re.sub(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b", "[redacted-token]", text)
    # Redact Bearer tokens
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", text)
    # Redact key-value secrets (token=..., api_key=..., secret=..., etc.)
    text = re.sub(
        r"(?i)(\b(?:token|api[_-]?key|api[_-]?hash|password|secret|auth[_-]?key)\s*[=:]\s*)[^\s,;]+",
        r"\1[redacted]",
        text,
    )
    # Redact POSIX absolute paths
    text = re.sub(r"(?<![A-Za-z0-9])/(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*", "[path redacted]", text)
    # Redact Windows absolute paths
    text = re.sub(r"\b[A-Za-z]:\\(?:[^\s\\]+\\)*[^\s,;]+", "[path redacted]", text)
    # Redact relative or basename .session file references
    text = re.sub(r"\b[A-Za-z0-9_.-]+\.session\b", "[session redacted]", text)
    # Redact phone-like number sequences (7 to 15 digits with optional '+' and spaces/dashes),
    # excluding Telegram channel IDs (negative integers, e.g. -1001234567890)
    text = re.sub(r"(?<![-\w])\+?\d[\d\s\-]{5,13}\d(?!\w)", "[phone redacted]", text)
    return text[:128]


class TelegramAccountRouter:
    """Manages active Telethon clients per account, routing requests fairly

    Maintains:
    - Long-lived Telethon clients pinned to the current running event loop.
    - Least-loaded account selection across concurrent streaming and history jobs.
    - Strict account affinity during streaming (never switches account mid-stream).
    - Per-account cooldown when FloodWaitError occurs (never evades waits by retrying on another account).
    - Account-isolated channel access status tracking.
    - Bounded in-memory operational metrics per account (active ops, bytes, errors, TTFB, throughput).
    - Bounded per-account and global concurrency limits.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_leases_per_account: Optional[int] = None,
    ) -> None:
        self._clock: Callable[[], float] = clock
        if max_leases_per_account is not None:
            if max_leases_per_account <= 0:
                raise ValueError(
                    f"max_leases_per_account must be > 0, got {max_leases_per_account}"
                )
            self._max_leases_per_account: Optional[int] = max_leases_per_account
        else:
            self._max_leases_per_account = DEFAULT_MAX_LEASES_PER_ACCOUNT
        self._clients: Dict[str, Any] = {}
        self._client_locks: Dict[str, asyncio.Lock] = {}
        self._active_leases: Dict[str, int] = {}
        self._cooldown_until: Dict[str, float] = {}
        self._bytes_transferred: Dict[str, int] = {}
        self._error_counts: Dict[str, int] = {}
        self._flood_wait_counts: Dict[str, int] = {}
        self._last_errors: Dict[str, Tuple[float, str, str]] = {}
        self._buffer_init_latencies: Dict[str, list[float]] = {}
        self._throughput_window: Dict[str, list[Tuple[float, int]]] = {}
        self._pool_lock = asyncio.Lock()
        self._lease_event = asyncio.Event()
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
        self._flood_wait_counts[valid_id] = self._flood_wait_counts.get(valid_id, 0) + 1
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

    def record_bytes(self, account_id: str, count: int) -> None:
        """Record transferred bytes for account."""
        valid_id = validate_account_id(account_id)
        bytes_count = max(0, int(count))
        self._bytes_transferred[valid_id] = self._bytes_transferred.get(valid_id, 0) + bytes_count
        now = self._clock()
        window = self._throughput_window.setdefault(valid_id, [])
        window.append((now, bytes_count))
        cutoff = now - 60.0
        while window and window[0][0] < cutoff:
            window.pop(0)
        if len(window) > 100:
            mid = len(window) // 2
            compacted_bytes = sum(b for _, b in window[:mid])
            window = [(window[mid - 1][0], compacted_bytes)] + window[mid:]
            self._throughput_window[valid_id] = window

    def get_recent_throughput_mb_s(self, account_id: str) -> float:
        """Calculate throughput in MB/s over recent window."""
        valid_id = validate_account_id(account_id)
        window = self._throughput_window.get(valid_id)
        if not window or len(window) < 1:
            return 0.0
        now = self._clock()
        cutoff = now - 60.0
        recent = [(t, b) for t, b in window if t >= cutoff]
        if not recent:
            return 0.0
        total_bytes = sum(b for _, b in recent)
        duration = max(0.5, now - recent[0][0])
        mb_s = (total_bytes / (1024 * 1024)) / duration
        return round(mb_s, 2)

    def record_buffer_init_latency(self, account_id: str, latency_ms: float) -> None:
        """Record TTFB / initial buffer latency in ms."""
        valid_id = validate_account_id(account_id)
        lat = max(0.0, float(latency_ms))
        samples = self._buffer_init_latencies.setdefault(valid_id, [])
        samples.append(lat)
        if len(samples) > 20:
            samples.pop(0)

    def get_buffer_init_latency_ms(self, account_id: str) -> Optional[float]:
        """Return average buffer-init latency (TTFB) in ms across recent streams."""
        valid_id = validate_account_id(account_id)
        samples = self._buffer_init_latencies.get(valid_id)
        if not samples:
            return None
        return round(sum(samples) / len(samples), 1)

    def record_error(self, account_id: str, exc_type: str, detail: str = "") -> None:
        """Record bounded error occurrence without sensitive data."""
        valid_id = validate_account_id(account_id)
        self._error_counts[valid_id] = self._error_counts.get(valid_id, 0) + 1
        clean_type = str(exc_type)[:64]
        clean_detail = _redact_error_detail(str(detail))
        self._last_errors[valid_id] = (self._clock(), clean_type, clean_detail)

    def get_account_metrics(self, account_id: str) -> Dict[str, Any]:
        """Retrieve bounded in-memory metrics for account."""
        valid_id = validate_account_id(account_id)
        last_err = self._last_errors.get(valid_id)
        return {
            "active_ops": self._active_leases.get(valid_id, 0),
            "bytes_transferred": self._bytes_transferred.get(valid_id, 0),
            "errors_count": self._error_counts.get(valid_id, 0),
            "flood_wait_count": self._flood_wait_counts.get(valid_id, 0),
            "cooldown_seconds": self.get_cooldown_remaining(valid_id),
            "throughput_mb_s": self.get_recent_throughput_mb_s(valid_id),
            "buffer_init_latency_ms": self.get_buffer_init_latency_ms(valid_id),
            "last_error": {
                "timestamp": round(last_err[0], 2),
                "type": last_err[1],
                "detail": last_err[2],
            } if last_err else None,
        }

    def get_all_metrics(self) -> Dict[str, Dict[str, Any]]:
        """Retrieve metrics for all known accounts."""
        all_ids = (
            set(self._active_leases.keys())
            | set(self._bytes_transferred.keys())
            | set(self._error_counts.keys())
            | set(self._cooldown_until.keys())
        )
        return {a_id: self.get_account_metrics(a_id) for a_id in all_ids}

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
                logger.debug("Could not tag Telegram client with account %s", valid_id, exc_info=True)
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
        if (
            self._max_leases_per_account is not None
            and self._active_leases.get(a_id, 0) >= self._max_leases_per_account
        ):
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
        - Bounded concurrency limit per account
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
            at_capacity = [
                f"{a.account_id} ({self._active_leases.get(a.account_id, 0)} ops)"
                for a in accounts
                if self._max_leases_per_account is not None
                and self._active_leases.get(a.account_id, 0) >= self._max_leases_per_account
            ]
            if at_capacity:
                raise RuntimeError(
                    f"All eligible Telegram accounts for channel {clean_channel} reached concurrency limit: {', '.join(at_capacity)}"
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
            if acc.enabled
            and not self.is_account_cooling_down(acc.account_id)
            and (
                self._max_leases_per_account is None
                or self._active_leases.get(acc.account_id, 0) < self._max_leases_per_account
            )
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
            at_capacity = [
                f"{a.account_id} ({self._active_leases.get(a.account_id, 0)} ops)"
                for a in accounts
                if self._max_leases_per_account is not None
                and self._active_leases.get(a.account_id, 0) >= self._max_leases_per_account
            ]
            if at_capacity:
                raise RuntimeError(
                    f"All enabled Telegram accounts reached concurrency limit: {', '.join(at_capacity)}"
                )
            raise RuntimeError("No enabled Telegram accounts available.")
        return min(candidates, key=lambda a_id: self._active_leases.get(a_id, 0))

    async def acquire_client_lease(
        self, *, affinity_account: Optional[str] = None, timeout: float = 30.0
    ) -> Tuple[str, Any, Callable[[], None]]:
        """Lease a client without channel scoping."""
        if affinity_account:
            account_id = self._validate_affinity_account(affinity_account)
        else:
            account_id = await self.select_eligible_account_general()

        client = await self.get_client_for_account(account_id)
        if self._max_leases_per_account is not None:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                async with self._pool_lock:
                    if self._active_leases.get(account_id, 0) < self._max_leases_per_account:
                        self._active_leases[account_id] = self._active_leases.get(account_id, 0) + 1
                        break
                    wait_evt = self._lease_event
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RuntimeError(
                        f"Affinity account '{account_id}' reached concurrency limit ({self._max_leases_per_account} operations) and wait timed out."
                    )
                try:
                    await asyncio.wait_for(wait_evt.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        f"Affinity account '{account_id}' reached concurrency limit ({self._max_leases_per_account} operations) and wait timed out."
                    )
        else:
            async with self._pool_lock:
                self._active_leases[account_id] = self._active_leases.get(account_id, 0) + 1

        released = False

        def _release() -> None:
            nonlocal released
            if not released:
                released = True
                self._release_lease_sync(account_id)

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
        except Exception as exc:
            self.handle_request_exception(account_id, "", exc)
            raise
        finally:
            release()

    async def acquire_channel_lease(
        self, channel_id: str, *, affinity_account: Optional[str] = None, timeout: float = 30.0
    ) -> Tuple[str, Any, Callable[[], None]]:
        """Lease a client with account affinity and least-loaded tracking.

        If affinity_account is provided (e.g. streaming continuation), verifies
        and pins that specific account after validating eligibility and cooldown.
        Waits safely with backpressure if the account is currently at the per-account lease cap.
        Returns: (account_id, client, release_fn)
        """
        clean_channel = str(channel_id).strip()
        if affinity_account:
            account_id = self._validate_affinity_account(affinity_account, channel_id=clean_channel)
        else:
            account_id = await self.select_eligible_account(clean_channel)

        client = await self.get_client_for_account(account_id)

        if self._max_leases_per_account is not None:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                async with self._pool_lock:
                    if self._active_leases.get(account_id, 0) < self._max_leases_per_account:
                        self._active_leases[account_id] = self._active_leases.get(account_id, 0) + 1
                        break
                    wait_evt = self._lease_event
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise RuntimeError(
                        f"Affinity account '{account_id}' reached concurrency limit ({self._max_leases_per_account} operations) and wait timed out."
                    )
                try:
                    await asyncio.wait_for(wait_evt.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        f"Affinity account '{account_id}' reached concurrency limit ({self._max_leases_per_account} operations) and wait timed out."
                    )
        else:
            async with self._pool_lock:
                self._active_leases[account_id] = self._active_leases.get(account_id, 0) + 1

        released = False

        def _release() -> None:
            nonlocal released
            if not released:
                released = True
                self._release_lease_sync(account_id)

        return account_id, client, _release

    def _release_lease_sync(self, account_id: str) -> None:
        """Synchronously release lease, decrement counter, and wake waiters."""
        current = self._active_leases.get(account_id, 0)
        if current > 0:
            self._active_leases[account_id] = current - 1
        evt = self._lease_event
        self._lease_event = asyncio.Event()
        evt.set()

    async def _release_lease(self, account_id: str) -> None:
        """Async compatibility wrapper."""
        self._release_lease_sync(account_id)

    def handle_request_exception(
        self, account_id: str, channel_id: str, exc: BaseException
    ) -> None:
        """Classify exceptions and update router state without evading flood waits."""
        if getattr(exc, "_router_handled", False):
            return
        try:
            object.__setattr__(exc, "_router_handled", True)
        except Exception:
            try:
                setattr(exc, "_router_handled", True)
            except Exception:
                logger.debug("Could not mark exception as handled by Telegram router", exc_info=True)
        valid_id = validate_account_id(account_id)
        clean_channel = str(channel_id).strip()
        exc_type = type(exc).__name__
        self.record_error(valid_id, exc_type, str(exc))

        # Check FloodWait
        seconds = getattr(exc, "seconds", None)
        if "floodwait" in exc_type.casefold() or seconds is not None:
            wait_time = int(seconds) if seconds else 30
            self.record_flood_wait(valid_id, wait_time)
            if clean_channel:
                set_channel_access(valid_id, clean_channel, "flood_wait", str(exc))
            return

        # Check Access / Permissions
        if exc_type in ACCESS_ERROR_NAMES:
            logger.warning(
                "Channel %s is inaccessible for account %s: %s",
                clean_channel or "<general>",
                valid_id,
                exc,
            )
            if clean_channel:
                set_channel_access(valid_id, clean_channel, "no_access", str(exc))
            return

        # Check Network / Transport errors
        if isinstance(exc, (OSError, asyncio.TimeoutError)) or exc_type in NETWORK_ERROR_NAMES:
            logger.debug(
                "Channel %s network/transient error for account %s: %s",
                clean_channel or "<general>",
                valid_id,
                exc,
            )
            if clean_channel:
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
