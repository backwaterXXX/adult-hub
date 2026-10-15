"""Map Telegram message dictionaries into provider video items."""

from __future__ import annotations

from typing import Optional

from .base import VideoItem, normalize_performers
from .telegram_messages import (
    _caption_of,
    _duration_of,
    _filter_caption_for_title,
    _filename_to_title,
    extract_file_ref,
    extract_hashtags,
    parse_caption_title,
    thumb_api_path,
)


def prepare_known_performer_map(known_performers, aliases):
    from common.performer_titles import normalize_known_map

    known_map = {str(name).strip().lower(): str(name).strip()
                 for name in (known_performers or []) if str(name).strip()}
    known_map.update({
        str(alias).strip().lower(): str(canonical).strip()
        for alias, canonical in (aliases or {}).items()
        if str(alias).strip() and str(canonical).strip()
    })
    return normalize_known_map(known_map)


def prepare_legacy_catalog(known_performers, aliases):
    """Build and index the fallback performer catalogue once per request."""
    from common.performer_catalog import prepare_catalog

    catalog = []
    by_name = {}
    for name in known_performers or []:
        display = str(name).strip()
        if not display:
            continue
        row = {"id": "local:" + display.casefold(), "name": display,
               "aliases": [], "trusted_aliases": [], "_persisted": False}
        catalog.append(row)
        by_name.setdefault(display.casefold(), row)
    for alias, canonical in (aliases or {}).items():
        display = str(canonical).strip()
        key = display.casefold()
        row = by_name.get(key)
        if row is None:
            row = {"id": "local:" + key, "name": display,
                   "aliases": [], "trusted_aliases": [], "_persisted": False}
            catalog.append(row)
            by_name[key] = row
        row["aliases"].append(str(alias))
        row["trusted_aliases"].append(str(alias))
    return catalog, prepare_catalog(catalog)


def message_to_video_impl(
    msg: dict,
    channel_title: str,
    known_performers=None,
    aliases=None,
    prepared_known_map=None,
    prepared_catalog=None,
    *,
    load_known_performers_fn,
    load_alias_map_fn,
    load_performer_catalog_fn,
    prepare_legacy_catalog_fn,
) -> Optional[VideoItem]:
    """Map a video message dictionary while keeping database loaders injectable."""
    ref = extract_file_ref(msg)
    if ref is None:
        return None
    _channel_id, message_id, unique_id = ref.rsplit(":", 2)
    # t.me/c/ requires the bare channel ID without the -100 prefix.
    channel_link = _channel_id[4:] if _channel_id.startswith("-100") else _channel_id.lstrip("-")
    url = f"https://t.me/c/{channel_link}/{message_id}"
    caption = _caption_of(msg)
    hashtags = extract_hashtags(caption)
    categories = ([channel_title] + ["#" + hashtag for hashtag in hashtags])[:16]
    filtered_caption = _filter_caption_for_title(caption)
    filename = msg.get("filename") or ""
    filename_title = _filename_to_title(filename)
    fallback = filename_title or f"{channel_title} #{message_id}"
    title = parse_caption_title(filtered_caption, fallback)
    load_from_db = known_performers is None
    if load_from_db:
        try:
            known_performers = load_known_performers_fn()
        except Exception:
            known_performers = []
    if aliases is None:
        if load_from_db:
            try:
                aliases = load_alias_map_fn()
            except Exception:
                aliases = {}
        else:
            aliases = {}
    proposals = []
    try:
        from common.performer_catalog import match_catalog, prepare_catalog

        if load_from_db:
            catalog = load_performer_catalog_fn()
            if not catalog:
                catalog, prepared_catalog = prepare_legacy_catalog_fn(known_performers, aliases)
            elif prepared_catalog is None:
                prepared_catalog = prepare_catalog(catalog)
        elif prepared_catalog is None:
            catalog, prepared_catalog = prepare_legacy_catalog_fn(known_performers, aliases)
        else:
            catalog = ()
        records, proposals = match_catalog(
            [("caption", caption), ("filename", filename_title), ("title", title)],
            catalog,
            prepared=prepared_catalog,
        )
        performers = normalize_performers(
            [record["name"] for record in records], alias_map=aliases or {}
        )
    except Exception:
        performers, proposals = [], []
    return VideoItem(
        id=f"telegram:{unique_id}",
        provider="telegram",
        provider_video_id=ref,
        title=title,
        slug=unique_id,
        url=url,
        thumbnail=thumb_api_path(ref),
        duration=_duration_of(msg),
        performers=performers,
        categories=categories,
        description=caption or None,
        source_text=caption or "",
        source_filename=filename,
        performer_proposals=proposals,
    )
