"""Thumbnail URL resolution, validation, caching, and download operations."""

import logging
import os
import tempfile
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Tuple
from urllib.parse import parse_qs, urlparse

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
    try:
        from http_client import http_get as _http_get
    except ImportError:
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


def _valid_thumbnail_bytes(data: bytes, url: str) -> bool:
    """Validate that image payload looks like a real preview and is not blank/black."""
    if not isinstance(data, (bytes, bytearray)):
        return False
    is_tg = urlparse(url).path == "/admin/api/thumb"
    if len(data) <= 1000:
        # Telegram can return a tiny JPEG preview (observed: 644 bytes).
        if not (is_tg and len(data) >= 100
                and data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9")):
            return False
    try:
        try:
            from common.image_utils import is_black_image
        except ImportError:
            from sync.image_utils import is_black_image  # fallback
        if is_black_image(data):
            return False
    except Exception:
        pass
    return True


def download_image(url: str, dest_path: Path, *,
                   services: Optional[ThumbnailServices] = None) -> None:
    """Download image to dest_path with negative caching on 404 permanent failures."""
    svc = _resolve_default_services(services)
    if dest_path.exists():
        try:
            existing = dest_path.read_bytes()
            if _valid_thumbnail_bytes(existing, url):
                try:
                    _thumb_fail_marker(dest_path).unlink(missing_ok=True)
                except OSError:
                    pass
                return
        except OSError:
            pass
    # Negative cache: skip retry while a recent permanent-failure marker exists.
    try:
        marker = _thumb_fail_marker(dest_path)
        if marker.exists() and (svc.time.time() - marker.stat().st_mtime) < svc.fail_ttl_seconds:
            svc.logger.debug(f"Skipping thumbnail retry (recent permanent failure): {dest_path.name}")
            return
    except OSError:
        pass
    tmp_path = dest_path.with_suffix(".jpg.tmp")
    try:
        data = svc.http_get(url, timeout=15.0)
        if _valid_thumbnail_bytes(data, url):
            with open(tmp_path, "wb") as f:
                f.write(data)
            os.replace(tmp_path, dest_path)
            try:
                _thumb_fail_marker(dest_path).unlink(missing_ok=True)
            except OSError:
                pass
        else:
            svc.logger.warning(f"Downloaded thumbnail too small ({len(data)} bytes) for {url}")
    except Exception as e:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        if "404" in str(e) or "Not Found" in str(e):
            try:
                _thumb_fail_marker(dest_path).touch(exist_ok=True)
            except OSError:
                pass
            svc.logger.warning(f"Thumbnail permanently unavailable (404, will skip for 7d): {url} -> {dest_path.name}")
        else:
            svc.logger.warning(f"Could not download thumb {url} -> {dest_path.name}: {e}")


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
