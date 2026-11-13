"""Per-account Telegram authorization checks with a short in-process cache.

The router uses these checks to skip accounts whose session is not logged in, so a
stale or unauthorized session cannot be picked for a stream or history request.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from resolver.providers.telegram_account import TelegramAccountContext, validate_account_id
from resolver.providers.telegram_account_store import get_account
from resolver.providers.telegram_session import is_session_authorized

logger = logging.getLogger(__name__)

POSITIVE_CACHE_SECONDS = 300.0
NEGATIVE_CACHE_SECONDS = 60.0
CHECK_CONCURRENCY = 4

# account_id -> (monotonic expiry, authorized). Guarded by _lock.
_cache: Dict[str, Tuple[float, bool]] = {}
# account_id -> generation. invalidate() bumps it so a check that started earlier cannot
# write its (possibly stale) result back into the cache.
_generations: Dict[str, int] = {}
# account_id -> the in-flight check task, so concurrent callers share one Telegram probe.
_inflight: Dict[str, "asyncio.Future[bool]"] = {}
_lock = threading.Lock()
_clock: Callable[[], float] = time.monotonic


def _forget_inflight(account_id: str, task: "asyncio.Future[bool]", _done: Any) -> None:
    with _lock:
        if _inflight.get(account_id) is task:
            _inflight.pop(account_id, None)


async def _probe_account(account_id: str) -> Optional[bool]:
    """Ask Telegram whether the account's session file is logged in.

    Returns True or False for a definite answer (including missing credentials and a session
    file that is not a database). Returns None when the check could not be completed, such as a
    network timeout or a locked database. None is never cached as "not authorized".
    """
    from common.config import get_telegram_login_timeout

    try:
        record = get_account(account_id)
        ctx = TelegramAccountContext(account_id)
        if not ctx.has_api_credentials():
            return False
        session_path = record.session_file if record and record.session_file else ctx.get_session_path()
        api_id, api_hash = ctx.get_api_credentials()
        authorized = await asyncio.wait_for(
            is_session_authorized(session_path, api_id, api_hash, raise_on_error=True),
            timeout=get_telegram_login_timeout(),
        )
        return bool(authorized)
    except ValueError:
        # A file without the SQLite header is not a session that Telegram could accept.
        return False
    except Exception as exc:
        logger.info("Telegram authorization check could not complete for account %s (%s)", account_id, type(exc).__name__)
        return None


async def _run_check(account_id: str, generation: int) -> bool:
    authorized = await _probe_account(account_id)
    if authorized is None:
        return False
    ttl = POSITIVE_CACHE_SECONDS if authorized else NEGATIVE_CACHE_SECONDS
    with _lock:
        if _generations.get(account_id, 0) == generation:
            _cache[account_id] = (_clock() + ttl, authorized)
    return authorized


async def is_account_authorized(account_id: str) -> bool:
    """Return True when the account's session is logged in. Results are cached per account.

    Positive results are reused for POSITIVE_CACHE_SECONDS and negative results for
    NEGATIVE_CACHE_SECONDS. Errors and missing credentials count as not authorized.
    """
    try:
        valid_id = validate_account_id(account_id)
    except ValueError:
        return False

    loop = asyncio.get_running_loop()
    with _lock:
        cached = _cache.get(valid_id)
        if cached is not None and cached[0] > _clock():
            return cached[1]
        task = _inflight.get(valid_id)
        if task is None or task.get_loop() is not loop:
            # A task left over from a loop that has since closed is never awaited here.
            generation = _generations.get(valid_id, 0)
            task = asyncio.ensure_future(_run_check(valid_id, generation))
            _inflight[valid_id] = task
            task.add_done_callback(functools.partial(_forget_inflight, valid_id, task))
    return await asyncio.shield(task)


def invalidate(account_id: str) -> None:
    """Drop the cached result for an account so the next check asks Telegram again."""
    key = str(account_id or "").strip()
    with _lock:
        _cache.pop(key, None)
        _inflight.pop(key, None)
        _generations[key] = _generations.get(key, 0) + 1


def _enabled_account_ids(account_ids: Iterable[str]) -> List[str]:
    enabled: List[str] = []
    for raw_id in account_ids:
        try:
            valid_id = validate_account_id(raw_id)
        except ValueError:
            continue
        record = get_account(valid_id)
        if record is not None and record.enabled and valid_id not in enabled:
            enabled.append(valid_id)
    return enabled


async def authorized_account_ids(account_ids: Iterable[str]) -> List[str]:
    """Return the enabled, authorized accounts from account_ids, in input order."""
    enabled = _enabled_account_ids(account_ids)
    if not enabled:
        return []
    semaphore = asyncio.Semaphore(CHECK_CONCURRENCY)

    async def _check(account_id: str) -> bool:
        async with semaphore:
            return await is_account_authorized(account_id)

    results = await asyncio.gather(*(_check(account_id) for account_id in enabled))
    return [account_id for account_id, ok in zip(enabled, results) if ok]
