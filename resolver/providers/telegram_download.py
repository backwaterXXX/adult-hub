"""Telegram MTProto download sizing and optional parallel stream helpers."""
import inspect
import os
from typing import Optional
import logging

logger = logging.getLogger(__name__)


def _is_parallel_stream_enabled() -> bool:
    val = (os.getenv("TELEGRAM_PARALLEL_STREAM_ENABLED") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def _get_stream_manager_config() -> dict:
    def _int_env(name: str, default: int) -> int:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            return default
        try:
            val = int(raw)
            return val if val > 0 else default
        except ValueError:
            return default

    def _float_env(name: str, default: float) -> float:
        raw = (os.getenv(name) or "").strip()
        if not raw:
            return default
        try:
            val = float(raw)
            return val if val > 0 else default
        except ValueError:
            return default

    raw_acc_workers = (os.getenv("TELEGRAM_DOWNLOAD_ACCOUNT_WORKERS") or "").strip()
    account_workers: Optional[int] = None
    if raw_acc_workers:
        try:
            val = int(raw_acc_workers)
            if val <= 0:
                logger.warning(
                    "Invalid TELEGRAM_DOWNLOAD_ACCOUNT_WORKERS=%r; must be a positive integer. Normalizing to None.",
                    raw_acc_workers,
                )
                account_workers = None
            else:
                account_workers = val
        except ValueError:
            logger.warning(
                "Invalid TELEGRAM_DOWNLOAD_ACCOUNT_WORKERS=%r; must be an integer. Normalizing to None.",
                raw_acc_workers,
            )
            account_workers = None

    return {
        "workers": _int_env("TELEGRAM_DOWNLOAD_WORKERS", 3),
        "global_workers": _int_env("TELEGRAM_DOWNLOAD_GLOBAL_WORKERS", 6),
        "block_size": _int_env("TELEGRAM_DOWNLOAD_BLOCK_BYTES", 524288),
        "read_ahead_bytes": _int_env("TELEGRAM_READ_AHEAD_BYTES", 33554432),
        "max_cache_bytes": _int_env("TELEGRAM_STREAM_CACHE_BYTES", 134217728),
        "cache_ttl_seconds": _float_env("TELEGRAM_STREAM_CACHE_TTL_SECONDS", 120.0),
        "account_workers": account_workers,
    }


_global_stream_manager = None


def _get_global_stream_manager():
    global _global_stream_manager
    if _global_stream_manager is None:
        try:
            # Deployed layout: resolver/ contents are the app root, so the
            # manager module is top-level.
            from telegram_stream import ParallelTelegramStreamManager
        except ImportError:
            try:
                from ..telegram_stream import ParallelTelegramStreamManager
            except (ImportError, ValueError):
                from resolver.telegram_stream import ParallelTelegramStreamManager
        cfg = _get_stream_manager_config()
        _global_stream_manager = ParallelTelegramStreamManager(**cfg)
    return _global_stream_manager


LEGAL_REQUEST_SIZES = (4096, 8192, 16384, 32768, 65536, 131072, 262144, 524288)


def _legal_request_size(size: Optional[int]) -> int:
    """Return a legal power-of-two Telegram request size (4096..524288)."""
    if size is None or size >= 524288:
        return 524288
    if size <= 4096:
        return 4096
    return 1 << (size - 1).bit_length()


async def _close_download_iterator(iterator):
    if iterator is None:
        return
    if hasattr(iterator, "close"):
        try:
            res = iterator.close()
            if inspect.isawaitable(res):
                await res
        except Exception:
            logger.debug("Ignored exception in _close_download_iterator", exc_info=True)
    if hasattr(iterator, "aclose"):
        try:
            res = iterator.aclose()
            if inspect.isawaitable(res):
                await res
        except Exception:
            logger.debug("Ignored exception in _close_download_iterator", exc_info=True)


async def _download_sequential(
    client,
    media,
    offset: int,
    length: Optional[int],
    request_size: Optional[int] = None,
):
    """Sequential download via client.iter_download with MTProto boundary safety.

    Enforces Telegram official requirements (https://core.telegram.org/api/files#downloading-files):
      - Request size is a power-of-two divisor of 1MiB (4096..524288).
      - Upstream offset is aligned down to request_size boundary so requests
        never cross 1MiB boundaries and are always multiples of 4096.
      - Leading unaligned bytes are skipped and trailing bytes trimmed.
      - Chunks limit is ceil((prefix_skip + length) / request_size); length=None streams to EOF.
      - Cleanly closes the download iterator on success, error, or cancellation.
    """
    plan = _plan_download(offset, length, request_size)
    if plan is None:
        return
    aligned_offset, prefix_skip, req_size, chunk_limit, length = plan

    iterator = client.iter_download(
        media, offset=aligned_offset, limit=chunk_limit, request_size=req_size
    )

    remaining = length
    skip = prefix_skip
    try:
        # Telethon's DirectDownloadIter implements both async iteration and
        # async context management, but __aexit__ calls close() directly. If
        # _init() fails before setting _sender, that close raises
        # AttributeError and masks the original Telegram/network error. The
        # finally block below already performs best-effort cleanup, so iterate
        # directly and keep cleanup failures from replacing the real outcome.
        async for chunk in iterator:
            if not chunk:
                break
            data = bytes(chunk)
            if not data:
                continue
            out, skip, remaining, stop = _consume_chunk(data, skip, remaining)
            if out:
                yield out
            if stop:
                break
    finally:
        await _close_download_iterator(iterator)


def _plan_download(offset: int, length: Optional[int], request_size: Optional[int]):
    """Return (aligned_offset, prefix_skip, req_size, chunk_limit, length), or None for an empty range."""
    offset = max(0, int(offset or 0))
    if length is not None:
        length = int(length)
        if length <= 0:
            return None

    if request_size is not None:
        req_size = _legal_request_size(request_size)
    elif length is not None and length < 524288:
        req_size = _legal_request_size(length)
    else:
        req_size = 524288

    aligned_offset = (offset // req_size) * req_size
    prefix_skip = offset - aligned_offset

    if length is not None:
        total_needed = prefix_skip + length
        chunk_limit = max(1, (total_needed + req_size - 1) // req_size)
    else:
        chunk_limit = None
    return aligned_offset, prefix_skip, req_size, chunk_limit, length


def _consume_chunk(data: bytes, skip: int, remaining: Optional[int]) -> tuple[bytes, int, Optional[int], bool]:
    """Apply the leading skip and the length limit to one non-empty chunk.

    Returns (bytes to yield, skip left, remaining left, stop). ``stop`` means
    the requested range is complete and the download should end.
    """
    if skip > 0:
        if len(data) <= skip:
            return b"", skip - len(data), remaining, False
        data = data[skip:]
        skip = 0
    if remaining is not None:
        if remaining <= 0:
            return b"", skip, remaining, True
        if len(data) > remaining:
            data = data[:remaining]
        remaining -= len(data)
    stop = remaining is not None and remaining <= 0
    return data, skip, remaining, stop


async def _download_block(client, media, block_offset: int, block_length: int) -> bytes:
    """Fetch an aligned or custom block using _download_sequential and properly close iterator."""
    if block_length <= 0:
        return b""
    chunks = []
    async for chunk in _download_sequential(
        client, media, offset=block_offset, length=block_length
    ):
        chunks.append(chunk)
    return b"".join(chunks)


async def _close_global_stream_manager():
    global _global_stream_manager
    if _global_stream_manager is not None:
        await _global_stream_manager.close()
        _global_stream_manager = None
