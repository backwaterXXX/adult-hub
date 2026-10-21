"""Bounded retries for Telegram downloads that have not sent any bytes yet.

Retrying mid-stream would corrupt an HTTP response whose earlier bytes already
reached the client, so every helper here re-runs only work that completes
before delivery: whole range requests (nothing yielded yet) or single download
blocks (bytes not forwarded yet).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("telegram.retry")

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 0.5
MAX_BACKOFF_SECONDS = 4.0
MAX_FLOOD_WAIT_SECONDS = 15.0
MAX_TOTAL_WAIT_SECONDS = 20.0

# Matched against exception class names so this module imports without telethon.
_RETRYABLE_TYPE_NAMES = frozenset({
    "FloodWaitError",
    "FileReferenceExpiredError",
    "FileReferenceInvalidError",
    "TimedOutError",
    "InvalidBufferError",
    "ServerError",
    "BadGatewayError",
    "ServiceUnavailableError",
    "InternalServerError",
    "ConnectionResetError",
    "BrokenPipeError",
})


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (OSError, asyncio.TimeoutError)):
        return True
    if type(exc).__name__ in _RETRYABLE_TYPE_NAMES:
        return True
    return type(exc).__name__ == "FloodWaitError" and _flood_seconds(exc) is not None


def _flood_seconds(exc: BaseException) -> Optional[float]:
    seconds = getattr(exc, "seconds", None)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    return float(seconds)


def retry_delay(exc: BaseException, attempt: int) -> float:
    flood = _flood_seconds(exc) if type(exc).__name__ == "FloodWaitError" else None
    if flood is not None:
        return flood
    return min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)


def _gives_up(exc: BaseException, delay: float) -> bool:
    flood = _flood_seconds(exc) if type(exc).__name__ == "FloodWaitError" else None
    return flood is not None and flood > MAX_FLOOD_WAIT_SECONDS


async def retry_stream(factory: Callable[[], "object"], *,
                       on_retry: Optional[Callable[[BaseException, int], None]] = None,
                       retryable: Optional[Callable[[BaseException], bool]] = None):
    """Yield chunks from factory(); rerun from scratch only while nothing was yielded."""
    yielded = False
    total_wait = 0.0
    is_allowed = retryable or is_retryable
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            async for chunk in factory():
                yielded = True
                yield chunk
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if yielded or attempt >= MAX_ATTEMPTS or not is_allowed(exc):
                raise
            delay = retry_delay(exc, attempt)
            if _gives_up(exc, delay) or total_wait + delay > MAX_TOTAL_WAIT_SECONDS:
                raise
            logger.warning(
                "Telegram download attempt %d failed (%s: %s); retrying in %.1fs",
                attempt, type(exc).__name__, exc, delay)
            total_wait += delay
            await asyncio.sleep(delay)
            if on_retry is not None:
                on_retry(exc, attempt)


async def retry_call(func: Callable[[], Awaitable], *,
                     on_retry: Optional[Callable[[BaseException, int], None]] = None,
                     retryable: Optional[Callable[[BaseException], bool]] = None):
    """Await func() with bounded retries for glitchy Telegram failures."""
    total_wait = 0.0
    is_allowed = retryable or is_retryable
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return await func()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if attempt >= MAX_ATTEMPTS or not is_allowed(exc):
                raise
            delay = retry_delay(exc, attempt)
            if _gives_up(exc, delay) or total_wait + delay > MAX_TOTAL_WAIT_SECONDS:
                raise
            logger.warning(
                "Telegram fetch attempt %d failed (%s: %s); retrying in %.1fs",
                attempt, type(exc).__name__, exc, delay)
            total_wait += delay
            await asyncio.sleep(delay)
            if on_retry is not None:
                on_retry(exc, attempt)
