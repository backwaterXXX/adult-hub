"""Bounded Telegram message and entity caches used by streaming requests."""

from __future__ import annotations

import time
import uuid
import weakref
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple

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


_CLIENT_STABLE_IDS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _client_session_key(client: Any, account_id: Optional[str] = None) -> str:
    """Derive a stable account/session-isolated cache prefix."""
    if account_id:
        return f"acc:{account_id}"
    client_acc = getattr(client, "_account_id", None) or getattr(client, "account_id", None)
    if client_acc:
        return f"acc:{client_acc}"
    session = getattr(client, "session", None)
    filename = getattr(session, "filename", None)
    if filename:
        return str(filename)
    stable_id = getattr(client, "_stable_id", None)
    if not stable_id:
        try:
            stable_id = _CLIENT_STABLE_IDS.get(client)
        except TypeError:
            stable_id = None
        if not stable_id:
            stable_id = f"client_{uuid.uuid4().hex[:12]}"
            try:
                _CLIENT_STABLE_IDS[client] = stable_id
            except TypeError:
                pass
            try:
                setattr(client, "_stable_id", stable_id)
            except Exception:
                pass
    return stable_id or "default_client"


def _evict_cached_message(channel_ref: str, message_id: int) -> None:
    """Drop cached messages whose file reference Telegram has expired."""
    global _MESSAGE_CACHE_BYTES
    suffix = f":{channel_ref}:{message_id}"
    exact = f"{channel_ref}:{message_id}"
    keys_to_pop = [
        k for k in _MESSAGE_CACHE
        if k == exact or k.endswith(suffix)
    ]
    for k in keys_to_pop:
        entry = _MESSAGE_CACHE.pop(k, None)
        if entry:
            _MESSAGE_CACHE_BYTES = max(0, _MESSAGE_CACHE_BYTES - entry[2])


async def _get_cached_message(
    client, channel_ref: str, message_id: int, account_id: Optional[str] = None
):
    global _MESSAGE_CACHE_BYTES
    client_key = _client_session_key(client, account_id=account_id)
    cache_key = f"{client_key}:{channel_ref}:{message_id}"
    now = time.time()
    cached = _MESSAGE_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _MESSAGE_CACHE_TTL:
        _MESSAGE_CACHE.move_to_end(cache_key)
        return cached[1]
    if cached:
        _MESSAGE_CACHE.pop(cache_key, None)
        _MESSAGE_CACHE_BYTES = max(0, _MESSAGE_CACHE_BYTES - cached[2])

    ent_key = f"{client_key}:{channel_ref}"
    cached_ent = _ENTITY_CACHE.get(ent_key)
    if cached_ent and (now - cached_ent[0]) < _ENTITY_CACHE_TTL:
        entity = cached_ent[1]
    else:
        entity = await client.get_entity(_entity_ref(channel_ref))
        if entity:
            _ENTITY_CACHE[ent_key] = (now, entity)
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
