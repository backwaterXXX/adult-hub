"""Map Telegram message dictionaries into provider video items."""

from __future__ import annotations

from typing import Optional

from common.telegram_metadata import extract_telegram_metadata, resolve_explicit_cast
from common.title_cleaning import clean_title_text
from .base import VideoItem
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


def _load_catalog(load_performer_catalog_fn) -> list:
    try:
        return load_performer_catalog_fn()
    except Exception:
        return []


def _signature_cast(extracted: dict, catalog) -> tuple:
    """Return (cast names, cast source), promoting a verified channel signature to the cast."""
    cast_names = extracted["performer_names"]
    cast_source = extracted["performer_source"]
    if not cast_names and extracted["signature_name_candidates"]:
        signature_names, signature_ids = resolve_explicit_cast(
            extracted["signature_name_candidates"], catalog,
        )
        if signature_ids and all(signature_ids):
            cast_names = extracted["signature_name_candidates"]
            cast_source = "channel_signature"
            extracted["title"] = ""
            extracted["title_source"] = ""
    return cast_names, cast_source


def _performer_resolution(cast_names, cast_source, exact_records, catalog, extracted) -> tuple:
    """Return (performers, performer_resolution) from the explicit cast, catalogue credits, or neither."""
    if cast_names:
        try:
            resolved_names, resolved_ids = resolve_explicit_cast(cast_names, catalog)
        except Exception:
            resolved_names, resolved_ids = list(cast_names), [""] * len(cast_names)
        # Do not run legacy normalization here: it strips demonyms and
        # scene-like suffixes, which can be real parts of a performer name.
        performers = list(dict.fromkeys(
            str(name).strip() for name in resolved_names if str(name).strip()
        ))
        return performers, {
            "status": "resolved" if all(resolved_ids) else "partial",
            "method": "telegram_field_parser",
            "source": cast_source,
            "raw_names": cast_names,
            "external_ids": resolved_ids,
            "priority": 10,
            "title_candidate": extracted["title"],
            "title_source": extracted["title_source"],
            "version": 1,
        }
    if exact_records:
        performers = [str(row.get("name") or "").strip()
                      for row in exact_records if str(row.get("name") or "").strip()]
        resolved_ids = [str(row.get("id") or "") for row in exact_records]
        return performers, {
            "status": "resolved" if all(resolved_ids) else "partial",
            "method": "telegram_catalog_credit_v1",
            "source": "caption_credit_list",
            "raw_names": performers,
            "external_ids": resolved_ids,
            "priority": 10,
            "title_candidate": extracted["title"],
            "title_source": extracted["title_source"],
            "version": 1,
        }
    # A fresh post with no cast signal invalidates old, unproven cast
    # assignments. Higher priority scene matches survive this update.
    return [], {
        "status": "unresolved",
        "method": "telegram_field_parser",
        "source": "no_cast_field",
        "raw_names": [],
        "external_ids": [],
        "priority": 10,
        "title_candidate": extracted["title"],
        "title_source": extracted["title_source"],
        "version": 1,
    }


def _resolved_title(extracted: dict, performer_resolution, filtered_caption: str, fallback: str) -> str:
    title = extracted["title"] or (
        fallback if performer_resolution and performer_resolution.get("source") != "no_cast_field"
        else parse_caption_title(filtered_caption, fallback)
    )
    return clean_title_text(title, max_length=120) or clean_title_text(fallback, max_length=120)


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
    filename = msg.get("filename") or ""
    hashtags = extract_hashtags(caption + "\n" + str(filename))
    categories = ([channel_title] + ["#" + hashtag for hashtag in hashtags])[:16]
    filtered_caption = _filter_caption_for_title(caption)
    extracted = extract_telegram_metadata(caption, channel_title)
    # Typed metadata extraction also treats any plausible first line as a
    # title. Do not let that bypass the promo-text filter: channel ads often
    # repeat across many posts and are not the title of the attached video.
    if not filtered_caption and extracted.get("title_source") == "first_line":
        extracted["title"] = ""
        extracted["title_source"] = ""
    filename_title = _filename_to_title(filename)
    fallback = filename_title or f"{channel_title} #{message_id}"
    proposals = []
    performers = []
    performer_resolution = None
    try:
        from common.performer_catalog import match_catalog, prepare_catalog

        # Always use the external catalogue for identity resolution. The legacy
        # name list is derived from existing video assignments and is not evidence.
        catalog = _load_catalog(load_performer_catalog_fn)
        if catalog and prepared_catalog is None:
            prepared_catalog = prepare_catalog(catalog)
        cast_names, cast_source = _signature_cast(extracted, catalog)
        external_catalog = [
            row for row in catalog
            if isinstance(row, dict)
            and str(row.get("id") or "").startswith(("stashdb:", "theporndb:", "fansdb:"))
        ]
        credit_text = ", ".join(cast_names) if cast_names else caption
        exact_records, proposals = match_catalog(
            [("caption", credit_text)], external_catalog,
            prepared=prepared_catalog if external_catalog and prepared_catalog else None,
        )
        performers, performer_resolution = _performer_resolution(
            cast_names, cast_source, exact_records, catalog, extracted,
        )
    except Exception:
        performers, proposals, performer_resolution = [], [], None
    title = _resolved_title(extracted, performer_resolution, filtered_caption, fallback)
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
        performer_resolution=performer_resolution,
    )
