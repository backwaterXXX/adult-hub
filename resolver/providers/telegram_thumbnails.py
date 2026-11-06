"""Telegram thumbnail cache and preview download operations."""
import asyncio
import time
from typing import Optional
import logging

logger = logging.getLogger(__name__)



def _telegram_module():
    # Resolve helper functions and tunables at call time to keep the provider
    # facade patchable and avoid importing it during module initialization.
    from . import telegram
    return telegram

async def download_thumb_impl(provider, ref: str) -> Optional[bytes]:
    tg = _telegram_module()
    """Cache preview bytes and bound the entire Telegram operation, including connect."""
    channel_ref, _, _ = tg._parse_ref(ref)
    if not tg.is_channel_authorized(channel_ref):
        return None
    cached = provider._thumb_cache.get(ref)
    if cached is not None:
        expires, data = cached
        if expires > time.monotonic():
            provider._thumb_cache.move_to_end(ref)
            return data
        provider._thumb_cache.pop(ref)
        provider._thumb_cache_bytes -= len(data or b"")
    task = provider._thumb_tasks.get(ref)
    if task is None:
        task = asyncio.create_task(provider._fetch_and_cache_thumb(ref))
        provider._thumb_tasks[ref] = task
        # Consume exceptions even when all callers disconnect before completion.
        def finished(done):
            if provider._thumb_tasks.get(ref) is done:
                provider._thumb_tasks.pop(ref, None)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
    return await asyncio.shield(task)


async def _fetch_and_cache_thumb_impl(provider, ref: str) -> Optional[bytes]:
    tg = _telegram_module()
    async def fetch():
        async with provider._thumb_slots:
            return await provider._download_thumb_uncached(ref)
    try:
        data = await asyncio.wait_for(fetch(), timeout=tg.THUMB_TIMEOUT_SECONDS)
        if data:
            try:
                from common.image_utils import is_black_image
                if await asyncio.to_thread(is_black_image, data):
                    data = None
            except Exception:
                logger.debug("Ignored exception checking black image in thumbnail provider", exc_info=True)
    except Exception:
        provider._cache_thumb(ref, None)
        raise
    provider._cache_thumb(ref, data)
    return data


def _cache_thumb_impl(provider, ref: str, data: Optional[bytes]):
    tg = _telegram_module()
    previous = provider._thumb_cache.pop(ref, None)
    if previous is not None:
        provider._thumb_cache_bytes -= len(previous[1] or b"")
    ttl = tg.THUMB_CACHE_TTL_SECONDS if data else tg.THUMB_FAILURE_TTL_SECONDS
    provider._thumb_cache[ref] = (time.monotonic() + ttl, data)
    provider._thumb_cache_bytes += len(data or b"")
    while (len(provider._thumb_cache) > tg.THUMB_CACHE_MAX_ENTRIES
           or provider._thumb_cache_bytes > tg.THUMB_CACHE_MAX_BYTES):
        _, (_, removed) = provider._thumb_cache.popitem(last=False)
        provider._thumb_cache_bytes -= len(removed or b"")


async def _download_thumb_uncached_impl(provider, ref: str) -> Optional[bytes]:
    tg = _telegram_module()
    """Message preview (photo/document thumb), max ~2 MB, otherwise None.

    Downloads ONLY the thumbnail, never the full video file.
    """
    try:
        channel_ref, message_id, unique_id = tg._parse_ref(ref)
    except Exception:
        logger.debug("Ignored exception in _download_thumb_uncached_impl", exc_info=True)
        return None
    if not tg.is_channel_authorized(channel_ref):
        tg.logger.warning("Telegram thumb rejected for unauthorized channel: %r", channel_ref)
        return None
    client = await tg._require_client()
    try:
        message = await tg._get_cached_message(client, channel_ref, int(message_id))
    except Exception:
        logger.debug("Ignored exception in _download_thumb_uncached_impl", exc_info=True)
        return None
    if message is None:
        return None
    try:
        candidates = _thumb_candidates(message, unique_id)
    except Exception:
        logger.debug("Ignored exception in _download_thumb_uncached_impl", exc_info=True)
        return None
    if candidates is None:
        return None
    return await _first_valid_thumb(client, candidates)


async def _first_valid_thumb(client, candidates) -> Optional[bytes]:
    for media_obj, size_obj in candidates:
        try:
            data = await client.download_media(
                media_obj, thumb=size_obj, file=bytes)
        except Exception:
            logger.debug("Ignored exception in _first_valid_thumb", exc_info=True)
            continue
        if not data:
            continue
        raw = bytes(data)
        if 0 < len(raw) <= 2_000_000:
            return raw
    return None


def _thumb_candidates(message, unique_id):
    """Return (media, size) pairs, largest first, or None if the message belongs to another file."""
    candidates = []
    doc = getattr(getattr(message, "media", None), "document", None)
    doc_id = getattr(doc, "id", None)
    if doc_id is not None and str(doc_id) != str(unique_id):
        return None
    thumbs = list(getattr(doc, "thumbs", None) or [])
    # Largest first: higher quality and sync won't reject (<1000 B).
    for size_obj in reversed(thumbs):
        candidates.append((doc, size_obj))
    photo = getattr(message, "photo", None)
    photo_id = getattr(photo, "id", None)
    if doc is None and photo_id is not None and str(photo_id) != str(unique_id):
        return None
    sizes = list(getattr(photo, "sizes", None) or [])
    if photo is not None:
        for size_obj in reversed(sizes):
            candidates.append((photo, size_obj))
    return candidates


