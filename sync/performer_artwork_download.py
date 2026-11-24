"""Download provider performer thumbnails into the local media tree.

The catalogue stores provider URLs (``https://stashdb.org/...`` and friends) in
``performer_photos.thumb``. The resolver only ever serves regular files that
live inside the media root, so a remote URL never reaches a client. This module
turns such a URL into a local JPEG under ``<media>/performers`` and rewrites the
row to that relative path, which is what the artwork route can serve.

Only https hosts on a fixed provider allowlist are fetched. Images are decoded,
rejected when black or undersized, downscaled, and re-encoded, so an unexpected
payload cannot be stored verbatim.
"""
from __future__ import annotations

import hashlib
import logging
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("sync.performer_artwork_download")

PERFORMER_ARTWORK_SUBDIR = "performers"
ALLOWED_ARTWORK_HOSTS = frozenset({
    "fansdb.cc",
    "gff.network",
    "pbc.wiki",
    "stashdb.org",
    "theporndb.net",
})
MAX_SOURCE_BYTES = 12_000_000
MIN_IMAGE_BYTES = 5_000
MAX_THUMB_EDGE = 720
JPEG_QUALITY = 90


def _host_allowed(host: str) -> bool:
    """Return True when the host is, or is a subdomain of, an allowlisted domain."""
    host = str(host or "").strip().casefold().rstrip(".")
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_ARTWORK_HOSTS)


def _source_url(thumb: Any) -> str:
    """Return the provider URL stored in the row when it is safe to fetch."""
    value = str(thumb or "").strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        return ""
    if not _host_allowed(parsed.hostname):
        return ""
    return value


def _slug(name: str) -> str:
    """Return a filesystem-safe, stable, human-readable performer slug."""
    cleaned = []
    for character in str(name or "").strip().casefold():
        if character.isalnum():
            cleaned.append(character)
        elif character in " -_.":
            cleaned.append("-")
    base = "-".join(part for part in "".join(cleaned).split("-") if part)[:80] or "performer"
    digest = hashlib.sha256(str(name or "").strip().casefold().encode("utf-8")).hexdigest()[:10]
    return f"{base}-{digest}"


def artwork_target(media_dir: Path, name: str) -> Path:
    """Return the absolute JPEG path for a performer thumbnail."""
    return (Path(media_dir) / PERFORMER_ARTWORK_SUBDIR / f"{_slug(name)}.jpg").resolve()


def stored_thumb_path(media_dir: Path, name: str) -> str:
    """Return the relative path recorded in ``performer_photos.thumb``."""
    target = artwork_target(media_dir, name)
    return target.relative_to(Path(media_dir).resolve()).as_posix()


def _encode_thumbnail(payload: bytes, *, min_bytes: int) -> bytes | None:
    """Validate a downloaded image payload and return a bounded JPEG."""
    from PIL import Image

    from common.image_utils import is_black_image

    if len(payload) < min_bytes:
        return None
    try:
        with Image.open(BytesIO(payload)) as downloaded:
            downloaded.load()
            if downloaded.width < 32 or downloaded.height < 32:
                return None
            if is_black_image(downloaded):
                return None
            thumbnail = downloaded.convert("RGB")
            thumbnail.thumbnail((MAX_THUMB_EDGE, MAX_THUMB_EDGE))
            output = BytesIO()
            thumbnail.save(output, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except Exception as exc:
        logger.debug("Rejected performer thumbnail payload (%s)", type(exc).__name__)
        return None
    encoded = output.getvalue()
    return encoded if len(encoded) >= min_bytes else None


def _fetch_bytes(url: str, http_client: Any) -> bytes | None:
    """Fetch the image body with the shared host rate limit and a size guard."""
    from common.http_rate_limit import request_with_host_limit

    client = http_client
    if client is None:
        import httpx

        client = httpx
    try:
        response = request_with_host_limit(
            client, "get", url, timeout=15.0, follow_redirects=True,
        )
    except Exception as exc:
        logger.warning("Performer artwork request failed (%s)", type(exc).__name__)
        return None
    if getattr(response, "status_code", 0) != 200:
        logger.warning("Performer artwork request returned HTTP %s", getattr(response, "status_code", "?"))
        return None
    final_host = getattr(getattr(response, "url", None), "host", "") or ""
    if final_host and not _host_allowed(final_host):
        logger.warning("Performer artwork redirected to a host outside the allowlist")
        return None
    declared = 0
    headers = getattr(response, "headers", None) or {}
    try:
        declared = int(headers.get("content-length") or 0)
    except (TypeError, ValueError):
        declared = 0
    if declared > MAX_SOURCE_BYTES:
        logger.warning("Performer artwork response too large (%s bytes)", declared)
        return None
    content = getattr(response, "content", b"") or b""
    if len(content) > MAX_SOURCE_BYTES:
        logger.warning("Performer artwork body too large (%s bytes)", len(content))
        return None
    return content


def _write_thumbnail(target: Path, payload: bytes) -> bool:
    """Write the JPEG beside the media tree without following symlinks."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            return False
        temporary = target.with_suffix(".jpg.part")
        temporary.write_bytes(payload)
        temporary.replace(target)
        return True
    except OSError as exc:
        logger.warning("Could not store performer artwork %s (%s)", target.name, type(exc).__name__)
        return False


def download_performer_artwork(conn, media_dir: Path, name: str, *,
                               http_client: Any = None,
                               min_bytes: int = MIN_IMAGE_BYTES) -> dict:
    """Materialise one performer thumbnail and record its local path.

    States: ``artwork_downloaded``, ``artwork_already_local``,
    ``artwork_download_failed`` and ``artwork_not_found`` (no fetchable URL).
    """
    name = str(name or "").strip()
    if not name:
        raise ValueError("A performer name is required.")
    row = conn.execute(
        "SELECT name, thumb FROM performer_photos "
        "WHERE lower(name) = lower(%s) ORDER BY name LIMIT 1",
        (name,),
    ).fetchone()
    if row is None:
        return {"local_state": "artwork_not_found", "reason": "no_photo_row"}
    stored_name, thumb = str(row[0]), str(row[1] or "").strip()

    target = artwork_target(media_dir, stored_name)
    if not thumb.casefold().startswith(("http://", "https://")):
        if thumb and target.is_file() and not target.is_symlink() and target.stat().st_size > 0:
            return {"local_state": "artwork_already_local", "path": thumb}
        return {"local_state": "artwork_not_found",
                "reason": "local_missing" if thumb else "no_remote_url"}

    url = _source_url(thumb)
    if not url:
        return {"local_state": "artwork_not_found", "reason": "host_not_allowed"}

    payload = _fetch_bytes(url, http_client)
    encoded = _encode_thumbnail(payload, min_bytes=min_bytes) if payload else None
    if not encoded:
        return {"local_state": "artwork_download_failed", "reason": "invalid_image"}

    if not _write_thumbnail(target, encoded):
        return {"local_state": "artwork_download_failed", "reason": "write_failed"}
    relative = stored_thumb_path(media_dir, stored_name)
    with conn:
        conn.execute(
            "UPDATE performer_photos SET thumb=%s, updated_at=now() "
            "WHERE name=%s",
            (relative, stored_name),
        )
    return {"local_state": "artwork_downloaded", "path": relative, "bytes": len(encoded)}
