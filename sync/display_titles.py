"""Display-title cleanup and provider-specific title hooks."""

import html
import re
from typing import Any, Dict, Iterable, Optional

from common.performers import MEDIA_EXT_SUFFIX_RE, is_performer_segment
from common.performer_titles import strip_performer_credits


_SYNC_SLUG_TOKEN_HOOKS: Dict[str, Any] = {}
_LEADING_TAGS_RE = re.compile(
    r'^(?:[^\w\s#]*\s*#[a-zA-Z0-9_]*[a-zA-Z_][a-zA-Z0-9_]*\s*)+[-–—:]?\s*',
    re.UNICODE,
)
_LEADING_AGGREGATOR_RE = re.compile(
    r'^(?:only\s*fans|just\s*for\s*fans)\s*[-–—:]?\s*', re.IGNORECASE
)


def register_sync_slug_token_hook(provider: str, fn) -> None:
    """Register a (text, slug) -> text rule for one provider."""
    key = (provider or "").strip().lower()
    if not key:
        raise ValueError("Slug-token hook provider must be a non-empty string.")
    if not callable(fn):
        raise TypeError("Slug-token hook must be callable.")
    _SYNC_SLUG_TOKEN_HOOKS[key] = fn


def get_sync_slug_token_hook(provider: str):
    """Return the registered rule for a provider (None = passthrough)."""
    return _SYNC_SLUG_TOKEN_HOOKS.get((provider or "").strip().lower())


def clean_display_title(
    title: str,
    performers: Optional[Iterable[str]] = None,
    provider: str = "",
    slug: str = "",
) -> str:
    """Clean a display title while preserving its meaningful text."""
    if not title:
        return ""
    text = html.unescape(title).strip()
    text = re.sub(r'\s*\(@?[\w\.\-]+\)', '', text).strip()

    cleaned_tags = _LEADING_TAGS_RE.sub("", text).strip()
    if cleaned_tags:
        text = cleaned_tags

    text = MEDIA_EXT_SUFFIX_RE.sub("", text).strip()
    cleaned_aggregator = _LEADING_AGGREGATOR_RE.sub("", text).strip()
    if cleaned_aggregator:
        text = cleaned_aggregator

    hook = get_sync_slug_token_hook(provider)
    if hook and slug:
        try:
            text = hook(text, slug)
        except Exception:
            pass

    perfs_list = list(performers) if performers else []
    return strip_performer_credits(text, perfs_list, is_performer_segment)
