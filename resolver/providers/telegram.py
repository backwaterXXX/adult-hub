"""Telegram provider: private channels read with a user account via MTProto.

Phase 1: channel = studio/category. Without hashtags (phase 2, not implemented).

Configuration (environment variables; secrets NEVER appear in logs,
URLs or VideoItem):
  TELEGRAM_API_ID        - numeric API ID from https://my.telegram.org (required)
  TELEGRAM_API_HASH      - API hash (required, kept only in memory)
  TELEGRAM_SESSION_PATH  - session file path (default: /data/telegram.session)
  TELEGRAM_CHANNELS      - comma-separated channel identifiers,
                           e.g. "-1001234567890,someusername" (required)
  TELEGRAM_MAX_HISTORY   - number of history pages (50 messages each) to fetch
                           per browse/category call (default: 1)

Session login (session must be authenticated BEFORE using the provider):
  1. Set TELEGRAM_API_ID / TELEGRAM_API_HASH in the environment.
  2. Step 1 (send SMS verification code)::

       python3 -c "
       import asyncio
       from providers.telegram import login
       print(asyncio.run(login('+48123456789')))"

     Returns "code_sent:<phone>" after sending the code.
  3. Step 2 (confirm with SMS code, optionally 2FA password)::

       python3 -c "
       import asyncio
       from providers.telegram import login
       print(asyncio.run(login('+48123456789', code='12345')))"

     Returns "authorized" upon successful login. The session file is written
     to TELEGRAM_SESSION_PATH - mount it into docker as a volume to persist restarts.

Proxy contract (resolver/app.py):
  * resolve_stream(provider_video_id) returns marker ``tg://{ref}`` (NEVER
    any direct Telegram HTTP URL - playback goes exclusively through proxy).
  * ``async def download_range(self, ref, offset, length)`` - async generator
    yielding bytes from ``client.iter_download`` (chunk ~1MB; length=None
    means "until end of file").
  * ``async def file_size(self, ref) -> Optional[int]`` - file size
    from metadata or None when unknown.

Ref format (stable, built by extract_file_ref):
  ``{channel_id}:{message_id}:{file_unique_id}``
"""

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, List, Optional, Tuple

from .base import BaseProvider, CategoryItem, VideoItem
from .telegram_metadata import (
    _ALIAS_MAP_CACHE,  # noqa: F401 - compatibility export
    _KNOWN_PERFORMERS_CACHE,  # noqa: F401 - compatibility export
    load_known_performers, load_alias_map, load_performer_catalog,
    match_known_performers,  # noqa: F401 - compatibility export
)
from .telegram_config import (
    _entity_ref,
    _get_api_hash,
    _get_api_id,
    _get_channels,
    _get_config,
    _get_max_history,
    _get_min_duration,
    _get_session_path,
    is_channel_authorized,
    passes_duration_filter,
)
from .telegram_download import (
    LEGAL_REQUEST_SIZES,  # noqa: F401 - compatibility export
    _close_global_stream_manager,
    _download_block,
    _download_sequential,
    _get_global_stream_manager,
    _is_parallel_stream_enabled,
    _legal_request_size,  # noqa: F401 - compatibility export
)
from .telegram_thumbnails import (
    _cache_thumb_impl, download_thumb_impl as _download_thumb_impl,
    _download_thumb_uncached_impl,
    _fetch_and_cache_thumb_impl,
)

from .telegram_messages import (
    _caption_of,  # noqa: F401 - compatibility export
    _duration_of,  # noqa: F401 - compatibility export
    _filter_caption_for_title,  # noqa: F401 - compatibility export
    _filename_to_title,  # noqa: F401 - compatibility export
    _nested_unique_id as _nested_unique_id,
    _size_of,
    _text_hits as _text_hits,
    channel_to_category,
    extract_file_ref,  # noqa: F401 - compatibility export
    extract_hashtags,  # noqa: F401 - compatibility export
    message_object_to_dict as _message_to_dict,
    parse_caption_title,  # noqa: F401 - compatibility export
    thumb_api_path,  # noqa: F401 - compatibility export
)
from .telegram_item_mapping import (
    message_to_video_impl as _message_to_video_impl,
    prepare_legacy_catalog as _prepare_legacy_catalog_impl,
)
from .telegram_message_cache import (
    _evict_cached_message,
    _get_cached_message,
    _message_cache_size_bytes,  # noqa: F401 - compatibility export
)
from .telegram_retry import is_retryable, retry_call, retry_stream
from .telegram_history import (
    channel_title as _channel_title_impl,
    fetch_history as _fetch_history_impl,
    passes_tag_filter,
)
from .telegram_session import login as _login_impl, require_client as _require_client_impl
from .telegram_variants import (
    extract_message_variants,
    extract_variant_info,  # noqa: F401 - compatibility export
    find_variant_document,
    should_ignore_alt_documents,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "telegram"
PAGE_SIZE = 50
SEARCH_HISTORY_LIMIT = 200
DOWNLOAD_CHUNK_SIZE = 1024 * 1024  # ~1MB na chunk z iter_download
THUMB_TIMEOUT_SECONDS = 8.0
THUMB_CACHE_TTL_SECONDS = 3600.0
THUMB_FAILURE_TTL_SECONDS = 30.0
THUMB_CACHE_MAX_ENTRIES = 256
THUMB_CACHE_MAX_BYTES = 32 * 1024 * 1024

_client = None
_client_lock = None


# ---------------------------------------------------------------------------
# Configuration (clean env reads; secrets never appear in logs)
# ---------------------------------------------------------------------------



















def _prepare_legacy_catalog(known_performers, aliases):
    return _prepare_legacy_catalog_impl(known_performers, aliases)


def _prepare_external_performer_catalog():
    from common.performer_catalog import prepare_catalog

    catalog = load_performer_catalog()
    external = [row for row in catalog if str(row.get("id") or "").startswith(
        ("stashdb:", "theporndb:", "fansdb:")
    )]
    return prepare_catalog(external)


# Build one shared snapshot off the event loop. The lock coalesces concurrent
# misses, including an empty catalogue, and the TTL bounds staleness.
_PERFORMER_SNAPSHOT_TTL = 300.0
_performer_snapshot = None
_performer_snapshot_at = 0.0
_performer_snapshot_lock = threading.Lock()


def _load_performer_snapshot():
    global _performer_snapshot, _performer_snapshot_at
    with _performer_snapshot_lock:
        now = time.monotonic()
        if _performer_snapshot is not None and now - _performer_snapshot_at < _PERFORMER_SNAPSHOT_TTL:
            return _performer_snapshot
        snapshot = (load_known_performers(), load_alias_map(), _prepare_external_performer_catalog())
        _performer_snapshot = snapshot
        _performer_snapshot_at = time.monotonic()
        return snapshot


def message_to_video(msg: dict, channel_title: str,
                     known_performers=None, aliases=None,
                     prepared_known_map=None, prepared_catalog=None) -> Optional[VideoItem]:
    """Mapuje message_dict z materialem video na VideoItem (None gdy brak video).

    known_performers/aliases: legacy compatibility inputs; automatic cast
    assignment requires an exact identity from the external performer catalogue.
    """
    return _message_to_video_impl(
        msg,
        channel_title,
        known_performers,
        aliases,
        prepared_known_map,
        prepared_catalog,
        load_known_performers_fn=load_known_performers,
        load_alias_map_fn=load_alias_map,
        load_performer_catalog_fn=load_performer_catalog,
        prepare_legacy_catalog_fn=_prepare_legacy_catalog,
    )


# ---------------------------------------------------------------------------
# Warstwa MTProto (Telethon ladowany leniwie WEWNATRZ funkcji)
# ---------------------------------------------------------------------------

def _parse_ref(ref: str) -> Tuple[str, int, str]:
    parts = (ref or "").rsplit(":", 2)
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise ValueError(f"Invalid telegram ref: {ref!r}")
    try:
        message_id = int(parts[1])
    except (TypeError, ValueError):
        raise ValueError(f"Invalid telegram ref: {ref!r}") from None
    return parts[0], message_id, parts[2]


async def _require_client():
    """Singleton klienta Telethon (connect; sesja musi byc juz zalogowana)."""
    global _client, _client_lock
    state = {"client": _client, "lock": _client_lock}
    try:
        return await _require_client_impl(
            state,
            api_id_fn=_get_api_id,
            api_hash_fn=_get_api_hash,
            session_path_fn=_get_session_path,
        )
    finally:
        _client, _client_lock = state["client"], state["lock"]


async def _channel_title(client, channel_ref, entity=None) -> str:
    return await _channel_title_impl(
        client,
        channel_ref,
        entity,
        config_fn=_get_config,
        entity_ref_fn=_entity_ref,
    )


async def _fetch_history(client, entity, page: int = 1, max_pages: Optional[int] = None,
                         offset_id: Optional[int] = None, limit: Optional[int] = None) -> list:
    """Fetch channel history with the existing pagination and media filter."""
    return await _fetch_history_impl(
        client,
        entity,
        page=page,
        max_pages=max_pages,
        offset_id=offset_id,
        limit=limit,
        max_history_fn=_get_max_history,
    )


async def login(phone: str, code: Optional[str] = None,
                password: Optional[str] = None) -> str:
    """Logowanie sesji userbota (dwuetapowe, kod z SMS).

    Krok 1: ``await login("+48123456789")`` -> wysyla kod, zwraca
    ``"code_sent:<phone>"``. Krok 2: ``await login("+48123456789",
    code="12345")`` (+ opcjonalne ``password`` przy 2FA) -> zwraca
    ``"authorized"``. Gdy sesja juz zalogowana, zwraca ``"already_authorized"``.
    """
    return await _login_impl(
        phone,
        code,
        password,
        api_id_fn=_get_api_id,
        api_hash_fn=_get_api_hash,
        session_path_fn=_get_session_path,
    )
# ---------------------------------------------------------------------------
# Parallel Telegram streaming configuration and helpers
# ---------------------------------------------------------------------------

def _variant_size(message, channel_ref: str, unique_id: str) -> Optional[int]:
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


class TelegramProvider(BaseProvider):
    """Provider MTProto: prywatne kanaly jako kategorie (etap 1, bez hashtagow)."""

    name = "telegram"

    def __init__(self):
        self._thumb_cache = OrderedDict()
        self._thumb_cache_bytes = 0
        self._thumb_tasks = {}
        self._thumb_slots = asyncio.Semaphore(4)

    def is_channel_authorized(self, channel_ref: Any) -> bool:
        return is_channel_authorized(channel_ref)

    async def check_configured_channel_access(self, channel_ref: Any) -> None:
        """Check read access with the existing session using one message metadata item.

        This deliberately performs no history iteration or media fetch. The admin
        route validates the ID against config first, including disabled channels.
        """
        client = await _require_client()
        entity = await client.get_entity(_entity_ref(channel_ref))
        await client.get_messages(entity, limit=1)

    async def check_message_metadata(self, channel_ref: str, message_id: int,
                                    unique_id: str) -> str:
        """Check one exact message using metadata only; never fetch media bytes."""
        if not is_channel_authorized(channel_ref):
            raise ValueError(f"Channel not authorized: {channel_ref!r}")
        client = await _require_client()
        message = await _get_cached_message(client, channel_ref, int(message_id))
        if message is None:
            return "missing"
        media = getattr(message, "media", None) or getattr(message, "document", None)
        if media is None:
            return "mismatch"
        target_doc = find_variant_document(media, str(unique_id))
        return "available" if target_doc is not None else "mismatch"

    async def get_categories(self) -> List[CategoryItem]:
        channels = _get_channels()
        if not channels:
            return []
        client = await _require_client()
        categories: List[CategoryItem] = []
        for channel_ref in channels:
            title = await _channel_title(client, channel_ref)
            categories.append(channel_to_category(channel_ref, title))
        return categories

    async def get_browse_videos(self, section: str = "latest",
                                page: int = 1) -> List[VideoItem]:
        channels = _get_channels()
        if not channels:
            raise RuntimeError(
                "Telegram provider is not configured: set TELEGRAM_CHANNELS "
                "env var (comma-separated channel identifiers)."
            )
        return await self.get_category_videos(channels[0], page=page)

    async def get_category_videos(self, category_id: str, page: int = 1,
                                  sort: str = "latest",
                                  min_duration: Optional[int] = None,
                                  offset_id: Optional[int] = None,
                                  limit: Optional[int] = None) -> List[VideoItem]:
        if not is_channel_authorized(category_id):
            raise ValueError(f"Channel not authorized: {category_id!r}")
        client = await _require_client()
        entity = await client.get_entity(_entity_ref(category_id))
        title = await _channel_title(client, category_id, entity)
        cfg = _get_config()
        ch_cfg = cfg.get_telegram_channel(category_id) if cfg else None

        effective_min_dur = min_duration
        if effective_min_dur is None and ch_cfg:
            effective_min_dur = ch_cfg.min_duration_seconds
        if effective_min_dur is None:
            effective_min_dur = _get_min_duration()

        messages = await _fetch_history(
            client, entity, page=page, max_pages=1,
            offset_id=offset_id, limit=limit
        )
        videos: List[VideoItem] = []
        kp, am, prepared_catalog = await asyncio.to_thread(_load_performer_snapshot)
        for msg in messages:
            item = message_to_video(
                _message_to_dict(msg, category_id), title,
                known_performers=kp, aliases=am,
                prepared_catalog=prepared_catalog,
            )
            if (item is not None and passes_duration_filter(item.duration, min_duration=effective_min_dur)
                    and passes_tag_filter(item, ch_cfg)):
                videos.append(item)
        return videos

    async def get_history_batch(self, category_id: str, *, offset_id: Optional[int] = None,
                                limit: int = 100) -> dict:
        """Return an unfiltered message cursor alongside videos passing channel filters."""
        if not is_channel_authorized(category_id):
            raise ValueError(f"Channel not authorized: {category_id!r}")
        client = await _require_client()
        entity = await client.get_entity(_entity_ref(category_id))
        title = await _channel_title(client, category_id, entity)
        cfg = _get_config()
        ch_cfg = cfg.get_telegram_channel(category_id) if cfg else None
        messages = await _fetch_history(client, entity, offset_id=offset_id, limit=limit)
        videos = []
        kp, am, prepared_catalog = await asyncio.to_thread(_load_performer_snapshot)
        for msg in messages:
            item = message_to_video(
                _message_to_dict(msg, category_id), title,
                known_performers=kp, aliases=am,
                prepared_catalog=prepared_catalog,
            )
            if (item and passes_duration_filter(item.duration,
                    min_duration=ch_cfg.min_duration_seconds if ch_cfg else _get_min_duration())
                    and passes_tag_filter(item, ch_cfg)):
                videos.append(item)
        videos = [item.__dict__ for item in videos]
        cursor = min((int(msg.id) for msg in messages), default=None)
        head = max((int(msg.id) for msg in messages), default=None)
        return {"videos": videos, "next_offset_id": cursor, "highest_id": head,
                "message_count": len(messages), "has_more": len(messages) >= limit}

    async def search(self, query: str, page: int = 1) -> List[VideoItem]:
        needle = (query or "").strip().lower()
        if not needle:
            return []
        channels = _get_channels()
        if not channels:
            raise RuntimeError(
                "Telegram provider is not configured: set TELEGRAM_CHANNELS "
                "env var (comma-separated channel identifiers)."
            )
        client = await _require_client()
        matched: List[VideoItem] = []
        known_performers, aliases, prepared_catalog = await asyncio.to_thread(_load_performer_snapshot)
        for channel_ref in channels:
            try:
                entity = await client.get_entity(_entity_ref(channel_ref))
            except Exception:
                logger.warning("Telegram channel unreachable, skipping")
                continue
            title = await _channel_title(client, channel_ref, entity)
            try:
                messages = await client.get_messages(
                    entity, limit=SEARCH_HISTORY_LIMIT)
            except Exception:
                logger.warning("Telegram history fetch failed, skipping")
                continue
            for msg in messages or []:
                data = _message_to_dict(msg, channel_ref)
                if needle not in (data.get("caption") or "").lower():
                    continue
                item = message_to_video(
                    data, title, known_performers=known_performers,
                    aliases=aliases, prepared_catalog=prepared_catalog
                )
                if item is not None and passes_duration_filter(item.duration):
                    matched.append(item)
        page = max(1, int(page or 1))
        start = (page - 1) * PAGE_SIZE
        return matched[start:start + PAGE_SIZE]

    async def get_video_variants(self, ref: str) -> dict:
        """Return all available video variants/qualities for a Telegram video reference."""
        channel_ref, message_id, unique_id = _parse_ref(ref)
        if not is_channel_authorized(channel_ref):
            raise ValueError(f"Channel not authorized: {channel_ref!r}")
        client = await _require_client()
        message = await _get_cached_message(client, channel_ref, int(message_id))
        if message is None:
            raise ValueError(f"Telegram message not found for ref {ref!r}")
        media = getattr(message, "media", None) or getattr(message, "document", None)
        if media is None:
            raise ValueError(f"Telegram message has no media for ref {ref!r}")
        doc = getattr(media, "document", None) or (media if hasattr(media, "id") else None)
        if doc is None:
            raise ValueError(f"Telegram message has no video document for ref {ref!r}")
        target_doc = find_variant_document(media, str(unique_id))
        if target_doc is None:
            raise ValueError(f"Telegram media id mismatch: expected {unique_id!r}")
        ignore_alts = await should_ignore_alt_documents(client)
        variants_data = extract_message_variants(message, ignore_alt_documents=ignore_alts)
        variants_data["video_id"] = f"telegram:{ref}"
        return variants_data

    async def resolve_stream(self, provider_video_id: str, variant: Optional[str] = None) -> str:
        """Zwraca marker tg:// - odtwarzanie idzie przez proxy resolvera.

        NIGDY nie zwraca http URL Telegrama.
        Gdy podano variant, weryfikuje istnienie wariantu i zwraca ref wskazujacy
        na wlasciwy dokument (oryginal lub alt_document).
        """
        if not provider_video_id:
            raise ValueError("Empty telegram video id")
        channel_ref, message_id, unique_id = _parse_ref(provider_video_id)
        if not is_channel_authorized(channel_ref):
            raise ValueError(f"Channel not authorized: {channel_ref!r}")

        chosen = (variant or "").strip()
        if not chosen or chosen.lower() == "original":
            return f"tg://{provider_video_id}"

        # Resolve selected variant
        client = await _require_client()
        message = await _get_cached_message(client, channel_ref, int(message_id))
        if message is None:
            raise ValueError(f"Telegram message not found for ref {provider_video_id!r}")
        media = getattr(message, "media", None) or getattr(message, "document", None)
        if media is None:
            raise ValueError(f"Telegram message has no media for ref {provider_video_id!r}")

        ignore_alts = await should_ignore_alt_documents(client)
        if ignore_alts:
            raise ValueError(f"Variant '{chosen}' not found (alt_documents disabled)")

        target_doc = find_variant_document(media, chosen)
        if target_doc is None:
            raise ValueError(f"Variant '{chosen}' not found")
        target_id = getattr(target_doc, "id", None)
        if target_id is None:
            raise ValueError(f"Variant '{chosen}' has no document id")

        return f"tg://{channel_ref}:{message_id}:{target_id}"

    async def download_range(self, ref: str, offset: int, length: Optional[int]):
        """Proxy byte stream with bounded retries that never start before delivery.

        The whole range is retried only while zero bytes have been yielded; once
        the client has received bytes, failures propagate so HTTP stays truthful.
        """
        def _on_retry(exc: BaseException, _attempt: int) -> None:
            if type(exc).__name__ in {"FileReferenceExpiredError", "FileReferenceInvalidError"}:
                channel_ref, message_id, _uid = _parse_ref(ref)
                _evict_cached_message(channel_ref, int(message_id))

        async for chunk in retry_stream(
                lambda: self._download_range_once(ref, offset, length),
                on_retry=_on_retry):
            yield chunk

    async def _download_range_once(self, ref: str, offset: int, length: Optional[int]):
        """Async generator bajtow pliku dla proxy.

        Validates ref/channel/document id BEFORE cache.
        Obtains total from document metadata.
        Falls back to sequential for unknown size or small probes (<=128KiB).
        Uses bounded parallel streaming when TELEGRAM_PARALLEL_STREAM_ENABLED is true.
        """
        channel_ref, message_id, unique_id = _parse_ref(ref)
        if not is_channel_authorized(channel_ref):
            raise ValueError(f"Channel not authorized: {channel_ref!r}")
        client = await _require_client()
        message = await _get_cached_message(client, channel_ref, int(message_id))
        if message is None:
            raise ValueError(f"Telegram message not found for ref {ref!r}")
        media = getattr(message, "media", None) or getattr(message, "document", None)
        if media is None:
            raise ValueError(f"Telegram message has no media for ref {ref!r}")
        target_doc = find_variant_document(media, str(unique_id))
        if target_doc is None:
            raise ValueError(f"Telegram media id mismatch: expected {unique_id!r}, not found in original or alt_documents")

        # Obtain total from document metadata
        total = getattr(target_doc, "size", None)
        if total is None:
            msg_dict = _message_to_dict(message, channel_ref)
            total = _size_of(msg_dict)

        doc_orig = getattr(media, "document", None) or (media if hasattr(media, "id") else None)
        download_target = media if (target_doc == doc_orig and media is not None) else target_doc

        # Small probes <= 128KiB stay sequential (no large prefetch)
        is_small_probe = (length is not None and length <= 131072)
        use_parallel = (
            _is_parallel_stream_enabled()
            and total is not None
            and total > 0
            and not is_small_probe
        )

        if not use_parallel:
            async for chunk in _download_sequential(client, download_target, offset=offset or 0, length=length):
                yield chunk
            return

        manager = _get_global_stream_manager()

        async def _fetch(blk_offset: int, blk_length: int) -> bytes:
            # A block is fully downloaded before delivery, so retrying it is safe.
            # File-reference errors need a fresh message and cannot be fixed here.
            return await retry_call(
                lambda: _download_block(client, download_target, blk_offset, blk_length),
                retryable=lambda exc: is_retryable(exc) and type(exc).__name__ not in {
                    "FileReferenceExpiredError", "FileReferenceInvalidError"},
            )

        cache_key = f"{ref}:{int(total)}"
        async for chunk in manager.stream(
            key=cache_key,
            offset=offset or 0,
            length=length,
            total=int(total),
            fetch=_fetch,
        ):
            yield chunk

    async def close(self):
        """Close provider resources including shared parallel stream manager."""
        await _close_global_stream_manager()

    async def file_size(self, ref: str) -> Optional[int]:
        """Rozmiar pliku z metadanych wiadomosci albo None, gdy nieznany."""
        try:
            channel_ref, message_id, unique_id = _parse_ref(ref)
        except Exception:
            logger.debug("Ignored exception in file_size", exc_info=True)
            return None
        if not is_channel_authorized(channel_ref):
            return None
        client = await _require_client()
        try:
            message = await retry_call(
                lambda: _get_cached_message(client, channel_ref, int(message_id)),
                retryable=lambda exc: is_retryable(exc)
                or "database is locked" in str(exc).casefold(),
            )
        except Exception:
            logger.debug("Ignored exception in file_size", exc_info=True)
            return None
        if message is None:
            return None
        return _variant_size(message, channel_ref, str(unique_id))

    async def download_thumb(self, ref: str) -> Optional[bytes]:
        return await _download_thumb_impl(self, ref)

    async def _fetch_and_cache_thumb(self, ref: str) -> Optional[bytes]:
        return await _fetch_and_cache_thumb_impl(self, ref)

    def _cache_thumb(self, ref: str, data: Optional[bytes]):
        return _cache_thumb_impl(self, ref, data)

    async def _download_thumb_uncached(self, ref: str) -> Optional[bytes]:
        return await _download_thumb_uncached_impl(self, ref)
