"""Shared title spellings used when querying external scene catalogues."""

from __future__ import annotations

import re

from common.performers import is_known_non_performer_label, strip_scene_suffix
from common.title_cleaning import clean_title_text

_MEDIA_EXT_RE = re.compile(r"\.(?:mp4|mkv|avi|mov|wmv|webm|m4v|ts|mpg|mpeg)$", re.IGNORECASE)
_YEAR_PARENS_RE = re.compile(r"\s*\((?:19|20)\d{2}\)")
_LEADING_TAG_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
_DASH_SPLIT_RE = re.compile(r"\s+[–—\-]\s+")
_QUALITY_RE = re.compile(r"\b(?:4k|2160p|1080p|720p|480p|hd|fhd|uhd|sd|xxx)\b", re.IGNORECASE)


def title_segments(title: str) -> list[str]:
    """Return cleaned dash-separated segments for matching and cast extraction."""
    text = _MEDIA_EXT_RE.sub("", str(title or "").strip())
    text = _LEADING_TAG_RE.sub("", text)
    text = _clean_segment(text)
    if not text:
        return []
    return [
        segment for segment in (_clean_segment(part) for part in _DASH_SPLIT_RE.split(text))
        if segment
    ]


def _clean_segment(value: str) -> str:
    """Remove catalogue noise from one dash-separated title segment."""
    text = _YEAR_PARENS_RE.sub("", str(value or ""))
    text = _QUALITY_RE.sub("", text)
    text = clean_title_text(text, max_length=200)
    text = strip_scene_suffix(text).strip()
    return re.sub(r"\s+", " ", text).strip(" -–—:|,.;!?_")


def _segment_score(value: str) -> int:
    """Rank segments that look like scene titles over studio names and cast lists."""
    if not value or is_known_non_performer_label(value):
        return -100
    words = value.split()
    score = 0
    if 1 <= len(words) <= 6:
        score += 2
    if "&" not in value and not any(char.isdigit() for char in value):
        score += 1
    if len(words) >= 3 and sum(1 for word in words if word[:1].isupper()) >= 3:
        score -= 2
    return score


def lookup_title_variants(title: str, max_variants: int = 3) -> list[str]:
    """Return short, then full, title spellings for a catalogue lookup.

    Telegram titles commonly prepend a studio and append a scene/part number or
    a cast list (``Raging Stallion - Overdrive Sc1 - Paul Wagner & Derek Kage``).
    Try the most title-like dash segment first, then the full cleaned title.
    The caller still applies its normal title/runtime acceptance rules.
    """
    text = _MEDIA_EXT_RE.sub("", str(title or "").strip())
    text = _LEADING_TAG_RE.sub("", text)
    text = _clean_segment(text)
    if not text:
        return []

    parts = [_clean_segment(part) for part in _DASH_SPLIT_RE.split(text)]
    parts = [part for part in parts if part]
    ranked = sorted(
        enumerate(parts),
        key=lambda item: (_segment_score(item[1]), -item[0]),
        reverse=True,
    )
    candidates = [value for _index, value in ranked if _segment_score(value) > 0]
    candidates.append(text)

    result = []
    seen = set()
    for value in candidates:
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
        if len(result) >= max(1, int(max_variants)):
            break
    return result
