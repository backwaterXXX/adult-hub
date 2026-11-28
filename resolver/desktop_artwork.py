"""Artwork validation, local-file lookup, and response helpers for the client API."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Any

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse

from common import db
from common.client_auth.service import BoundedValidationError
from resolver import artwork_derivatives

logger = logging.getLogger("adult_hub.desktop_artwork")


_ALLOWED_ARTWORK_ENTITIES = {"video", "performer", "channel"}
_ALLOWED_VIDEO_KINDS = {"poster", "backdrop", "thumb", "primary"}
_ALLOWED_PERFORMER_KINDS = {"photo", "portrait", "thumb", "default", "primary"}
_ALLOWED_CHANNEL_KINDS = {"icon", "thumb", "default", "primary"}


def _artwork_error_response(
    title: str,
    status_code: int,
    detail: str,
    code: str,
) -> JSONResponse:
    """Return RFC 7807 problem details response for artwork errors with strict private no-store caching."""
    return JSONResponse(
        content={
            "title": title,
            "status": status_code,
            "detail": detail,
            "error": detail,
            "code": code,
        },
        status_code=status_code,
        headers={"Cache-Control": "private, no-store"},
        media_type="application/problem+json",
    )


def _reject_unsafe_artwork_id(clean: str) -> None:
    """Reject artwork identifiers that are oversized, contain NUL bytes, traverse paths, or name URLs."""
    if len(clean) > 500:
        raise BoundedValidationError("id exceeds maximum length of 500")
    if "\x00" in clean:
        raise BoundedValidationError("NUL bytes not permitted in identifier")
    if ".." in clean or clean.startswith("/") or clean.startswith("\\") or "\\" in clean:
        raise BoundedValidationError("Path traversal sequences not permitted in identifier")
    lower = clean.lower()
    if lower.startswith("file:") or lower.startswith("http:") or lower.startswith("https:"):
        raise BoundedValidationError("External or filesystem URLs not permitted in identifier")


def _check_artwork_id_format(clean: str, entity_type: str) -> None:
    """Check that an artwork identifier uses the character set allowed for its entity type."""
    if entity_type == "video":
        if not re.fullmatch(r"[a-zA-Z0-9_\-\.:]+", clean):
            raise BoundedValidationError("Invalid video identifier format")
    elif entity_type == "channel":
        if not re.fullmatch(r"[a-zA-Z0-9_\-\.:@]+", clean):
            raise BoundedValidationError("Invalid channel identifier format")
    elif entity_type == "performer":
        if not re.fullmatch(r"[a-zA-Z0-9_\-\.: %]+", clean):
            raise BoundedValidationError("Invalid performer identifier format")


def _validate_artwork_id(raw_id: str, entity_type: str) -> str:
    """Validate and sanitize an artwork resource identifier, preventing path traversal."""
    if not raw_id or not isinstance(raw_id, str):
        raise BoundedValidationError("id is required")
    clean = raw_id.strip()
    if not clean:
        raise BoundedValidationError("id is required")
    _reject_unsafe_artwork_id(clean)
    _check_artwork_id_format(clean, entity_type)
    return clean


def _detect_image_media_type(data: bytes) -> str:
    """Identify safe image MIME type from binary signatures."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _validate_artwork_width(width: int | None) -> int | None:
    """Reject widths outside the published derivative ladder."""
    if width is None:
        return None
    if width not in artwork_derivatives.ALLOWED_DERIVATIVE_WIDTHS:
        allowed = ", ".join(str(value) for value in artwork_derivatives.ALLOWED_DERIVATIVE_WIDTHS)
        raise BoundedValidationError(f"Invalid artwork width. Allowed widths: {allowed}.")
    return width


_BLACK_CHECK_CACHE: dict[tuple[str, int, int], bool] = {}
_BLACK_CHECK_LOCK = threading.Lock()


def _is_black_artwork_cached(path: Path) -> bool:
    """Black-frame check memoised by file identity.

    The check decodes the whole image with Pillow; doing that on every artwork
    request was pure overhead because the file changes far less often than the
    client asks for it.
    """
    try:
        stat_result = path.stat()
    except OSError:
        return False
    key = (str(path), stat_result.st_mtime_ns, stat_result.st_size)
    with _BLACK_CHECK_LOCK:
        cached = _BLACK_CHECK_CACHE.get(key)
    if cached is not None:
        return cached
    from common.image_utils import is_black_image

    result = bool(is_black_image(path))
    with _BLACK_CHECK_LOCK:
        if len(_BLACK_CHECK_CACHE) > 4096:
            _BLACK_CHECK_CACHE.clear()
        _BLACK_CHECK_CACHE[key] = result
    return result


def _local_video_artwork(db_url: str | None, cand: str, media_root: Path) -> bytes | None:
    """Read artwork stored next to a video's .strm file when it is a regular file inside the media root."""
    from resolver.admin_catalog_queries import _get_video_artwork_path

    art_path = _get_video_artwork_path(db_url, cand, media_root)
    if art_path is not None and art_path.is_file():
        res_path = art_path.resolve()
        if res_path.is_relative_to(media_root) and not stat.S_ISLNK(art_path.lstat().st_mode):
            content = res_path.read_bytes()
            if content and not _is_black_artwork_cached(res_path):
                return content
    return None


def _telegram_thumb_ref(conn: db.Connection, cand: str) -> str | None:
    """Return the Telegram provider reference for a video candidate, from its ID prefix or its active source row."""
    ref = cand.removeprefix("telegram:") if cand.startswith("telegram:") else None
    if not ref:
        tables = db.table_names(conn)
        if "video_sources" in tables:
            src_row = conn.execute(
                "SELECT provider_ref FROM video_sources WHERE video_id = %s AND provider = 'telegram' AND is_active = 1 LIMIT 1",
                (cand,),
            ).fetchone()
            if src_row and src_row[0]:
                ref = str(src_row[0])
    return ref


def _telegram_cached_thumb(ref: str) -> bytes | None:
    """Return the in-memory Telegram thumbnail for a provider reference when it is fresh and not black."""
    from resolver.providers import get_provider
    from common.image_utils import is_black_image

    try:
        tg_prov = get_provider("telegram")
        cached = getattr(tg_prov, "_thumb_cache", {}).get(ref)
        if cached is not None:
            expires, data = cached
            if data and expires > time.monotonic():
                if not is_black_image(data):
                    return bytes(data)
    except KeyError:
        pass
    return None


def _resolve_video_artwork(conn: db.Connection, clean_id: str, db_url: str | None, media_root: Path) -> bytes | None:
    """Find artwork for a video or one of its alias IDs, trying local files before the Telegram thumbnail cache."""
    from resolver.desktop_api import _get_video_alias_ids

    canonical_id, alias_ids = _get_video_alias_ids(conn, clean_id)
    candidates = [canonical_id] + [a for a in alias_ids if a != canonical_id]

    for cand in candidates:
        # 1. Local artwork next to .strm
        try:
            content = _local_video_artwork(db_url, cand, media_root)
        except Exception:
            logger.debug("Failed reading local artwork for %s", cand, exc_info=True)
            content = None
        if content:
            return content

        # 2. Telegram provider in-memory thumbnail cache
        try:
            ref = _telegram_thumb_ref(conn, cand)
            if ref:
                content = _telegram_cached_thumb(ref)
                if content is not None:
                    return content
        except Exception:
            logger.debug("Failed reading telegram cache for %s", cand, exc_info=True)

    return None


def _first_image_in_subdirs(media_root: Path, subdirs: list[str], stem: str) -> bytes | None:
    """Return the first non-black image named stem with a known extension in the given subdirectories of the media root."""
    from common.image_utils import is_black_image

    for sub in subdirs:
        sub_dir = (media_root / sub).resolve() if sub else media_root
        if sub_dir.is_dir() and sub_dir.is_relative_to(media_root):
            for ext in [".jpg", ".jpeg", ".png", ".webp"]:
                candidate = (sub_dir / f"{stem}{ext}").resolve()
                if candidate.is_file() and not stat.S_ISLNK(candidate.lstat().st_mode) and candidate.is_relative_to(media_root):
                    content = candidate.read_bytes()
                    if content and not is_black_image(candidate):
                        return content
    return None


def _performer_canonical_name(conn: db.Connection, clean_id: str, tables: Any) -> str:
    """Resolve a performer identifier to the canonical performer name, falling back to the identifier itself."""
    canonical_name = None
    if clean_id.startswith("name:"):
        canonical_name = clean_id[5:].strip()
    elif "performer_catalog" in tables:
        row = conn.execute(
            "SELECT name FROM performer_catalog WHERE id = %s AND active = true LIMIT 1",
            (clean_id,),
        ).fetchone()
        if row and row[0]:
            canonical_name = str(row[0]).strip()
        elif "performer_catalog_aliases" in tables:
            a_row = conn.execute(
                "SELECT c.name FROM performer_catalog_aliases a JOIN performer_catalog c ON c.id = a.performer_id WHERE a.performer_id = %s AND a.approved = true AND c.active = true LIMIT 1",
                (clean_id,),
            ).fetchone()
            if a_row and a_row[0]:
                canonical_name = str(a_row[0]).strip()
    if not canonical_name:
        canonical_name = clean_id.strip()
    return canonical_name


def _performer_thumb_artwork(conn: db.Connection, canonical_name: str, media_root: Path) -> bytes | None:
    """Return the thumbnail file recorded in performer_photos when it is a local regular file inside the media root."""
    from common.image_utils import is_black_image

    p_row = conn.execute(
        "SELECT thumb FROM performer_photos WHERE lower(name) = lower(%s) ORDER BY CASE WHEN name = %s THEN 0 ELSE 1 END, updated_at DESC NULLS LAST, name ASC LIMIT 1",
        (canonical_name, canonical_name),
    ).fetchone()
    if p_row and p_row[0]:
        thumb_val = str(p_row[0]).strip()
        if not thumb_val.startswith("http://") and not thumb_val.startswith("https://"):
            candidate_path = (media_root / thumb_val).resolve()
            if candidate_path.is_file() and not stat.S_ISLNK(candidate_path.lstat().st_mode) and candidate_path.is_relative_to(media_root):
                content = candidate_path.read_bytes()
                if content and not is_black_image(candidate_path):
                    return content
    return None


def _resolve_performer_artwork(conn: db.Connection, clean_id: str, media_root: Path) -> bytes | None:
    """Find performer artwork from the performer_photos table, then from photo files in the media root."""
    tables = db.table_names(conn)
    canonical_name = _performer_canonical_name(conn, clean_id, tables)

    if canonical_name and "performer_photos" in tables:
        content = _performer_thumb_artwork(conn, canonical_name, media_root)
        if content:
            return content

    return _first_image_in_subdirs(media_root, [".photos", "photos", ""], canonical_name)


def _resolve_artwork_asset(
    conn: db.Connection,
    entity_type: str,
    clean_id: str,
    kind: str,
    request: Request | None = None,
) -> bytes | None:
    from resolver import admin as admin_module

    db_url = getattr(admin_module, "HUB_DB_URL", None) or os.environ.get("DATABASE_URL")
    media_dir = getattr(admin_module, "MEDIA_DIR", "/media/adult")
    media_root = Path(media_dir).resolve()

    if entity_type == "video":
        return _resolve_video_artwork(conn, clean_id, db_url, media_root)
    if entity_type == "performer":
        return _resolve_performer_artwork(conn, clean_id, media_root)
    if entity_type == "channel":
        return _first_image_in_subdirs(media_root, ["channels", ""], clean_id)
    return None


def _artwork_kind_or_error(clean_entity: str, kind: str | None) -> tuple[str | None, str | None]:
    """Normalize the requested artwork kind for an entity, returning KIND_NOT_FOUND when the kind is not allowed."""
    effective_kind = str(kind or "").strip().lower() if kind else None
    if clean_entity == "video":
        if not effective_kind or effective_kind not in _ALLOWED_VIDEO_KINDS:
            return "KIND_NOT_FOUND", None
    elif clean_entity == "performer":
        if effective_kind and effective_kind not in _ALLOWED_PERFORMER_KINDS:
            return "KIND_NOT_FOUND", None
        effective_kind = effective_kind or "photo"
    elif clean_entity == "channel":
        if effective_kind and effective_kind not in _ALLOWED_CHANNEL_KINDS:
            return "KIND_NOT_FOUND", None
        effective_kind = effective_kind or "icon"
    return None, effective_kind


def _artwork_success_response(request: Request, raw_bytes: bytes) -> Response:
    """Build the artwork response, returning 304 when If-None-Match matches the ETag and no body for HEAD."""
    media_type = _detect_image_media_type(raw_bytes)
    etag = f'"{hashlib.sha256(raw_bytes).hexdigest()[:16]}"'

    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match.strip() == etag:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={
                "Cache-Control": "private, no-store",
                "ETag": etag,
            },
        )

    headers = {
        "Cache-Control": "private, no-store",
        "ETag": etag,
        "Content-Length": str(len(raw_bytes)),
    }
    if request.method == "HEAD":
        return Response(
            status_code=status.HTTP_200_OK,
            headers=headers,
            media_type=media_type,
        )
    return Response(
        content=raw_bytes,
        status_code=status.HTTP_200_OK,
        headers=headers,
        media_type=media_type,
    )


