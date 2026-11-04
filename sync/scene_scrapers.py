"""Lightweight scene metadata helpers for Adult Hub.

Scene metadata is resolved from conservative text and fingerprint matches
(exact-title lookups, OSHASH and pHash) against public catalogues.
"""
from __future__ import annotations

import json
import inspect
import logging
import os  # noqa: F401  # Compatibility re-export used by the scene source modules.
import re
import time  # noqa: F401  # Backfill helpers resolve time through this compatibility module.
from pathlib import Path
from typing import Any, List, Optional, Tuple
from urllib.parse import urlparse  # noqa: F401
from common.http_rate_limit import request_with_host_limit  # noqa: F401  # Compatibility re-export.

try:
    from sync.scene_scraper_models import ScrapedScene, TelegramOshashResult
except ModuleNotFoundError:
    from scene_scraper_models import ScrapedScene, TelegramOshashResult

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
TELEGRAM_RANGE_BUDGET = 8 * 1024 * 1024
TELEGRAM_PHASH_BYTE_BUDGET = 256 * 1024 * 1024
TELEGRAM_HASH_BLOCK = 64 * 1024



def _decode_scene_payload(response: Any) -> Optional[dict]:
    try:
        text = getattr(response, "text", None)
        return json.loads(text) if isinstance(text, str) else None
    except Exception as _exc:
        logger.debug("Ignored exception in %s", "_decode_scene_payload", exc_info=True)
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
    duration: Optional[int] = None, duration_tolerance: int = 2,
) -> Optional[ScrapedScene]:
    return _fingerprint_source_module().search_tpdb_scene_by_oshash(
        fingerprint, http_client=http_client, duration=duration,
        duration_tolerance=duration_tolerance,
    )


def _search_stashdb_scene_by_oshash(
    fingerprint: str, *, http_client: Optional[Any] = None,
    duration: Optional[int] = None, duration_tolerance: int = 2,
) -> Optional[ScrapedScene]:
    return _fingerprint_source_module().search_stashdb_scene_by_oshash(
        fingerprint, http_client=http_client, duration=duration,
        duration_tolerance=duration_tolerance,
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

def search_scene_by_oshash(
    fingerprint: str, *, http_client: Optional[Any] = None,
    duration: Optional[int] = None, duration_tolerance: int = 2,
) -> Optional[ScrapedScene]:
    """Try exact, duration-compatible OSHASH lookups in catalog priority order."""
    if not re.fullmatch(r"[0-9a-fA-F]{16}", str(fingerprint or "")):
        return None
    errors = []
    lookups = (
        ("StashDB", _search_stashdb_scene_by_oshash),
        ("ThePornDB", _search_tpdb_scene_by_oshash),
    )
    for source_name, lookup in lookups:
        try:
            scene = lookup(
                fingerprint.lower(), http_client=http_client, duration=duration,
                duration_tolerance=duration_tolerance,
            )
        except Exception as exc:
            errors.append(exc)
            logger.warning("%s OSHASH lookup failed (%s)", source_name, type(exc).__name__)
            continue
        if scene is not None:
            return scene
    if errors:
        raise errors[0]
    return None


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


def backfill_telegram_fingerprints(conn: Any, media_dir: Path, limit: int=20, sleep_sec: float=0.5, http_client: Optional[Any]=None, video_ids: Optional[List[str]]=None, channel_id: Optional[str]=None, progress_callback: Optional[Any]=None) -> Tuple[int, int]:
    try:
        from .scene_scraper_backfill import backfill_telegram_fingerprints as implementation
    except ImportError:
        from scene_scraper_backfill import backfill_telegram_fingerprints as implementation
    kwargs = {
        "limit": limit,
        "sleep_sec": sleep_sec,
        "http_client": http_client,
        "video_ids": video_ids,
        "channel_id": channel_id,
    }
    if "progress_callback" in inspect.signature(implementation).parameters:
        kwargs["progress_callback"] = progress_callback
    return implementation(conn, media_dir, **kwargs)


def _update_media_files(media_dir: Path, video_id: str, matched: ScrapedScene, performers: List[str], http_client: Optional[Any]=None, conn: Optional[Any]=None, strict: bool=False) -> None:
    try:
        from .scene_scraper_backfill import _update_media_files as implementation
    except ImportError:
        from scene_scraper_backfill import _update_media_files as implementation
    return implementation(media_dir, video_id, matched, performers, http_client, conn, strict)
