"""Conservative extraction of typed metadata from Telegram captions.

Telegram captions can contain a scene title, cast, both, or neither. Keep the
fields separate so performer matching never scans arbitrary title or promo text.
"""

from __future__ import annotations

import os
import re
from typing import Iterable

from common.title_cleaning import clean_title_text


_FIELD_RE = re.compile(r"^\s*(?P<label>[^|:]{1,24})\s*[|:]\s*(?P<value>.*?)\s*$")
_CAST_LABEL_RE = re.compile(r"\b(?:cast|names|performers?|actors?)\b|👤", re.IGNORECASE)
_TITLE_LABEL_RE = re.compile(r"\b(?:title|scene|video)\b|📹|🎥|🎬", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"\s*(?:,|&|\band\b)\s*", re.IGNORECASE)
_HASHTAG_OR_LINK_RE = re.compile(r"(?:#\S+|@\w+|https?://\S+|t\.me/\S+)")
_ALNUM_RE = re.compile(r"[^\W_]", re.UNICODE)
_DECORATION_RE = re.compile(r"^[\W_]+$", re.UNICODE)
_PROMO_RE = re.compile(r"\b(?:website|quality|format|subscribe|telegram|studios? media online)\b", re.IGNORECASE)


def _clean_line(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n-–—:|")


def _is_name_list(value: str) -> list[str]:
    """Return a syntactically clear multi-name list, otherwise an empty list."""
    text = _clean_line(value)
    if not re.search(r",|&|\band\b", text, re.IGNORECASE):
        return []
    names = [_clean_line(part) for part in _SEPARATOR_RE.split(text)]
    if len(names) < 2 or any(not name for name in names):
        return []
    if any(len(name) > 80 or len(name.split()) > 5 for name in names):
        return []
    if any(_HASHTAG_OR_LINK_RE.search(name) for name in names):
        return []
    return names


def _labeled_fields(lines: list[str]) -> tuple[str, str, list[str], str]:
    """Return (title, title_source, cast_names, cast_source) from explicit ``Label: value`` lines."""
    title = ""
    title_source = ""
    cast_names: list[str] = []
    cast_source = ""
    for line in lines:
        match = _FIELD_RE.match(line)
        if not match:
            continue
        label = match.group("label").strip()
        value = _clean_line(match.group("value"))
        if not value:
            continue
        if _CAST_LABEL_RE.search(label) and not cast_names:
            cast_names = _is_name_list(value)
            if not cast_names:
                cast_names = [value]
            cast_source = "labeled_cast"
        elif _TITLE_LABEL_RE.search(label) and not title:
            title = clean_title_text(value, max_length=120)
            title_source = "labeled_title"
    return title, title_source, cast_names, cast_source


def _signature_cast_names(lines: list[str], lower_caption: str, cast_names: list[str]) -> list[str]:
    signature_channel = (os.getenv("TELEGRAM_SIGNATURE_CHANNEL") or "").strip().casefold()
    if not cast_names and signature_channel and signature_channel in lower_caption:
        first = next((line for line in lines if not line.startswith(("#", "@"))), "")
        return _is_name_list(first)
    return []


def _first_line_title(lines: list[str], cast_source: str) -> tuple[str, str]:
    """Return (title, title_source) from the first prose line, or ("", "") when none qualifies."""
    for line in lines:
        if cast_source == "labeled_cast":
            # A labeled cast does not make nearby prose a scene title.
            # The title must have its own explicit label in this format.
            continue
        match = _FIELD_RE.match(line)
        if match and (_CAST_LABEL_RE.search(match.group("label"))
                      or _TITLE_LABEL_RE.search(match.group("label"))):
            continue
        candidate = re.sub(r"^(?:[🎬🎥📹]\s*)", "", line).strip()
        if (candidate and _ALNUM_RE.search(candidate)
                and not candidate.startswith(("#", "@"))
                and not _DECORATION_RE.fullmatch(candidate)
                and not _PROMO_RE.search(candidate)):
            return clean_title_text(candidate, max_length=120), "first_line"
    return "", ""


def extract_telegram_metadata(caption: str, channel_title: str = "") -> dict:
    """Extract an explicit scene title and cast field from one Telegram post.

    A first-line cast list is accepted only for the configured signature layout:
    the post carries its distinctive account handle and the first line is a
    compact, delimiter-separated list. Channel identity alone is insufficient.
    """
    lines = [line.strip() for line in str(caption or "").splitlines() if line.strip()]
    title, title_source, cast_names, cast_source = _labeled_fields(lines)
    signature_names = _signature_cast_names(lines, str(caption or "").casefold(), cast_names)
    if not title:
        title, title_source = _first_line_title(lines, cast_source)

    return {
        "title": title,
        "title_source": title_source,
        "performer_names": cast_names,
        "performer_source": cast_source,
        "signature_name_candidates": signature_names,
        "source_channel": str(channel_title or ""),
    }


def resolve_explicit_cast(names: Iterable[str], catalog: Iterable[dict]) -> tuple[list[str], list[str]]:
    """Resolve whole cast-field entries to external identities when unique.

    Unknown names from an explicitly labeled cast field are retained literally;
    local identities derived from previous video assignments are not used.
    """
    from common.performer_catalog import normalize_name

    external_catalog = [
        row for row in catalog
        if isinstance(row, dict)
        and str(row.get("id") or "").startswith(("stashdb:", "theporndb:", "fansdb:"))
    ]
    aliases: dict[str, dict[str, dict]] = {}
    for row in external_catalog:
        performer_id = str(row.get("id") or "")
        for value in [row.get("name"), *(row.get("aliases") or [])]:
            key = normalize_name(str(value or ""))
            if key:
                aliases.setdefault(key, {})[performer_id] = row

    resolved_names: list[str] = []
    resolved_ids: list[str] = []
    for raw_name in names:
        candidate = _clean_line(str(raw_name))
        if not candidate:
            continue
        matches = aliases.get(normalize_name(candidate), {})
        if len(matches) == 1:
            row = next(iter(matches.values()))
            resolved_names.append(str(row.get("name") or candidate))
            resolved_ids.append(str(row.get("id") or ""))
        else:
            resolved_names.append(candidate)
            resolved_ids.append("")
    return resolved_names, resolved_ids
