"""Telegram message normalization and video-reference extraction."""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any, List, Optional, Tuple

from common.title_cleaning import clean_title_text, extract_title_hashtags

from .base import CategoryItem


_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")

_LEADING_TAGS_RE = re.compile(
    r'^(?:[^\w\s#]*\s*#[a-zA-Z0-9_]*[a-zA-Z_][a-zA-Z0-9_]*\s*)+[-–—:]?\s*',
    re.UNICODE,
)

_TITLE_HAS_ALNUM_RE = re.compile(r"[^\W_]", re.UNICODE)

_TITLE_MENTION_RE = re.compile(r"@[\w_]+", re.UNICODE)

_TITLE_URL_RE = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+", re.IGNORECASE)

def parse_caption_title(caption: str, fallback: str) -> str:
    """First non-empty line of caption (max 120 chars) or fallback.

    Lines consisting exclusively of hashtags (e.g. '#ANAL #MEN') are skipped
    in favour of a line with content. Leading hashtags and category prefixes
    (e.g. '#ANAL #MEN - Arno Antino, Enzo Muller') are stripped so the title
    is clean ('Arno Antino, Enzo Muller').
    """
    for line in (caption or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        cleaned = clean_title_text(_LEADING_TAGS_RE.sub("", stripped))
        if cleaned:
            return clean_title_text(cleaned, max_length=120)
    return clean_title_text(fallback, max_length=120)

def extract_hashtags(caption: str) -> List[str]:
    """Extract category ``#tag`` markers (numeric scene IDs remain in titles).

    Names preserve the first spelling, deduplicate case-insensitively, and cap at 15.
    Empty/None -> [].
    """
    return extract_title_hashtags(caption)

def _filter_caption_for_title(caption: str) -> str:
    """Keep only lines containing at least one letter or digit (unicode-aware).

    Lines consisting SOLELY of mentions/links/emojis are stripped:
    mentions (@user) and links (http://, t.me/, www.) are removed,
    then regex ``[^\\W_]`` checks if any letter/digit remains.
    When all lines match this -> returns "" (caption treated as empty).
    """
    if not caption or not isinstance(caption, str):
        return ""
    kept: List[str] = []
    for line in caption.splitlines():
        if not line.strip():
            continue
        tmp = _TITLE_URL_RE.sub("", line)
        tmp = _TITLE_MENTION_RE.sub("", tmp)
        if not _TITLE_HAS_ALNUM_RE.search(tmp):
            continue
        kept.append(line)
    text = "\n".join(kept)
    # Cyrillic promo-spam: more than one Cyrillic word -> treat as
    # empty (single word, e.g. title, remains). Fallback: filename/channel.
    if text and _CYRILLIC_RE.search(text) and len(text.split()) > 1:
        return ""
    return text

def _filename_to_title(filename: str) -> str:
    """Filename -> title: no extension, `_`/`-`/`.` -> spaces, max 120."""
    if not filename or not isinstance(filename, str):
        return ""
    base = os.path.basename(filename.strip())
    if not base:
        return ""
    stem, _ext = os.path.splitext(base)
    if not stem.strip():
        return ""
    title = re.sub(r"[_\-\.]+", " ", stem)
    return clean_title_text(title, max_length=120)

def _text_hits(haystack: str, flat: str, clean: str) -> bool:
    """Granice slow albo (dla nazw wielowyrazowych >= 8 liter) wariant bezspacjowy."""
    try:
        pattern = r"(?<!\w)" + re.escape(clean) + r"(?!\w)"
        if re.search(pattern, haystack, re.IGNORECASE | re.UNICODE):
            return True
        if " " in clean:
            nospace = re.sub(r"\s+", "", clean)
            if len(nospace) >= 8:
                return nospace.lower() in flat.lower()
    except re.error:
        pass
    return False

def _nested_unique_id(value) -> Optional[str]:
    if isinstance(value, dict):
        for key in ("file_unique_id", "file_unique", "unique_id", "id"):
            candidate = value.get(key)
            if candidate:
                return str(candidate)
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None

def _extract_file_unique_id(message_dict: dict) -> Optional[str]:
    """First non-empty nested file id, else first flat file id, else None."""
    unique_id: Optional[str] = None
    for key in ("video", "document", "video_note", "animation", "file"):
        unique_id = _nested_unique_id(message_dict.get(key))
        if unique_id:
            break
    if not unique_id:
        for key in ("file_unique_id", "file_unique", "unique_id"):
            candidate = message_dict.get(key)
            if candidate:
                unique_id = str(candidate)
                break
    return unique_id

def extract_file_ref(message_dict) -> Optional[str]:
    """Wybiera material video (video / document-video), pomija tekst i zdjecia.

    Zwraca stabilny ref ``{channel_id}:{message_id}:{file_unique_id}``
    albo None, gdy wiadomosc nie zawiera video.
    """
    if not isinstance(message_dict, dict) or not message_dict:
        return None
    kind = str(message_dict.get("kind") or "").strip().lower()
    if kind and kind not in ("video", "document-video", "video-note",
                             "animation", "document"):
        return None
    unique_id = _extract_file_unique_id(message_dict)
    if not unique_id:
        return None
    channel_id = message_dict.get("channel_id", message_dict.get("chat_id"))
    message_id = message_dict.get("message_id", message_dict.get("id"))
    if channel_id is None or message_id is None:
        return None
    try:
        message_id_int = int(message_id)
    except (TypeError, ValueError):
        return None
    return f"{channel_id}:{message_id_int}:{unique_id}"

def _caption_of(msg: dict) -> str:
    for key in ("caption", "message", "text"):
        value = msg.get(key)
        if isinstance(value, str) and value:
            return value
    return ""

def _duration_of(msg: dict) -> int:
    for key in ("duration",):
        try:
            if msg.get(key) is not None:
                return int(msg[key])
        except (TypeError, ValueError):
            continue
    for key in ("video", "document", "file"):
        nested = msg.get(key)
        if isinstance(nested, dict) and nested.get("duration") is not None:
            try:
                return int(nested["duration"])
            except (TypeError, ValueError):
                continue
    return 0

def _size_of(msg: dict) -> Optional[int]:
    if msg.get("size") is not None:
        try:
            return int(msg["size"])
        except (TypeError, ValueError):
            pass
    for key in ("video", "document", "file"):
        nested = msg.get(key)
        if isinstance(nested, dict) and nested.get("size") is not None:
            try:
                return int(nested["size"])
            except (TypeError, ValueError):
                continue
    return None

def channel_to_category(channel_id, title) -> CategoryItem:
    """Map Telegram channel to CategoryItem (channel = studio/category)."""
    text = str(channel_id)
    display = (title or "").strip() if isinstance(title, str) else str(title or "")
    if not display:
        display = text
    if text.startswith("-100"):
        url = f"https://t.me/c/{text[4:]}"
    elif text.lstrip("-").isdigit():
        url = f"https://t.me/c/{text.lstrip('-')}"
    else:
        url = f"https://t.me/{text}"
    return CategoryItem(id=text, title=display, slug=text, url=url)

def thumb_api_path(ref: str) -> str:
    """Wzgledna sciezka miniatury przez resolver (sync dokleja swoj base URL)."""
    return "/admin/api/thumb?provider=telegram&ref=" + urllib.parse.quote(ref or "", safe="")

def _msg_caption_text(msg: Any) -> str:
    """Message text from ``message`` or ``text``, always returned as a str."""
    caption = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
    if not isinstance(caption, str):
        caption = str(caption)
    return caption


def _resolve_message_document(msg: Any, media: Any) -> Any:
    """Document from media.document, falling back to a non-callable msg.document."""
    document = getattr(media, "document", None) if media is not None else None
    if document is None:
        candidate = getattr(msg, "document", None)
        if candidate is not None and not callable(candidate):
            document = candidate
    return document


def _scan_video_attribute(document: Any, is_video: bool) -> Tuple[bool, int]:
    """Return (is_video, duration) after scanning document attributes for a Video attribute."""
    duration = 0
    try:
        for attr in (getattr(document, "attributes", None) or []):
            if "Video" in type(attr).__name__:
                is_video = True
                dur = getattr(attr, "duration", None)
                if dur:
                    duration = int(dur)
                break
    except (TypeError, ValueError):
        pass
    return is_video, duration


def _document_filename(document: Any) -> str:
    """First non-blank ``file_name`` attribute of the document, stripped; else ""."""
    filename = ""
    try:
        for attr in (getattr(document, "attributes", None) or []):
            fname = getattr(attr, "file_name", None)
            if isinstance(fname, str) and fname.strip():
                filename = fname.strip()
                break
    except (TypeError, ValueError):
        pass
    return filename


def message_object_to_dict(msg: Any, channel_ref: Any) -> dict:
    """Normalize a Telethon-like message object into the provider's message shape."""
    message_id = getattr(msg, "id", None)
    caption = _msg_caption_text(msg)
    duration = 0
    mime_type = ""
    size = getattr(getattr(msg, "file", None), "size", None)
    unique_id: Optional[str] = None
    filename = ""
    kind = "none"
    media = getattr(msg, "media", None)
    document = _resolve_message_document(msg, media)
    if document is not None:
        mime_type = str(getattr(document, "mime_type", "") or "")
        if size is None:
            size = getattr(document, "size", None)
        doc_id = getattr(document, "id", None)
        if doc_id is not None:
            unique_id = str(doc_id)
        is_video, duration = _scan_video_attribute(document, mime_type.startswith("video"))
        filename = _document_filename(document)
        kind = "video" if is_video else "other"
    out = {
        "channel_id": channel_ref,
        "chat_id": channel_ref,
        "message_id": message_id,
        "id": message_id,
        "caption": caption,
        "kind": kind,
        "duration": duration,
        "mime_type": mime_type,
        "size": size,
        "filename": filename,
    }
    if unique_id:
        out["file_unique_id"] = unique_id
        out["video" if kind == "video" else "document"] = {
            "file_unique_id": unique_id,
            "duration": duration,
            "mime_type": mime_type,
            "size": size,
        }
    return out
