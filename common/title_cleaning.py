"""Shared cleanup rules for titles originating in source listings."""

from __future__ import annotations

import html
import re
import unicodedata
from typing import Optional


_HASHTAG_RE = re.compile(r"#([\w]+)", re.UNICODE)
_WORD_RE = re.compile(r"\w+(?:['’]\w+)*", re.UNICODE)
_REPEATED_MARKS_RE = re.compile(r"[!?]{2,}")
_EDGE_PUNCTUATION = " \t\r\n-–—:|,.;!?…'\""


def _is_category_hashtag(name: str) -> bool:
    """Numeric-only markers such as #203 are scene numbers, not category tags."""
    return any(char == "_" or char.isalpha() for char in name)


def extract_title_hashtags(text: str, limit: int = 15) -> list[str]:
    """Return category hashtags in source order, preserving the first spelling."""
    if not isinstance(text, str) or not text:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for match in _HASHTAG_RE.finditer(text):
        name = match.group(1)
        if not name or not _is_category_hashtag(name):
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(name)
        if len(result) >= max(0, int(limit)):
            break
    return result


def _remove_category_hashtags(text: str) -> str:
    groups: list[list[int]] = []
    separators = re.compile(r"[\s,;:|.!?–—-]*")
    for match in _HASHTAG_RE.finditer(text):
        if not _is_category_hashtag(match.group(1)):
            continue
        if groups and separators.fullmatch(text[groups[-1][1]:match.start()]):
            groups[-1][1] = match.end()
        else:
            groups.append([match.start(), match.end()])
    if not groups:
        return text

    result: list[str] = []
    position = 0
    trailing_separators = re.compile(r"[ \t]*[,;:|.!?–—-]?[ \t]*")
    for start, end in groups:
        result.append(text[position:start])
        result.append(" ")
        suffix = text[end:]
        trailing = trailing_separators.match(suffix)
        position = end + (trailing.end() if trailing else 0)
    result.append(text[position:])
    return "".join(result)


def _remove_symbols_and_controls(text: str) -> str:
    kept: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if char.isspace() or category[0] in {"L", "N", "P", "M"} or category == "Sc":
            codepoint = ord(char)
            if 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF:
                kept.append(" ")
            else:
                kept.append(char)
        else:
            kept.append(" ")
    return "".join(kept)


def _collapse_repeated_whole_title(text: str) -> str:
    words = list(_WORD_RE.finditer(text))
    count = len(words)
    for width in range(1, count // 2 + 1):
        if width == 1 and (count != 2 or len(words[0].group(0)) < 5):
            continue
        first = [match.group(0).casefold() for match in words[:width]]
        repeats = 1
        while (repeats + 1) * width <= count and all(
            words[repeats * width + index].group(0).casefold() == first[index]
            for index in range(width)
        ):
            repeats += 1
        if repeats >= 2:
            prefix = text[:words[width - 1].end()].rstrip(_EDGE_PUNCTUATION)
            suffix = text[words[repeats * width - 1].end():].lstrip()
            text = f"{prefix} {suffix}".strip() if suffix else prefix
            break
    return text


def clean_title_text(value: object, max_length: Optional[int] = None) -> str:
    """Remove category tags and decorative symbols, then normalize whitespace.

    Numeric-only hashtags are retained as title identifiers (for example, #203).
    Repeated punctuation and exact whole-title copies are removed before truncation.
    """
    if not isinstance(value, str) or not value:
        return ""
    text = html.unescape(value)
    text = re.sub(r"[0-9#*]\ufe0f?\u20e3", " ", text)
    text = _remove_category_hashtags(text)
    text = _remove_symbols_and_controls(text)
    text = _REPEATED_MARKS_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(_EDGE_PUNCTUATION)
    text = _collapse_repeated_whole_title(text)
    text = re.sub(r"\s+", " ", text).strip(_EDGE_PUNCTUATION)
    if max_length is not None:
        text = text[:max(0, int(max_length))].rstrip(_EDGE_PUNCTUATION)
    return text
