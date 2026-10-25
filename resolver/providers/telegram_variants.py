"""Telegram video qualities and alternative documents helper.

Implements detection, attribute parsing, and filtering for Telegram alternative
documents (alt_documents) per https://core.telegram.org/api/files#video-qualities
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

from telethon.tl import functions, types

logger = logging.getLogger("providers.telegram.variants")

# Cache for MTProto app_config to avoid querying GetAppConfigRequest repeatedly
_APP_CONFIG_CACHE: Optional[Dict[str, Any]] = None
_APP_CONFIG_CACHE_TIME: float = 0.0
_APP_CONFIG_CACHE_TTL: float = 3600.0  # 1 hour


def parse_tl_json(val: Any) -> Any:
    """Convert Telethon JSON TL objects into python native types."""
    if val is None:
        return None
    name = type(val).__name__
    if name == "JsonNull":
        return None
    if name in ("JsonBool", "JsonNumber", "JsonString"):
        return val.value
    if name == "JsonArray":
        return [parse_tl_json(x) for x in val.value]
    if name == "JsonObject":
        return {item.key: parse_tl_json(item.value) for item in val.value}
    if isinstance(val, dict):
        return {k: parse_tl_json(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [parse_tl_json(x) for x in val]
    return val


async def should_ignore_alt_documents(client: Any = None) -> bool:
    """Check if alt_documents must be ignored per configuration or server flags.

    Checks:
      1. Local environment variable TELEGRAM_VIDEO_IGNORE_ALT_DOCUMENTS
      2. Configuration file setting (if configured)
      3. MTProto help.getAppConfig 'video_ignore_alt_documents'
    """
    global _APP_CONFIG_CACHE, _APP_CONFIG_CACHE_TIME

    # 1. Environment variable override
    env_flag = (os.getenv("TELEGRAM_VIDEO_IGNORE_ALT_DOCUMENTS") or "").strip().lower()
    if env_flag in ("1", "true", "yes", "on"):
        return True
    if env_flag in ("0", "false", "no", "off"):
        # Explicit false override
        return False

    # 2. Check config loader
    try:
        from .telegram_config import _get_config
        cfg = _get_config()
        if cfg:
            tg_src = cfg.get_source("telegram")
            if tg_src and getattr(tg_src, "video_ignore_alt_documents", False):
                return True
    except Exception as exc:
        logger.debug("Config check for video_ignore_alt_documents skipped: %s", exc)

    # 3. MTProto help.getAppConfig
    if client is not None:
        now = time.monotonic()
        if _APP_CONFIG_CACHE is None or (now - _APP_CONFIG_CACHE_TIME) > _APP_CONFIG_CACHE_TTL:
            try:
                res = await client(functions.help.GetAppConfigRequest(hash=0))
                if hasattr(res, "config"):
                    _APP_CONFIG_CACHE = parse_tl_json(res.config)
                    _APP_CONFIG_CACHE_TIME = now
            except Exception as exc:
                logger.debug("Failed to fetch MTProto app config: %s", exc)

        if isinstance(_APP_CONFIG_CACHE, dict):
            if bool(_APP_CONFIG_CACHE.get("video_ignore_alt_documents")):
                return True

    return False


def is_full_video_document(doc: Any) -> bool:
    """Check whether a Document represents a standalone video stream.

    Filters out HLS playlists, storyboards, storyboard maps, voice/round notes,
    stickers, images, and non-video attachments.
    """
    if doc is None:
        return False

    mime_type = str(getattr(doc, "mime_type", "") or "").lower().strip()
    # Explicitly exclude playlists, storyboards, and maps
    if mime_type in (
        "application/x-mpegurl",
        "application/vnd.apple.mpegurl",
        "application/x-tgstoryboard",
        "application/x-tgstoryboardmap",
    ):
        return False

    video_attr = None
    for attr in (getattr(doc, "attributes", None) or []):
        if isinstance(attr, types.DocumentAttributeVideo) or "Video" in type(attr).__name__:
            video_attr = attr
            break

    # If neither video attribute nor video mime type is present, it's not a video
    if video_attr is None and not mime_type.startswith("video/"):
        return False

    # Round video messages (telegram video notes) are not movie variants
    if video_attr is not None and getattr(video_attr, "round_message", False):
        return False

    return True


def extract_variant_info(doc: Any, *, is_original: bool = False) -> Optional[Dict[str, Any]]:
    """Extract truthful variant metadata from a Document or dict.

    Returns None if the document is not a valid full video stream.
    Bitrate is strictly separated into:
      - bitrate: reliably known upstream bitrate (null when absent from MTProto)
      - estimated_average_bitrate: calculated from file_size and duration
    """
    if not is_full_video_document(doc):
        return None

    doc_id = getattr(doc, "id", None)
    if doc_id is None and isinstance(doc, dict):
        doc_id = doc.get("id") or doc.get("file_unique_id")

    video_attr = None
    for attr in (getattr(doc, "attributes", None) or []):
        if isinstance(attr, types.DocumentAttributeVideo) or "Video" in type(attr).__name__:
            video_attr = attr
            break

    w = getattr(video_attr, "w", None) if video_attr else None
    h = getattr(video_attr, "h", None) if video_attr else None
    codec = getattr(video_attr, "video_codec", None) if video_attr else None
    dur = getattr(video_attr, "duration", None) if video_attr else None

    # Fallback to dict / attributes if mock or dict
    if isinstance(doc, dict):
        w = w if w is not None else doc.get("width", doc.get("w"))
        h = h if h is not None else doc.get("height", doc.get("h"))
        codec = codec if codec is not None else doc.get("codec")
        dur = dur if dur is not None else doc.get("duration")

    size = getattr(doc, "size", None)
    if size is None and isinstance(doc, dict):
        size = doc.get("size") or doc.get("file_size")

    mime_type = getattr(doc, "mime_type", None)
    if mime_type is None and isinstance(doc, dict):
        mime_type = doc.get("mime_type")

    # Clean width / height
    int_w = None
    if w is not None:
        try:
            int_w = int(w) if int(w) > 0 else None
        except (TypeError, ValueError):
            pass

    int_h = None
    if h is not None:
        try:
            int_h = int(h) if int(h) > 0 else None
        except (TypeError, ValueError):
            pass

    # Clean size
    int_size = None
    if size is not None:
        try:
            int_size = int(size) if int(size) >= 0 else None
        except (TypeError, ValueError):
            pass

    # Clean duration
    float_dur = None
    if dur is not None:
        try:
            val_dur = float(dur)
            float_dur = round(val_dur, 3) if val_dur > 0 else None
        except (TypeError, ValueError):
            pass

    # Estimated average bitrate in bps: (file_size * 8) / duration
    estimated_bitrate = None
    if int_size is not None and int_size > 0 and float_dur is not None and float_dur > 0:
        estimated_bitrate = int(round((int_size * 8) / float_dur))

    # Stable, opaque identifier: "original" for original, "alt_<id>" for alternatives
    if is_original:
        variant_id = "original"
    else:
        variant_id = f"alt_{doc_id}" if doc_id is not None else "alt_unknown"

    return {
        "id": variant_id,
        "is_original": is_original,
        "width": int_w,
        "height": int_h,
        "codec": str(codec).strip().lower() if codec else None,
        "mime_type": str(mime_type).strip() if mime_type else None,
        "file_size": int_size,
        "duration": float_dur,
        "bitrate": None,
        "estimated_average_bitrate": estimated_bitrate,
    }


def extract_message_variants(
    message: Any,
    *,
    ignore_alt_documents: bool = False,
) -> Dict[str, Any]:
    """Inspect a Telegram message and build variants list with status.

    Returns a dict with:
      - status: "ready" | "original_only" | "processing"
      - video_processing_pending: bool
      - has_alternatives: bool
      - variants: list of variant dicts
    """
    media = getattr(message, "media", None) or getattr(message, "document", None)
    orig_doc = getattr(media, "document", None) or (media if hasattr(media, "id") else None)

    pending = bool(getattr(message, "video_processing_pending", False))

    orig_variant = extract_variant_info(orig_doc, is_original=True)

    alt_variants: List[Dict[str, Any]] = []
    if not ignore_alt_documents and media is not None:
        for alt in (getattr(media, "alt_documents", None) or []):
            alt_info = extract_variant_info(alt, is_original=False)
            if alt_info is not None:
                alt_variants.append(alt_info)

    # Sort alternatives by resolution (height) descending, then estimated bitrate descending
    alt_variants.sort(
        key=lambda x: (
            x.get("height") or 0,
            x.get("estimated_average_bitrate") or 0,
            x.get("file_size") or 0,
        ),
        reverse=True,
    )

    if pending:
        status = "processing"
    elif len(alt_variants) > 0:
        status = "ready"
    else:
        status = "original_only"

    all_variants = [orig_variant] if orig_variant else []
    all_variants.extend(alt_variants)

    return {
        "status": status,
        "video_processing_pending": pending,
        "has_alternatives": len(alt_variants) > 0,
        "variants": all_variants,
    }


def find_variant_document(media: Any, variant_id: str) -> Optional[Any]:
    """Locate the Document object matching a variant identifier or document ID.

    Accepts:
      - "original" or the original document's ID -> returns original Document
      - "alt_<id>" or "<id>" -> returns matching alt Document from alt_documents
    """
    if media is None or not variant_id:
        return None

    clean_id = str(variant_id).strip()
    orig_doc = getattr(media, "document", None) or (media if hasattr(media, "id") else None)
    orig_doc_id = str(getattr(orig_doc, "id", None) or "")

    if clean_id.lower() == "original" or (orig_doc_id and clean_id == orig_doc_id):
        return orig_doc

    # Check alt_documents
    target_num = clean_id[len("alt_"):] if clean_id.startswith("alt_") else clean_id
    for alt in (getattr(media, "alt_documents", None) or []):
        alt_id = str(getattr(alt, "id", None) or "")
        if alt_id and (clean_id == f"alt_{alt_id}" or target_num == alt_id):
            return alt

    return None
