"""Telegram channel history retrieval and channel-level filters."""

from __future__ import annotations

from typing import Optional
import logging
import asyncio

from common.config import get_telegram_history_timeout
from resolver.providers.base import VideoItem

logger = logging.getLogger(__name__)


PAGE_SIZE = 50


async def channel_title(client, channel_ref, entity=None, *, config_fn, entity_ref_fn) -> str:
    config = config_fn()
    if config:
        channel_config = config.get_telegram_channel(channel_ref)
        if channel_config and channel_config.studio:
            return channel_config.studio
    try:
        resolved = entity if entity is not None else await client.get_entity(entity_ref_fn(channel_ref))
        name = getattr(resolved, "title", None) or getattr(resolved, "username", None)
        if name:
            return str(name)
    except Exception:
        logger.debug("Ignored exception in channel_title", exc_info=True)
    return str(channel_ref)


async def fetch_history(
    client,
    entity,
    page: int = 1,
    max_pages: Optional[int] = None,
    offset_id: Optional[int] = None,
    limit: Optional[int] = None,
    *,
    max_history_fn,
) -> list:
    """Fetch pages of 50 media messages or one cursor-based history batch."""
    try:
        from telethon.tl.types import InputMessagesFilterPhotoVideo
        media_filter = InputMessagesFilterPhotoVideo()
    except ImportError:
        media_filter = None

    if offset_id is not None or limit is not None:
        batch_limit = limit or PAGE_SIZE
        kwargs = {"limit": batch_limit, "filter": media_filter}
        if offset_id is not None:
            kwargs["offset_id"] = offset_id
        try:
            messages = await asyncio.wait_for(
                client.get_messages(entity, **kwargs),
                timeout=get_telegram_history_timeout(),
            )
            return list(messages or [])
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("Telegram fetch_history (cursor) timeout for entity %s", entity.id if hasattr(entity, 'id') else entity)
            return []

    page = max(1, int(page or 1))
    pages = max_pages if max_pages is not None else max_history_fn()
    start = (page - 1) * PAGE_SIZE * pages
    messages: list = []
    for index in range(pages):
        try:
            batch = await asyncio.wait_for(
                client.get_messages(
                    entity,
                    limit=PAGE_SIZE,
                    add_offset=start + index * PAGE_SIZE,
                    filter=media_filter,
                ),
                timeout=get_telegram_history_timeout(),
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("Telegram fetch_history (page %s) timeout for entity %s", index + 1, entity.id if hasattr(entity, 'id') else entity)
            break
        if not batch:
            break
        messages.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    return messages


def passes_tag_filter(item: VideoItem, channel_config) -> bool:
    if channel_config is None:
        return True
    tags = {value[1:].casefold() for value in item.categories
            if isinstance(value, str) and value.startswith("#")}
    whitelist = {str(value).strip().lstrip("#").casefold()
                 for value in channel_config.tags_whitelist}
    blacklist = {str(value).strip().lstrip("#").casefold()
                 for value in channel_config.tags_blacklist}
    return not bool(tags & blacklist) and (not whitelist or bool(tags & whitelist))
