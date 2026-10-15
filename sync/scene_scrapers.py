"""Lightweight scene metadata scrapers for Adult Hub.

Directly queries official public JSON/Algolia endpoints used by major studios
(Falcon Studios Group, NextDoor Studios, Men.com, Sean Cody) without requiring
a separate Stash instance.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time  # noqa: F401  # Backfill helpers resolve time through this compatibility module.
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import urlparse  # noqa: F401
from common.http_rate_limit import request_with_host_limit

try:
    from sync.scene_scraper_models import ScrapedScene, TelegramOshashResult
    from sync.scene_scraper_engines import (
        AlgoliaScraper, BelAmiScraper, HelixScraper, LucasScraper, Project1Scraper,
    )
except ModuleNotFoundError:
    from scene_scraper_models import ScrapedScene, TelegramOshashResult
    from scene_scraper_engines import (
        AlgoliaScraper, BelAmiScraper, HelixScraper, LucasScraper, Project1Scraper,
    )

try:
    import httpx
except ImportError:
    httpx = None

try:
    from common import db
    from common.external_metadata import record_attempt
except ImportError:
    try:
        import db  # type: ignore
    except ImportError:
        db = None
    record_attempt = None

logger = logging.getLogger("scene_scrapers")

DEFAULT_TIMEOUT = 12.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
GAMMA_IMAGE_CDN = "https://images03-fame.gammacdn.com"
TELEGRAM_RANGE_BUDGET = 8 * 1024 * 1024
TELEGRAM_PHASH_BYTE_BUDGET = 32 * 1024 * 1024
TELEGRAM_HASH_BLOCK = 64 * 1024



def _clean_query(text: str, studio: str = "") -> str:
    """Compatibility facade for scene title normalization."""
    if __package__:
        from .scene_scraper_matching import _clean_query as implementation
    else:
        from scene_scraper_matching import _clean_query as implementation
    return implementation(text, studio)


def _calculate_match_confidence(
    query_title: str,
    target_title: str,
    query_duration: int = 0,
    target_duration: int = 0,
    query_performers: Optional[List[str]] = None,
    target_performers: Optional[List[str]] = None,
) -> float:
    """Compatibility facade for scene match confidence scoring."""
    if __package__:
        from .scene_scraper_matching import _calculate_match_confidence as implementation
    else:
        from scene_scraper_matching import _calculate_match_confidence as implementation
    return implementation(
        query_title, target_title, query_duration, target_duration,
        query_performers, target_performers,
    )


# ==============================================================================
# 6. Studio Router & Matcher
# ==============================================================================

STUDIO_ROUTING = [
    # Falcon Studios Group
    (re.compile(r"\b(falcon|raging\s*stallion|hot\s*house|naked\s*sword)\b", re.I), "algolia", "falconstudios", "Falcon Studios"),
    # NextDoor Studios
    (re.compile(r"\b(next\s*door|nextdoor|nde)\b", re.I), "algolia", "nextdoorstudios", "NextDoor Studios"),
    # Sean Cody
    (re.compile(r"\b(sean\s*cody|seancody)\b", re.I), "project1", "seancody", "Sean Cody"),
    # Men.com
    (re.compile(r"\b(men\.com|mencom)\b", re.I), "project1", "men", "Men.com"),
    # Helix Studios
    (re.compile(r"\b(helix\s*studios?|helix)\b", re.I), "helix", "helixstudios", "Helix Studios"),
    # BelAmi
    (re.compile(r"\b(belami|bel\s*ami|belamionline)\b", re.I), "belami", "belami", "BelAmi"),
    # Lucas Entertainment
    (re.compile(r"\b(lucas\s*entertainment|lucas\s*men|lucas)\b", re.I), "lucas", "lucas", "Lucas Entertainment"),
]


def resolve_studio_engine(studio: str = "", title: str = "") -> Optional[Tuple[str, str, str]]:
    """Determine (engine, engine_key, canonical_studio) from title or studio name/tag."""
    # 1. Check title first (e.g. Next Door or Sean Cody in title on aggregator channels)
    if title:
        t = str(title).strip()
        for pattern, engine, key, canonical in STUDIO_ROUTING:
            if pattern.search(t):
                return engine, key, canonical

    # 2. Check studio name
    if studio:
        s = str(studio).strip()
        for pattern, engine, key, canonical in STUDIO_ROUTING:
            if pattern.search(s):
                return engine, key, canonical

    return None


def search_scene(
    title: str,
    studio: str = "",
    duration: int = 0,
    performers: Optional[List[str]] = None,
    min_confidence: float = 0.65,
    http_client: Optional[Any] = None,
) -> Optional[ScrapedScene]:
    """Search official studio APIs, then TPDB as a catalog-wide fallback."""
    resolved = resolve_studio_engine(studio, title=title)
    engine, engine_key, canonical_studio = resolved or ("", "", studio or "")
    clean_title = _clean_query(title, studio=canonical_studio if resolved else studio)
    if not clean_title:
        return None

    # Try searching by clean title
    hits = []
    if engine == "algolia":
        hits = AlgoliaScraper.search(engine_key, clean_title, limit=6, http_client=http_client)
        if not hits and performers:
            p_query = " ".join(performers[:2])
            hits = AlgoliaScraper.search(engine_key, p_query, limit=6, http_client=http_client)
    elif engine == "project1":
        hits = Project1Scraper.search(engine_key, clean_title, limit=6, http_client=http_client)
        if not hits and performers:
            p_query = " ".join(performers[:2])
            hits = Project1Scraper.search(engine_key, p_query, limit=6, http_client=http_client)
    elif engine == "helix":
        hits = HelixScraper.search(clean_title, limit=4, http_client=http_client)
        if not hits and performers:
            p_query = " ".join(performers[:2])
            hits = HelixScraper.search(p_query, limit=4, http_client=http_client)
    elif engine == "belami":
        hits = BelAmiScraper.search(clean_title, limit=4, http_client=http_client)
        if not hits and performers:
            p_query = " ".join(performers[:2])
            hits = BelAmiScraper.search(p_query, limit=4, http_client=http_client)
    elif engine == "lucas":
        hits = LucasScraper.search(clean_title, limit=4, http_client=http_client)
        if not hits and performers:
            p_query = " ".join(performers[:2])
            hits = LucasScraper.search(p_query, limit=4, http_client=http_client)

    if not hits:
        return _search_tpdb_scene(
            title=title, clean_title=clean_title, studio=studio,
            duration=duration, performers=performers or [],
            min_confidence=max(min_confidence, 0.78), http_client=http_client,
        )

    best_match: Optional[ScrapedScene] = None
    best_confidence = 0.0

    for hit in hits:
        if engine == "algolia":
            candidate = AlgoliaScraper.to_scraped_scene(hit, default_studio=canonical_studio)
        elif engine == "project1":
            candidate = Project1Scraper.to_scraped_scene(hit, default_studio=canonical_studio)
        elif engine == "helix":
            candidate = HelixScraper.to_scraped_scene(hit, default_studio=canonical_studio, http_client=http_client)
        elif engine == "belami":
            candidate = BelAmiScraper.to_scraped_scene(hit, default_studio=canonical_studio, http_client=http_client)
        elif engine == "lucas":
            candidate = LucasScraper.to_scraped_scene(hit, default_studio=canonical_studio, http_client=http_client)
        else:
            continue

        conf = _calculate_match_confidence(
            query_title=title,
            target_title=candidate.title,
            query_duration=duration,
            target_duration=candidate.duration,
            query_performers=performers,
            target_performers=candidate.performers,
        )

        candidate.confidence = conf
        if conf > best_confidence:
            best_confidence = conf
            best_match = candidate

    if best_match and best_confidence >= min_confidence:
        best_match.source = f"studio:{engine}"
        logger.info(
            "Matched scene for %r [%s]: %r (confidence: %.2f)",
            title, canonical_studio, best_match.title, best_confidence,
        )
        return best_match

    return _search_tpdb_scene(
        title=title, clean_title=clean_title, studio=studio,
        duration=duration, performers=performers or [],
        min_confidence=max(min_confidence, 0.78), http_client=http_client,
    )


def _search_tpdb_scene(
    title: str,
    clean_title: str,
    studio: str,
    duration: int,
    performers: List[str],
    min_confidence: float,
    http_client: Optional[Any] = None,
) -> Optional[ScrapedScene]:
    """Search ThePornDB's scene catalog for titles outside supported studio APIs.

    TPDB requests are opt-in via the existing API token. Exact source records are
    still scored against local title, duration, and cast before being accepted.
    """
    token = os.environ.get("THEPORNDB_API_TOKEN", "").strip() or os.environ.get("THEPORNDB_API_KEY", "").strip()
    if not token:
        return None
    client = http_client or httpx
    if client is None:
        return None

    response = request_with_host_limit(
        client, "get",
        "https://api.theporndb.net/scenes",
        params={"q": clean_title, "per_page": 10, "page": 1},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": USER_AGENT},
        timeout=DEFAULT_TIMEOUT,
    )
    if callable(getattr(response, "raise_for_status", None)):
        response.raise_for_status()
    payload = response.json() if callable(getattr(response, "json", None)) else _decode_scene_payload(response)
    if not isinstance(payload, dict):
        raise ValueError("TPDB scene search returned an invalid response")
    results = payload.get("data")
    if not isinstance(results, list):
        raise ValueError("TPDB scene search response has no result list")

    best: Optional[ScrapedScene] = None
    best_confidence = 0.0
    for item in results:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        site = item.get("site") if isinstance(item.get("site"), dict) else {}
        cast = item.get("performers") if isinstance(item.get("performers"), list) else []
        names = [str(p.get("name") or "").strip() for p in cast if isinstance(p, dict) and p.get("name")]
        image = item.get("poster_image") or item.get("image") or item.get("poster") or ""
        candidate = ScrapedScene(
            title=str(item.get("title") or "").strip(),
            plot=str(item.get("description") or "").strip(),
            date=str(item.get("date") or "")[:10],
            duration=int(item.get("duration") or 0),
            poster_url=str(image).strip(),
            performers=names,
            studio=str(site.get("name") or studio or "").strip(),
            external_id=str(item.get("id") or item.get("_id") or ""),
            url=str(item.get("url") or "").strip(),
        )
        confidence = _calculate_match_confidence(
            title, candidate.title, duration, candidate.duration, performers, names,
        )
        candidate.confidence = confidence
        if confidence > best_confidence:
            best, best_confidence = candidate, confidence
    if best and best_confidence >= min_confidence:
        best.source = "theporndb"
        logger.info("Matched TPDB scene for %r: %r (confidence: %.2f)", title, best.title, best_confidence)
        return best
    return None


def _decode_scene_payload(response: Any) -> Optional[dict]:
    try:
        text = getattr(response, "text", None)
        return json.loads(text) if isinstance(text, str) else None
    except Exception:
        return None


def _probe_telegram_hash_ranges(head: bytes, tail: bytes, file_size: int, tail_start: int) -> dict:
    """Compatibility facade for probing facts from OSHASH byte ranges."""
    if __package__:
        from .scene_scraper_oshash import probe_telegram_hash_ranges as implementation
    else:
        from scene_scraper_oshash import probe_telegram_hash_ranges as implementation
    return implementation(head, tail, file_size, tail_start)


def _telegram_oshash(video_id: str, *, http_client: Optional[Any] = None) -> TelegramOshashResult:
    """Calculate Stash-compatible OSHASH from two bounded Telegram byte ranges.

    The resolver proxies Telegram Range requests. We reject servers that do not
    return an exact 206 range, so a missing range implementation cannot turn
    this operation into a full video download. The current algorithm uses at
    most 128 KiB; ffprobe examines a sparse local file containing those same
    ranges, so media metadata adds no source transfer. The larger per-video
    budget remains a hard ceiling for future frame hash fallbacks.
    """
    if __package__:
        from .scene_scraper_oshash import telegram_oshash as implementation
    else:
        from scene_scraper_oshash import telegram_oshash as implementation
    return implementation(video_id, http_client=http_client)


def _fingerprint_source_module():
    try:
        from . import scene_scraper_fingerprint_sources as implementation
    except ImportError:
        import scene_scraper_fingerprint_sources as implementation
    return implementation


def _search_tpdb_scene_by_oshash(
    fingerprint: str, *, http_client: Optional[Any] = None,
) -> Optional[ScrapedScene]:
    return _fingerprint_source_module().search_tpdb_scene_by_oshash(
        fingerprint, http_client=http_client
    )


def _search_stashdb_scene_by_oshash(
    fingerprint: str, *, http_client: Optional[Any] = None,
) -> Optional[ScrapedScene]:
    return _fingerprint_source_module().search_stashdb_scene_by_oshash(
        fingerprint, http_client=http_client
    )


def _search_stashdb_scene_by_fingerprint(
    fingerprint: str, algorithm: str, *, http_client: Optional[Any] = None,
    max_distance: int = 0, duration: Optional[int] = None,
    duration_tolerance: int = 2,
) -> Optional[ScrapedScene]:
    return _fingerprint_source_module().search_stashdb_scene_by_fingerprint(
        fingerprint, algorithm, http_client=http_client, max_distance=max_distance,
        duration=duration, duration_tolerance=duration_tolerance,
    )

def search_scene_by_oshash(fingerprint: str, *, http_client: Optional[Any] = None) -> Optional[ScrapedScene]:
    """Try exact OSHASH lookups; avoid falling back to numeric-title search."""
    if not re.fullmatch(r"[0-9a-fA-F]{16}", str(fingerprint or "")):
        return None
    return (_search_tpdb_scene_by_oshash(fingerprint.lower(), http_client=http_client)
            or _search_stashdb_scene_by_oshash(fingerprint.lower(), http_client=http_client))


def search_scene_by_phash(
    fingerprint: str, *, http_client: Optional[Any] = None,
    duration: Optional[int] = None, max_distance: int = 10,
    duration_tolerance: int = 2,
) -> Optional[ScrapedScene]:
    """Compatibility facade for the fingerprint catalogue lookup module."""
    return _fingerprint_source_module().search_scene_by_phash(
        fingerprint, http_client=http_client, duration=duration,
        max_distance=max_distance, duration_tolerance=duration_tolerance,
    )

def _scene_match_provenance(scene: ScrapedScene) -> Tuple[str, str, str, float]:
    try:
        from .scene_scraper_backfill import _scene_match_provenance as implementation
    except ImportError:
        from scene_scraper_backfill import _scene_match_provenance as implementation
    return implementation(scene)


def _cached_video_fingerprint(conn: Any, video_id: str, algorithm: str) -> Optional[str]:
    try:
        from .scene_scraper_backfill import _cached_video_fingerprint as implementation
    except ImportError:
        from scene_scraper_backfill import _cached_video_fingerprint as implementation
    return implementation(conn, video_id, algorithm)


def _store_video_fingerprint(conn: Any, video_id: str, algorithm: str, fingerprint: str, *, media_probe: Optional[dict]=None) -> None:
    try:
        from .scene_scraper_backfill import _store_video_fingerprint as implementation
    except ImportError:
        from scene_scraper_backfill import _store_video_fingerprint as implementation
    return implementation(conn, video_id, algorithm, fingerprint, media_probe=media_probe)


def backfill_scenes(conn: Any, media_dir: Path, limit: int=50, sleep_sec: float=0.5, http_client: Optional[Any]=None) -> Tuple[int, int]:
    try:
        from .scene_scraper_backfill import backfill_scenes as implementation
    except ImportError:
        from scene_scraper_backfill import backfill_scenes as implementation
    return implementation(conn, media_dir, limit, sleep_sec, http_client)


def _apply_matched_scene(conn: Any, media_dir: Path, video_id: str, matched: ScrapedScene,
                         http_client: Optional[Any] = None, now: Optional[int] = None) -> bool:
    """Persist a scene match without replacing non-empty catalog metadata."""
    if __package__:
        from .scene_scraper_backfill import _apply_matched_scene as implementation
    else:
        from scene_scraper_backfill import _apply_matched_scene as implementation
    return implementation(conn, media_dir, video_id, matched, http_client, now)


def _telegram_oshash_batch_limit() -> int:
    try:
        from .scene_scraper_backfill import _telegram_oshash_batch_limit as implementation
    except ImportError:
        from scene_scraper_backfill import _telegram_oshash_batch_limit as implementation
    return implementation()


def backfill_missing_telegram_oshashes(conn: Any, limit: int=200, sleep_sec: float=0.1, http_client: Optional[Any]=None) -> Tuple[int, int]:
    try:
        from .scene_scraper_backfill import backfill_missing_telegram_oshashes as implementation
    except ImportError:
        from scene_scraper_backfill import backfill_missing_telegram_oshashes as implementation
    return implementation(conn, limit, sleep_sec, http_client)


def _fingerprint_feature_enabled(name: str, legacy_name: str, *, default: bool=False) -> bool:
    try:
        from .scene_scraper_backfill import _fingerprint_feature_enabled as implementation
    except ImportError:
        from scene_scraper_backfill import _fingerprint_feature_enabled as implementation
    return implementation(name, legacy_name, default=default)


def backfill_telegram_fingerprints(conn: Any, media_dir: Path, limit: int=20, sleep_sec: float=0.5, http_client: Optional[Any]=None, video_ids: Optional[List[str]]=None, channel_id: Optional[str]=None) -> Tuple[int, int]:
    try:
        from .scene_scraper_backfill import backfill_telegram_fingerprints as implementation
    except ImportError:
        from scene_scraper_backfill import backfill_telegram_fingerprints as implementation
    return implementation(conn, media_dir, limit, sleep_sec, http_client, video_ids, channel_id)


def _update_media_files(media_dir: Path, video_id: str, matched: ScrapedScene, performers: List[str], http_client: Optional[Any]=None, conn: Optional[Any]=None) -> None:
    try:
        from .scene_scraper_backfill import _update_media_files as implementation
    except ImportError:
        from scene_scraper_backfill import _update_media_files as implementation
    return implementation(media_dir, video_id, matched, performers, http_client, conn)
