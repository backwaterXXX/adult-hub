"""Bounded Telegram message and entity caches used by streaming requests."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Dict, Tuple

from .telegram_config import _entity_ref


_MESSAGE_CACHE: "OrderedDict[str, Tuple[float, Any, int]]" = OrderedDict()
_MESSAGE_CACHE_TTL = 3600.0
_MESSAGE_CACHE_MAX_ENTRIES = 5000
_MESSAGE_CACHE_MAX_BYTES = 16 * 1024 * 1024
_MESSAGE_CACHE_BYTES = 0
_ENTITY_CACHE: Dict[str, Tuple[float, Any]] = {}
_ENTITY_CACHE_TTL = 7200.0


def _message_cache_size_bytes(message: Any) -> int:
    """Estimate the retained message metadata size for the bounded cache."""
    caption = getattr(message, "message", None) or getattr(message, "text", None) or ""
    size = 2048 + (len(caption.encode("utf-8", errors="replace")) if isinstance(caption, str) else 0)
    media = getattr(message, "media", None)
    document = getattr(media, "document", None) if media is not None else None
    if document is None:
        candidate = getattr(message, "document", None)
        if candidate is not None and not callable(candidate):
            document = candidate
    if document is None:
        return size

    size += 4096
    mime_type = getattr(document, "mime_type", None)
    if isinstance(mime_type, str):
        size += len(mime_type.encode("utf-8", errors="replace"))
    attributes = getattr(document, "attributes", None) or ()
    try:
        for attribute in attributes:
            size += 512
            for name in ("file_name", "title", "performer"):
                value = getattr(attribute, name, None)
                if isinstance(value, str):
                    size += len(value.encode("utf-8", errors="replace"))
    except TypeError:
        pass
    return size


async def _get_cached_message(client, channel_ref: str, message_id: int):
    global _MESSAGE_CACHE_BYTES
    cache_key = f"{channel_ref}:{message_id}"
    now = time.time()
    cached = _MESSAGE_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _MESSAGE_CACHE_TTL:
        _MESSAGE_CACHE.move_to_end(cache_key)
        return cached[1]
    if cached:
        _MESSAGE_CACHE.pop(cache_key, None)
        _MESSAGE_CACHE_BYTES = max(0, _MESSAGE_CACHE_BYTES - cached[2])

    cached_ent = _ENTITY_CACHE.get(channel_ref)
    if cached_ent and (now - cached_ent[0]) < _ENTITY_CACHE_TTL:
        entity = cached_ent[1]
    else:
        entity = await client.get_entity(_entity_ref(channel_ref))
        if entity:
            _ENTITY_CACHE[channel_ref] = (now, entity)
            if len(_ENTITY_CACHE) > 500:
                _ENTITY_CACHE.pop(next(iter(_ENTITY_CACHE)), None)

    message = await client.get_messages(entity, ids=message_id)
    if message:
        message_size = _message_cache_size_bytes(message)
        if message_size <= _MESSAGE_CACHE_MAX_BYTES:
            previous = _MESSAGE_CACHE.pop(cache_key, None)
            if previous:
                _MESSAGE_CACHE_BYTES = max(0, _MESSAGE_CACHE_BYTES - previous[2])
            _MESSAGE_CACHE[cache_key] = (now, message, message_size)
            _MESSAGE_CACHE_BYTES += message_size
            while (
                len(_MESSAGE_CACHE) > _MESSAGE_CACHE_MAX_ENTRIES
                or _MESSAGE_CACHE_BYTES > _MESSAGE_CACHE_MAX_BYTES
            ):
                _old_key, _old_entry = _MESSAGE_CACHE.popitem(last=False)
                _MESSAGE_CACHE_BYTES = max(0, _MESSAGE_CACHE_BYTES - _old_entry[2])
    return message
