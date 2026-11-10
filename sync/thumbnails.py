"""Thumbnail URL resolution, validation, caching, and download operations."""

import logging
import os
import tempfile
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# How long to skip re-downloading a thumbnail after a permanent failure (404).
THUMB_FAIL_TTL_SECONDS = int(os.getenv("THUMB_FAIL_TTL_SECONDS", str(7 * 86400)))


@dataclass(frozen=True)
class ThumbnailServices:
    """Dependencies supplied dynamically by the sync.py facade."""

    http_get: Callable
    logger: Any
    time: Any = _time
    fail_ttl_seconds: int = THUMB_FAIL_TTL_SECONDS


def _resolve_default_services(services: Optional[ThumbnailServices]) -> ThumbnailServices:
    if services is not None:
        return services
    from sync.http_client import http_get as _http_get
    return ThumbnailServices(
        http_get=_http_get,
        logger=logging.getLogger("sync.thumbnails"),
        time=_time,
        fail_ttl_seconds=THUMB_FAIL_TTL_SECONDS,
    )


def _thumb_fail_marker(dest_path: Path) -> Path:
    """Return the sentinel file path indicating recent download failure."""
    return dest_path.with_suffix(".thumb_fail")


def thumbnail_urls(raw_url: str, *,
                   resolver_public_url: str = "",
                   resolver_api_url: str = "") -> Tuple[str, str]:
    """Return the client-facing URL and the URL sync should download.

    Telegram previews are served by the resolver. Keep the public HTTPS URL
    in video metadata/NFO, but fetch the image over the internal Compose network.
    """
    url = (raw_url or "").strip()
    parsed = urlparse(url)
    if parsed.path == "/admin/api/thumb" and \
            parse_qs(parsed.query).get("provider") == ["telegram"] and \
            (url.startswith("/") or parsed.scheme in ("http", "https")):
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        pub = resolver_public_url.rstrip("/") if resolver_public_url else ""
        api = resolver_api_url.rstrip("/") if resolver_api_url else ""
        return pub + path, api + path
    return url, url


_BUMPER_HASHES_CACHE: Optional[Set[int]] = None
_BUMPER_HASHES_MTIME: float = 0.0


def _get_cached_bumper_hashes() -> Set[int]:
    global _BUMPER_HASHES_CACHE, _BUMPER_HASHES_MTIME
    cache_path_env = os.getenv("BUMPER_CACHE_FILE")
    cache_file = Path(cache_path_env) if cache_path_env else None
    if not cache_file or not cache_file.is_file():
        # Check default paths: data/cache/bumper_dhashes.json
        candidates = (
            Path("/data/cache/bumper_dhashes.json"),
            Path("data/cache/bumper_dhashes.json"),
            Path(__file__).resolve().parent.parent / "data" / "cache" / "bumper_dhashes.json",
        )
        cache_file = next((candidate for candidate in candidates if candidate.is_file()), None)
    if not cache_file or not cache_file.is_file():
        return set()

    try:
        mtime = cache_file.stat().st_mtime
        if _BUMPER_HASHES_CACHE is None or mtime > _BUMPER_HASHES_MTIME:
            from common.image_utils import load_bumper_hashes
            _BUMPER_HASHES_CACHE = load_bumper_hashes(cache_file)
            _BUMPER_HASHES_MTIME = mtime
        return _BUMPER_HASHES_CACHE or set()
    except Exception:
        logger.debug("Ignored exception in _get_cached_bumper_hashes", exc_info=True)
        return _BUMPER_HASHES_CACHE or set()


def known_bumper_hashes() -> Set[int]:
    """Return hashes for studio bumpers/logos detected during artwork audits."""
    return _get_cached_bumper_hashes()


def _tiny_telegram_preview_ok(data: bytes, is_tg: bool) -> bool:
    # Telegram can return a tiny JPEG preview (observed: 644 bytes).
    return (is_tg and len(data) >= 100
            and data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"))


def _is_too_small_image(data: bytes, minimum_size: Tuple[int, int]) -> bool:
    """True when the image is below the minimum size, or cannot be decoded."""
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            minimum_short, minimum_long = minimum_size
            if min(image.size) < minimum_short or max(image.size) < minimum_long:
                return True
    except Exception:
        logger.debug("Ignored exception in _valid_thumbnail_bytes", exc_info=True)
        return True
    return False


def _is_blank_or_bumper_image(data: bytes, bumper_hashes: Optional[Set[int]]) -> bool:
    """True for black frames and known studio bumpers/logos."""
    try:
        from common.image_utils import compute_dhash, is_black_image, is_bumper_hash
        if is_black_image(data):
            return True

        known_bumpers = bumper_hashes if bumper_hashes is not None else _get_cached_bumper_hashes()
        if known_bumpers:
            try:
                dh = compute_dhash(data)
                if is_bumper_hash(dh, known_bumpers, max_distance=4):
                    return True
            except Exception:
                logger.debug("Ignored exception in _valid_thumbnail_bytes", exc_info=True)
    except Exception:
        logger.debug("Ignored exception in _valid_thumbnail_bytes", exc_info=True)
    return False


def _valid_thumbnail_bytes(data: bytes, url: str, bumper_hashes: Optional[Set[int]] = None,
                           minimum_size: Optional[Tuple[int, int]] = None) -> bool:
    """Validate that image payload looks like a real preview and is not blank/black or a known bumper."""
    if not isinstance(data, (bytes, bytearray)):
        return False
    is_tg = urlparse(url).path == "/admin/api/thumb"
    if len(data) <= 1000 and not _tiny_telegram_preview_ok(data, is_tg):
        return False
    if minimum_size is not None and _is_too_small_image(data, minimum_size):
        return False
    if _is_blank_or_bumper_image(data, bumper_hashes):
        return False
    return True


def _reuse_existing_thumbnail(dest_path: Path, url: str) -> bool:
    """Return True when dest_path already holds a valid thumbnail (clearing any fail marker)."""
    if not dest_path.exists():
        return False
    try:
        existing = dest_path.read_bytes()
        if _valid_thumbnail_bytes(existing, url):
            try:
                _thumb_fail_marker(dest_path).unlink(missing_ok=True)
            except OSError:
                pass
            return True
    except OSError:
        pass
    return False


def _recently_failed(dest_path: Path, svc: ThumbnailServices) -> bool:
    """Negative cache: True while a recent permanent-failure marker exists."""
    try:
        marker = _thumb_fail_marker(dest_path)
        if marker.exists() and (svc.time.time() - marker.stat().st_mtime) < svc.fail_ttl_seconds:
            svc.logger.debug("Skipping thumbnail retry (recent permanent failure): %s", dest_path.name)
            return True
    except OSError:
        pass
    return False


def _persist_thumbnail(tmp_path: Path, dest_path: Path, data: bytes) -> None:
    with open(tmp_path, "wb") as f:
        f.write(data)
    os.replace(tmp_path, dest_path)
    try:
        _thumb_fail_marker(dest_path).unlink(missing_ok=True)
    except OSError:
        pass


def _log_download_failure(svc: ThumbnailServices, url: str, dest_path: Path, exc: Exception) -> None:
    if "404" in str(exc) or "Not Found" in str(exc):
        try:
            _thumb_fail_marker(dest_path).touch(exist_ok=True)
        except OSError:
            pass
        svc.logger.warning("Thumbnail permanently unavailable (404, will skip for 7d): %s -> %s", url, dest_path.name)
    else:
        svc.logger.warning("Could not download thumb %s -> %s: %s", url, dest_path.name, exc)


def _remove_partial_download(tmp_path: Path) -> None:
    if tmp_path.exists():
        try:
            tmp_path.unlink()
        except OSError:
            pass


def download_image(url: str, dest_path: Path, *,
                   services: Optional[ThumbnailServices] = None) -> None:
    """Download image to dest_path with negative caching on 404 permanent failures."""
    svc = _resolve_default_services(services)
    if _reuse_existing_thumbnail(dest_path, url):
        return
    if _recently_failed(dest_path, svc):
        return
    tmp_path = dest_path.with_suffix(".jpg.tmp")
    try:
        data = svc.http_get(url, timeout=15.0)
        if _valid_thumbnail_bytes(data, url):
            _persist_thumbnail(tmp_path, dest_path, data)
        else:
            svc.logger.warning("Downloaded thumbnail too small (%d bytes) for %s", len(data), url)
    except Exception as e:
        _remove_partial_download(tmp_path)
        _log_download_failure(svc, url, dest_path, e)



def download_image_if_missing(url: str, dest_path: Path, *,
                              services: Optional[ThumbnailServices] = None) -> bool:
    """Create an artwork sidecar only if absent; never replace an existing file."""
    svc = _resolve_default_services(services)
    if dest_path.is_symlink():
        return False
    if dest_path.exists():
        try:
            return dest_path.is_file() and _valid_thumbnail_bytes(dest_path.read_bytes(), url)
        except OSError:
            return False
    temporary = None
    try:
        data = svc.http_get(url, timeout=15.0)
        if not isinstance(data, bytes) or not _valid_thumbnail_bytes(data, url):
            return False
        fd, name = tempfile.mkstemp(prefix=".adult-hub-artwork-", suffix=".tmp", dir=dest_path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, dest_path, follow_symlinks=False)
            return True
        except FileExistsError:
            if dest_path.is_symlink():
                return False
            try:
                return dest_path.is_file() and _valid_thumbnail_bytes(dest_path.read_bytes(), url)
            except OSError:
                return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
