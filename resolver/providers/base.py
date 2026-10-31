import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Iterable
from common.performers import (
    MEDIA_EXT_SUFFIX_RE,
    is_performer_segment,
    is_valid_performer_name,  # noqa: F401
)
from common.performer_titles import parse_title_performers_with_known_map as _parse_title_performers_with_known_map
from common.performer_titles import strip_performer_credits as _strip_performer_credits
from common.title_cleaning import clean_title_text

# ---------------------------------------------------------------------------
# Neutral extension hooks (ETAP C).
#
# Provider-specific rules that used to be hardcoded here now arrive via
# hooks registered by the optional web extension. Defaults are passthrough
# (no special-casing), so the public core never names a web provider.
# ---------------------------------------------------------------------------

# slug-token hooks: fn(cleaned_title, slug) -> cleaned_title, per provider.
_SLUG_TOKEN_HOOKS: Dict[str, Callable[[str, str], str]] = {}


def register_slug_token_hook(provider_name: str, fn: Callable[[str, str], str]):
    """Register a provider-specific slug-token title rule. Returns fn."""
    name = (provider_name or "").strip()
    if not name:
        raise ValueError("Hook provider name must be a non-empty string.")
    if not callable(fn):
        raise TypeError("Slug-token hook must be callable.")
    _SLUG_TOKEN_HOOKS[name] = fn
    return fn


def get_slug_token_hook(provider_name: str) -> Optional[Callable[[str, str], str]]:
    """Return the slug-token hook for a provider, or None (passthrough)."""
    return _SLUG_TOKEN_HOOKS.get((provider_name or "").strip())


def parse_title_performers(title: str, known_performers: Optional[Iterable[str]] = None) -> List[str]:
    # Resolver callers pass a collection of names; preserve its historical map policy.
    known_set = {k.strip().lower(): k.strip() for k in known_performers} if known_performers else {}
    return _parse_title_performers_with_known_map(title, known_set)




_LEADING_TAGS_RE = re.compile(
    r'^(?:[^\w\s#]*\s*#[a-zA-Z0-9_]*[a-zA-Z_][a-zA-Z0-9_]*\s*)+[-–—:]?\s*',
    re.UNICODE,
)
_LEADING_AGGREGATOR_RE = re.compile(
    r'^(?:only\s*fans|just\s*for\s*fans)\s*[-–—:]?\s*', re.IGNORECASE
)


def clean_display_title(
    title: str,
    performers: Optional[Iterable[str]] = None,
    provider: str = "",
    slug: str = "",
) -> str:
    """Return a cleaned display title suitable for Jellyfin / UI cards.

    Strips parenthesized handles, leading category hashtags, and removes performer
    prefix/suffix separated by dash/colon when performers are already known. Preserves
    original title if removing performers leaves nothing or destroys the title.

    Provider-specific slug-token rules (e.g. stripping a trailing random
    hash from a video slug) are NOT hardcoded here: they arrive via
    :func:`register_slug_token_hook`. With no hook registered for the
    provider the default is passthrough (no stripping). The optional web
    extension registers its own rules; the public core stays neutral.
    """
    if not title:
        return ""
    text = clean_title_text(title)
    if not text:
        return ""

    # 1. Strip parenthesized handles e.g. (handle) or (@handle)
    text = re.sub(r'\s*\(@?[\w\.\-]+\)', '', text).strip()

    # 2. Strip leading hashtags and category prefixes e.g. #ANAL #MEN -
    cleaned_tags = _LEADING_TAGS_RE.sub("", text).strip()
    if cleaned_tags:
        text = cleaned_tags

    # 3. Strip file extensions (.mp4, .mkv, etc.) and leading aggregators (Only Fans, Just For Fans)
    text = MEDIA_EXT_SUFFIX_RE.sub("", text).strip()
    cleaned_agg = _LEADING_AGGREGATOR_RE.sub("", text).strip()
    if cleaned_agg:
        text = cleaned_agg

    # 4. Provider-specific slug-token rule via hook (default: passthrough).
    hook = get_slug_token_hook(provider)
    if hook is not None and slug:
        text = hook(text, slug)

    perfs_list = list(performers) if performers else []
    return _strip_performer_credits(text, perfs_list, is_performer_segment)



@dataclass
class VideoItem:
    id: str                      # Format: "provider:video_id", e.g. "telegram:ch:42:uniq"
    provider: str                # e.g. "telegram"
    provider_video_id: str       # e.g. "ch:42:uniq" (opaque, provider-defined)
    title: str
    slug: str
    url: str
    thumbnail: str
    duration: int                # duration in seconds
    performers: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)
    views: Optional[str] = None
    published: Optional[str] = None
    description: Optional[str] = None
    source_text: str = ""
    source_filename: str = ""
    performer_proposals: List[Dict[str, str]] = field(default_factory=list)
    performer_resolution: Optional[Dict[str, object]] = None

@dataclass
class CategoryItem:
    id: str                      # e.g. "anal-9ft75"
    title: str                   # e.g. "Anal"
    slug: str                    # e.g. "anal-9ft75"
    url: str
    thumbnail: Optional[str] = None
    video_count: Optional[int] = None

class BaseProvider(ABC):
    BASE_URL: str = ""

    def set_address(self, address: str):
        """Configure the base address for the provider."""
        if address:
            self.BASE_URL = address.rstrip("/")

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider identifier."""

    @abstractmethod
    async def get_categories(self) -> List[CategoryItem]:
        """Fetch list of available categories."""

    @abstractmethod
    async def get_browse_videos(self, section: str = "trending", page: int = 1) -> List[VideoItem]:
        """Fetch videos from browse sections (trending, popular, latest)."""

    @abstractmethod
    async def get_category_videos(self, category_id: str, page: int = 1, sort: str = "latest") -> List[VideoItem]:
        """Fetch videos belonging to a category."""

    @abstractmethod
    async def search(self, query: str, page: int = 1) -> List[VideoItem]:
        """Search videos by query."""

    @abstractmethod
    async def resolve_stream(self, provider_video_id: str) -> str:
        """Resolve playback stream URL (e.g. HLS m3u8 URL)."""

    def video_page_url(self, provider_video_id: str, slug: str = "") -> Optional[str]:
        """Canonical video page URL for single-video enrichment, or None.

        Providers whose listing cards carry no performers implement this so
        sync can backfill performers one cheap request at a time (never in
        bulk loops). Default None = not supported.
        """
        return None
