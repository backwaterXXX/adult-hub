"""In-memory and database caching for Telegram file sizes.

Speeds up seek and range queries from seconds down to sub-millisecond lookups,
avoiding redundant Telegram MTProto metadata round-trips and timeouts during playback.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
from typing import Any, Awaitable, Callable, Optional, Tuple

from common import db
from resolver.providers.telegram_messages import (
    _size_of,
    message_object_to_dict as _message_to_dict,
)
from resolver.providers.telegram_retry import is_retryable, retry_call
from resolver.providers.telegram_variants import find_variant_document

logger = logging.getLogger(__name__)

# Bounded LRU cache of ref -> file size in bytes
_MAX_CACHE_ENTRIES = 5000
_FILE_SIZE_CACHE: OrderedDict[str, int] = OrderedDict()


def _parse_ref(ref: str) -> Tuple[str, int, str]:
    parts = (ref or "").rsplit(":", 2)
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise ValueError(f"Invalid telegram ref: {ref!r}")
    try:
        message_id = int(parts[1])
    except (TypeError, ValueError):
        raise ValueError(f"Invalid telegram ref: {ref!r}") from None
    return parts[0], message_id, parts[2]


def _variant_size(message: Any, channel_ref: str, unique_id: str) -> Optional[int]:
    """Return the size of the media variant whose unique id matches, or None when unknown."""
    media = getattr(message, "media", None) or getattr(message, "document", None)
    if media is None:
        return None
    target_doc = find_variant_document(media, unique_id)
    if target_doc is None:
        return None
    size = getattr(target_doc, "size", None)
    if size is not None:
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = None
        if size is not None and size >= 0:
            return size
    doc_orig = getattr(media, "document", None) or (media if hasattr(media, "id") else None)
    if target_doc == doc_orig:
        return _size_of(_message_to_dict(message, channel_ref))
    return None


def get_cached_file_size(ref: str) -> Optional[int]:
    """Return in-memory cached file size if present, moving entry to MRU position."""
    clean_ref = str(ref or "").strip()
    if not clean_ref:
        return None
    size = _FILE_SIZE_CACHE.get(clean_ref)
    if size is not None:
        _FILE_SIZE_CACHE.move_to_end(clean_ref)
        return size
    unique_id = clean_ref.rsplit(":", 1)[-1].strip()
    if unique_id and unique_id != clean_ref:
        size = _FILE_SIZE_CACHE.get(unique_id)
        if size is not None:
            _FILE_SIZE_CACHE.move_to_end(unique_id)
            _FILE_SIZE_CACHE[clean_ref] = size
            return size
    return None


def set_cached_file_size(ref: str, size: Optional[int]) -> None:
    """Store file size in the in-memory cache, evicting oldest if over capacity."""
    clean_ref = str(ref or "").strip()
    if not clean_ref or not isinstance(size, int) or size < 0:
        return
    _FILE_SIZE_CACHE[clean_ref] = size
    _FILE_SIZE_CACHE.move_to_end(clean_ref)
    unique_id = clean_ref.rsplit(":", 1)[-1].strip()
    if unique_id and unique_id != clean_ref:
        _FILE_SIZE_CACHE[unique_id] = size
        _FILE_SIZE_CACHE.move_to_end(unique_id)
    while len(_FILE_SIZE_CACHE) > _MAX_CACHE_ENTRIES:
        _FILE_SIZE_CACHE.popitem(last=False)


def clear_file_size_cache() -> None:
    """Clear in-memory cache (for test isolation)."""
    _FILE_SIZE_CACHE.clear()


def get_db_file_size(ref: str, db_url: Optional[str] = None) -> Optional[int]:
    """Look up file_size_bytes from PostgreSQL videos table."""
    clean_ref = str(ref or "").strip()
    if not clean_ref:
        return None
    unique_id = clean_ref.rsplit(":", 1)[-1].strip()
    candidate_ids = [
        clean_ref if clean_ref.startswith("telegram:") else f"telegram:{clean_ref}",
    ]
    if unique_id and f"telegram:{unique_id}" not in candidate_ids:
        candidate_ids.append(f"telegram:{unique_id}")

    try:
        with db.connection(db_url, connect_timeout=2) as conn:
            for candidate_id in candidate_ids:
                row = conn.execute(
                    "SELECT file_size_bytes FROM videos WHERE id = %s AND file_size_bytes IS NOT NULL AND file_size_bytes > 0",
                    (candidate_id,),
                ).fetchone()
                if row and row[0] is not None:
                    size = int(row[0])
                    set_cached_file_size(clean_ref, size)
                    return size
    except Exception as exc:
        logger.debug("Database file_size lookup failed for %s: %s", ref, exc)
    return None


async def resolve_file_size(
    ref: str,
    *,
    fetch_upstream_fn: Callable[[str], Awaitable[Optional[int]]],
    db_url: Optional[str] = None,
) -> Optional[int]:
    """Resolve file size from in-memory cache, database, or upstream MTProto."""
    clean_ref = str(ref or "").strip()
    if not clean_ref:
        return None

    # 1. Fast in-memory hit
    cached = get_cached_file_size(clean_ref)
    if cached is not None:
        return cached

    # 2. Local database hit
    db_size = await asyncio.to_thread(get_db_file_size, clean_ref, db_url)
    if db_size is not None:
        return db_size

    # 3. Upstream fetch
    size = await fetch_upstream_fn(clean_ref)
    if size is not None and isinstance(size, int) and size >= 0:
        set_cached_file_size(clean_ref, size)
        return size

    return size


async def file_size_impl(
    provider: Any,
    ref: str,
    *,
    acquire_lease_fn: Any,
    is_channel_authorized_fn: Any,
    get_cached_message_fn: Any,
) -> Optional[int]:
    """Implementation of TelegramProvider.file_size with caching and DB fallbacks."""
    async def _fetch_upstream(_ref: str) -> Optional[int]:
        try:
            channel_ref, message_id, unique_id = _parse_ref(_ref)
        except Exception:
            logger.debug("Ignored exception in file_size", exc_info=True)
            return None
        if not is_channel_authorized_fn(channel_ref):
            return None
        try:
            async with acquire_lease_fn(channel_ref) as (_account_id, client):
                message = await retry_call(
                    lambda: get_cached_message_fn(client, channel_ref, int(message_id), account_id=_account_id),
                    retryable=lambda exc: is_retryable(exc)
                    or "database is locked" in str(exc).casefold(),
                )
                if message is None:
                    return None
                return _variant_size(message, channel_ref, str(unique_id))
        except Exception:
            logger.debug("Ignored exception in file_size", exc_info=True)
            return None

    return await resolve_file_size(ref, fetch_upstream_fn=_fetch_upstream)
