"""Desktop-first API integration slice for Adult Hub Client API v1.

Provides:
- Honest Desktop version and capability discovery (/api/v1/version)
- Authenticated login and single-use refresh token rotation (/api/v1/auth/login, /api/v1/auth/refresh, /api/v1/auth/revoke)
- TV device pairing flow (/api/v1/auth/pair/init, /api/v1/auth/pair/status)
- Authenticated read-only catalog queries:
  - GET /api/v1/catalog/videos (canonical browse and search with filtering)
  - GET /api/v1/catalog/videos/{video_id} (canonical detail with alias resolution)
  - GET /api/v1/catalog/channels (registered Telegram channels catalog)
  - GET /api/v1/catalog/channels/{channel_id} (channel detail)
- Zero metadata leaks: no local filesystem paths (.strm, .jpg), no raw Telegram URLs/credentials,
  no internal admin audit fields, no is_admin exposure in client models
- Strict Cache-Control: private, no-store on sensitive endpoints and error paths
- Actual bounded pre-auth HTTP body reads (max 32 KB) and typed string enforcement
- Explicit auth response fields and structured typed DTOs
- One canonical production DB selector and explicit scoped test injection (isolated factory)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import datetime
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import re
import stat
import time
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse

from common import db
from common.app_version import __version__ as APP_VERSION
from common.config import parse_bool
from common.client_auth.service import (
    MAX_DEVICE_ID_LEN,
    MAX_DEVICE_NAME_LEN,
    MAX_PASSWORD_LEN,
    MAX_USERNAME_LEN,
    AuthenticationFailed,
    BoundedValidationError,
    RateLimitExceeded,
    _safe_hash_token,
    _validate_bounds,
    _validate_optional_string,
    authenticate_by_password,
    refresh_session_token,
    revoke_session_token,
    verify_access_token,
)
from resolver.client_catalog import (
    query_catalog_categories,
    query_catalog_channel_detail,
    query_catalog_channels,
    query_catalog_performer_detail,
    query_catalog_performers,
    query_catalog_studios,
    query_catalog_tags,
    query_catalog_video_detail,
    query_catalog_videos,
)

logger = logging.getLogger("adult_hub.desktop_api")

router = APIRouter(tags=["Desktop API"])

MAX_AUTH_BODY_BYTES: int = 32 * 1024  # 32 KB maximum pre-auth payload size
_RENDITION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.]{1,64}$")


# ---------------------------------------------------------------------------
# Structured Typed DTOs for Auth Responses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientUserDTO:
    """Safe user profile representation strictly omitting is_admin and sensitive fields."""

    id: str
    username: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
        }


@dataclass(frozen=True)
class AuthLoginResponseDTO:
    """Explicit wire format for successful password login."""

    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str
    user: ClientUserDTO

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "user": self.user.to_dict(),
        }


@dataclass(frozen=True)
class AuthRefreshResponseDTO:
    """Explicit wire format for successful single-use token rotation."""

    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
        }


@dataclass(frozen=True)
class AuthRevokeResponseDTO:
    """Explicit wire format for successful session revocation."""

    revoked: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "revoked": self.revoked,
        }


@dataclass(frozen=True)
class PlaybackRenditionDTO:
    """Safe pre-existing Direct Play rendition choices strictly omitting Telegram URLs and upstream credentials."""

    id: str
    label: str
    mime_type: str | None
    file_size_bytes: int | None
    width: int | None
    height: int | None
    is_default: bool
    stream_path: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "mime_type": self.mime_type or "video/mp4",
            "file_size_bytes": self.file_size_bytes,
            "width": self.width,
            "height": self.height,
            "is_default": self.is_default,
            "stream_path": self.stream_path,
        }


@dataclass(frozen=True)
class PlaybackDescriptorDTO:
    """Safe client-facing playback descriptor exposing only canonical ID, safe media metadata, and same-origin stream path."""

    video_id: str
    stream_path: str
    renditions: list[PlaybackRenditionDTO]
    mime_type: str | None = None
    duration_seconds: float | None = None
    file_size_bytes: int | None = None
    width: int | None = None
    height: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "stream_path": self.stream_path,
            "mime_type": self.mime_type or "video/mp4",
            "duration_seconds": self.duration_seconds,
            "file_size_bytes": self.file_size_bytes,
            "width": self.width,
            "height": self.height,
            "renditions": [r.to_dict() for r in self.renditions],
        }


@dataclass(frozen=True)
class WatchProgressDTO:
    """Explicit wire format for user watch progress matching WatchProgressDto schema."""

    video_id: str
    position_seconds: int
    duration_seconds: int
    played_percentage: float
    is_played: bool
    last_watched_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "position_seconds": self.position_seconds,
            "duration_seconds": self.duration_seconds,
            "played_percentage": self.played_percentage,
            "is_played": self.is_played,
            "last_watched_at": self.last_watched_at,
        }


# ---------------------------------------------------------------------------
# Canonical Database Selector & Explicit Scoped Test Injection
# ---------------------------------------------------------------------------


def _canonical_production_db_url() -> str | None:
    """One canonical production DB selector matching Resolver HUB_DB_URL."""
    from resolver import admin as admin_module

    return (
        getattr(admin_module, "HUB_DB_URL", None)
        or (os.getenv("DATABASE_URL") or os.getenv("STANDALONE_DATABASE_URL") or "").strip()
        or None
    )


def _db_connection(request: Request) -> db.Connection:
    """Acquire a database connection via explicit scoped test injection or canonical production selector.

    Isolated test apps without an explicitly configured db_url are strictly prevented
    from implicitly falling back to production connections.
    """
    app_state = getattr(getattr(request, "app", None), "state", None)
    if app_state is not None:
        if hasattr(app_state, "db_url"):
            explicit_url = app_state.db_url
            if explicit_url:
                return db.connect(explicit_url)
            raise RuntimeError("Database connection not configured for isolated test app")
        if getattr(app_state, "isolated_desktop_app", False):
            raise RuntimeError("Database connection not configured for isolated test app")

    url = _canonical_production_db_url()
    if url:
        return db.connect(url)
    raise RuntimeError("Canonical database URL is not configured")


async def _run_in_thread(func, *args, **kwargs):
    """Run synchronous blocking DB queries and CPU-bound scrypt hashing off the asyncio event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


def _extract_client_ip(request: Request) -> str:
    """Extract client peer IP using exclusively the ASGI peer address."""
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def _api_response(
    data: dict[str, Any],
    status_code: int = 200,
    cache_control: str = "private, no-store",
) -> JSONResponse:
    """Helper to return JSON response with specified caching headers."""
    return JSONResponse(
        content=data,
        status_code=status_code,
        headers={"Cache-Control": cache_control},
    )


def _auth_validation_error_response(exc: Exception) -> JSONResponse:
    """Map an AuthenticationFailed to a 401 response and any other auth or validation error to a 400."""
    if isinstance(exc, AuthenticationFailed):
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
    return _api_response(
        {"error": msg},
        status_code=status.HTTP_400_BAD_REQUEST,
        cache_control="private, no-store",
    )


async def _read_bounded_body(request: Request, max_bytes: int = MAX_AUTH_BODY_BYTES) -> bytes:
    """Read request body up to max_bytes.

    Rejects payloads exceeding max_bytes with BoundedValidationError.
    """
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            cl: int | None = int(content_length)
        except (ValueError, TypeError):
            cl = None
        if cl is not None and cl > max_bytes:
            raise BoundedValidationError(f"Request body exceeds maximum size of {max_bytes} bytes")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise BoundedValidationError(f"Request body exceeds maximum size of {max_bytes} bytes")
    return bytes(body)


async def _parse_json_object(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Parse request body and guarantee it is a bounded JSON object (dict)."""
    try:
        raw_body = await _read_bounded_body(request, max_bytes=MAX_AUTH_BODY_BYTES)
    except BoundedValidationError as exc:
        return None, _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except Exception:
        return None, _api_response(
            {"error": "Failed to read request body"},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    if not raw_body or not raw_body.strip():
        return None, _api_response(
            {"error": "Request body must be a valid JSON object"},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return None, _api_response(
            {"error": "Request body must be a valid JSON object"},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    if not isinstance(data, dict):
        return None, _api_response(
            {"error": "Request body must be a JSON object"},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    return data, None


def _require_typed_str(val: Any, field_name: str) -> str:
    """Enforce that a required input value is strictly a string type."""
    if not isinstance(val, str):
        raise BoundedValidationError(f"{field_name} must be a string")
    return val


def _optional_typed_str(val: Any, field_name: str) -> str | None:
    """Enforce that an optional input value is strictly a string or None."""
    if val is None:
        return None
    if not isinstance(val, str):
        raise BoundedValidationError(f"{field_name} must be a string")
    return val


def _clean_str_filter(val: Any, name: str, max_len: int = 255) -> str | None:
    """Validate and clean an optional string query parameter, rejecting NUL bytes and oversized inputs."""
    if val is None:
        return None
    if not isinstance(val, str):
        raise BoundedValidationError(f"{name} must be a string")
    if "\x00" in val:
        raise BoundedValidationError(f"{name} must not contain NUL bytes")
    stripped = val.strip()
    if not stripped:
        return None
    if len(stripped) > max_len:
        raise BoundedValidationError(f"{name} exceeds maximum length of {max_len}")
    return stripped


def _extract_bearer_token(request: Request) -> str:
    """Extract and validate Bearer token from Authorization header before opening DB connections."""
    auth_header = request.headers.get("authorization", "")
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthenticationFailed("Authorization Bearer token required")
    return token.strip()


def _verify_client_session(conn: db.Connection, token: str) -> dict[str, Any]:
    """Validate Bearer access token against client_sessions and client_users.

    Raises AuthenticationFailed on invalid, expired, or revoked tokens.
    Strictly omits is_admin in client session representations.
    """
    session = verify_access_token(conn, token)
    if not session:
        raise AuthenticationFailed("Invalid or expired access token")
    return session


# ---------------------------------------------------------------------------
# Discovery Endpoint
# ---------------------------------------------------------------------------


@router.get("/api/v1/version")
async def get_version_discovery() -> JSONResponse:
    """Desktop-oriented discovery endpoint advertising implemented v1 capabilities."""
    discovery = {
        "api_version": "1.0.0",
        "supported_versions": ["1.0.0"],
        "server_version": APP_VERSION,
        "status": "ok",
        "capabilities": {
            "desktop_api": True,
            "tv_pairing": True,
            "direct_play": True,
            "user_state": False,
            "transcoding": False,
        },
        "endpoints": {
            "auth": "/api/v1/auth",
            "catalog": "/api/v1/catalog",
            "channels": "/api/v1/catalog/channels",
            "playback": "/api/v1/playback",
            "user": "/api/v1/user",
            "artwork": "/api/v1/artwork",
        },
    }
    return _api_response(discovery, status_code=200, cache_control="public, max-age=300")


# ---------------------------------------------------------------------------
# Authentication Endpoints (Password & Token Rotation Only - No Pairing)
# ---------------------------------------------------------------------------


@router.post("/api/v1/auth/login")
async def post_login(request: Request) -> JSONResponse:
    """Authenticate with username and password, issuing short-lived access and refresh tokens."""
    if request.query_params:
        return _api_response(
            {"error": "Query parameters are strictly prohibited on authentication endpoints."},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    payload, err_resp = await _parse_json_object(request)
    if err_resp:
        return err_resp

    raw_username = payload.get("username")
    raw_password = payload.get("password")
    raw_device_name = payload.get("device_name")
    raw_device_id = payload.get("device_id")
    client_ip = _extract_client_ip(request)

    try:
        username = _require_typed_str(raw_username, "username")
        password = _require_typed_str(raw_password, "password")
        _validate_bounds(username, "Username", 1, MAX_USERNAME_LEN)
        _validate_bounds(password, "Password", 1, MAX_PASSWORD_LEN)
        device_name = _validate_optional_string(
            _optional_typed_str(raw_device_name, "device_name"),
            "device_name",
            MAX_DEVICE_NAME_LEN,
        )
        device_id = _validate_optional_string(
            _optional_typed_str(raw_device_id, "device_id"),
            "device_id",
            MAX_DEVICE_ID_LEN,
        )
    except BoundedValidationError as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            return authenticate_by_password(
                conn,
                username=username,
                password=password,
                device_name=device_name or "",
                device_id=device_id or "",
                client_ip=client_ip,
            )
        finally:
            conn.close()

    try:
        result = await _run_in_thread(_execute)
        user_info = result.get("user") or {}
        user_dto = ClientUserDTO(
            id=str(user_info.get("id", "")),
            username=str(user_info.get("username", "")),
        )
        response_dto = AuthLoginResponseDTO(
            access_token=str(result["access_token"]),
            token_type=str(result.get("token_type", "Bearer")),
            expires_in=int(result.get("expires_in", 900)),
            refresh_token=str(result["refresh_token"]),
            user=user_dto,
        )
        return _api_response(
            response_dto.to_dict(),
            status_code=status.HTTP_200_OK,
            cache_control="private, no-store",
        )
    except RateLimitExceeded as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            cache_control="private, no-store",
        )
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except BoundedValidationError as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error during desktop login: %s", exc)
        return _api_response(
            {"error": "Authentication failed"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


def _refresh_token_from_payload(payload: dict[str, Any]) -> tuple[str | None, JSONResponse | None]:
    """Validate the refresh_token payload field, returning the token or a 400 response."""
    raw_token = payload.get("refresh_token")
    if raw_token is None or raw_token == "":
        return None, _api_response(
            {"error": "refresh_token is required"},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    try:
        refresh_token = _require_typed_str(raw_token, "refresh_token")
        _safe_hash_token(refresh_token, name="refresh_token")
    except BoundedValidationError as exc:
        return None, _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    return refresh_token, None


@router.post("/api/v1/auth/refresh")
async def post_refresh(request: Request) -> JSONResponse:
    """Rotate single-use refresh token and issue new access token."""
    if request.query_params:
        return _api_response(
            {"error": "Query parameters are strictly prohibited on authentication endpoints."},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    payload, err_resp = await _parse_json_object(request)
    if err_resp:
        return err_resp

    refresh_token, err_resp = _refresh_token_from_payload(payload)
    if err_resp:
        return err_resp

    def _execute():
        conn = _db_connection(request)
        try:
            return refresh_session_token(conn, refresh_token)
        finally:
            conn.close()

    try:
        result = await _run_in_thread(_execute)
        response_dto = AuthRefreshResponseDTO(
            access_token=str(result["access_token"]),
            token_type=str(result.get("token_type", "Bearer")),
            expires_in=int(result.get("expires_in", 900)),
            refresh_token=str(result["refresh_token"]),
        )
        return _api_response(
            response_dto.to_dict(),
            status_code=status.HTTP_200_OK,
            cache_control="private, no-store",
        )
    except RateLimitExceeded as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            cache_control="private, no-store",
        )
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except BoundedValidationError as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error during desktop token refresh: %s", exc)
        return _api_response(
            {"error": "Token refresh failed"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


@router.post("/api/v1/auth/revoke")
async def post_revoke(request: Request) -> JSONResponse:
    """Explicitly revoke a session family."""
    if request.query_params:
        return _api_response(
            {"error": "Query parameters are strictly prohibited on authentication endpoints."},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    payload, err_resp = await _parse_json_object(request)
    if err_resp:
        return err_resp

    raw_token = payload.get("refresh_token")
    if raw_token is None or raw_token == "":
        return _api_response(
            {"error": "refresh_token is required"},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    try:
        refresh_token = _require_typed_str(raw_token, "refresh_token")
        _safe_hash_token(refresh_token, name="refresh_token")
    except BoundedValidationError as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            revoke_session_token(conn, refresh_token)
        finally:
            conn.close()

    try:
        await _run_in_thread(_execute)
        response_dto = AuthRevokeResponseDTO(revoked=True)
        return _api_response(
            response_dto.to_dict(),
            status_code=status.HTTP_200_OK,
            cache_control="private, no-store",
        )
    except BoundedValidationError as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except AuthenticationFailed:
        return _api_response(
            {"error": "Invalid or expired token"},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.warning("Revocation failed: %s", exc)
        return _api_response(
            {"error": "Failed to revoke token"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


# ---------------------------------------------------------------------------
# TV Device Pairing Endpoints
# ---------------------------------------------------------------------------

from resolver.client_auth_routes import pairing_router  # noqa: E402

router.include_router(pairing_router)


# ---------------------------------------------------------------------------
# Authenticated Read-Only Catalog Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/v1/catalog/videos")
async def get_catalog_videos(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    sort: str = Query("latest"),
    direction: str | None = Query(None),
    channel_id: str | None = None,
    q: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    studio: str | None = None,
    performer: str | None = None,
    performer_id: str | None = None,
    min_duration: int | None = Query(None, ge=0),
    max_duration: int | None = Query(None, ge=0),
    is_favorite: bool | None = Query(None),
) -> JSONResponse:
    """Browse and search canonical catalog videos with deterministic sorting and filtering."""
    try:
        token = _extract_bearer_token(request)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            session = _verify_client_session(conn, token)
            user_id = session.get("user_id") if isinstance(session, dict) else None

            if sort not in {"latest", "title", "duration"}:
                raise BoundedValidationError("Invalid sort parameter. Must be 'latest', 'title', or 'duration'.")

            clean_direction = direction.lower().strip() if direction is not None else None
            if clean_direction is not None and clean_direction not in {"asc", "desc"}:
                raise BoundedValidationError("Invalid direction parameter. Must be 'asc' or 'desc'.")

            clean_channel_id = _clean_str_filter(channel_id, "channel_id")
            clean_q = _clean_str_filter(q, "q")
            clean_category = _clean_str_filter(category, "category")
            clean_tag = _clean_str_filter(tag, "tag")
            clean_studio = _clean_str_filter(studio, "studio")
            # `performer` is a display-name filter (contract: name match); `performer_id` is a canonical ID or name: fallback.
            clean_performer_name = _clean_str_filter(performer, "performer")
            clean_performer_id = _clean_str_filter(performer_id, "performer_id")
            if clean_performer_name is not None:
                clean_performer = f"name:{clean_performer_name}"
            else:
                clean_performer = clean_performer_id

            result = query_catalog_videos(
                conn,
                q=clean_q,
                channel_id=clean_channel_id,
                category=clean_category,
                tag=clean_tag,
                studio=clean_studio,
                performer_id=clean_performer,
                min_duration=min_duration,
                max_duration=max_duration,
                sort=sort,
                direction=clean_direction,
                offset=offset,
                limit=limit,
                user_id=user_id,
                is_favorite=is_favorite,
            )
            return result.to_dict()
        finally:
            conn.close()

    try:
        data = await _run_in_thread(_execute)
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error querying catalog videos: %s", exc)
        return _api_response(
            {"error": "Failed to query catalog videos"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


def _fetch_video_detail(request: Request, token: str, clean_vid: str) -> dict[str, Any] | None:
    """Load the video detail payload for an authenticated caller, or None when the video is missing."""
    conn = _db_connection(request)
    try:
        session = _verify_client_session(conn, token)
        user_id = session.get("user_id") if isinstance(session, dict) else None
        item = query_catalog_video_detail(conn, clean_vid, user_id=user_id)
        if item is None:
            return None
        return item.to_dict()
    finally:
        conn.close()


@router.get("/api/v1/catalog/videos/{video_id}")
async def get_catalog_video_detail(request: Request, video_id: str) -> JSONResponse:
    """Query video details by canonical ID, resolving duplicate merge aliases."""
    try:
        token = _extract_bearer_token(request)
        clean_vid = _clean_str_filter(video_id, "video_id")
    except (AuthenticationFailed, BoundedValidationError, HTTPException) as exc:
        return _auth_validation_error_response(exc)

    if not clean_vid:
        return _api_response(
            {"error": "video_id is required"},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    try:
        data = await _run_in_thread(_fetch_video_detail, request, token, clean_vid)
        if data is None:
            return _api_response(
                {"error": "Video not found"},
                status_code=status.HTTP_404_NOT_FOUND,
                cache_control="private, no-store",
            )
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except (AuthenticationFailed, BoundedValidationError, HTTPException) as exc:
        return _auth_validation_error_response(exc)
    except Exception as exc:
        logger.exception("Unexpected error querying catalog video detail: %s", exc)
        return _api_response(
            {"error": "Failed to query video detail"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


@router.get("/api/v1/catalog/channels")
async def get_catalog_channels(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    sort: str = Query("title"),
) -> JSONResponse:
    """Browse registered Telegram channels catalog with active video counts."""
    if sort not in {"title", "video_count", "latest"}:
        return _api_response(
            {"error": "Invalid sort parameter. Must be 'title', 'video_count', or 'latest'."},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    try:
        token = _extract_bearer_token(request)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            _verify_client_session(conn, token)
            result = query_catalog_channels(
                conn,
                sort=sort,
                offset=offset,
                limit=limit,
            )
            return result.to_dict()
        finally:
            conn.close()

    try:
        data = await _run_in_thread(_execute)
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error querying catalog channels: %s", exc)
        return _api_response(
            {"error": "Failed to query channels"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


def _fetch_channel_detail(request: Request, token: str, clean_cid: str) -> dict[str, Any] | None:
    """Load the channel detail payload for an authenticated caller, or None when the channel is missing."""
    conn = _db_connection(request)
    try:
        _verify_client_session(conn, token)
        item = query_catalog_channel_detail(conn, clean_cid)
        if item is None:
            return None
        return item.to_dict()
    finally:
        conn.close()


@router.get("/api/v1/catalog/channels/{channel_id}")
async def get_catalog_channel_detail(request: Request, channel_id: str) -> JSONResponse:
    """Query channel details and active video counts by channel ID."""
    try:
        token = _extract_bearer_token(request)
        clean_cid = _clean_str_filter(channel_id, "channel_id")
    except (AuthenticationFailed, BoundedValidationError, HTTPException) as exc:
        return _auth_validation_error_response(exc)

    if not clean_cid:
        return _api_response(
            {"error": "channel_id is required"},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    try:
        data = await _run_in_thread(_fetch_channel_detail, request, token, clean_cid)
        if data is None:
            return _api_response(
                {"error": "Channel not found"},
                status_code=status.HTTP_404_NOT_FOUND,
                cache_control="private, no-store",
            )
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except (AuthenticationFailed, BoundedValidationError, HTTPException) as exc:
        return _auth_validation_error_response(exc)
    except Exception as exc:
        logger.exception("Unexpected error querying channel detail: %s", exc)
        return _api_response(
            {"error": "Failed to query channel detail"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


@router.get("/api/v1/catalog/categories")
async def get_catalog_categories(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    q: str | None = None,
) -> JSONResponse:
    """List video categories with bounded pagination and distinct movie counts."""
    try:
        token = _extract_bearer_token(request)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            _verify_client_session(conn, token)
            clean_q = _clean_str_filter(q, "q")
            result = query_catalog_categories(conn, q=clean_q, offset=offset, limit=limit)
            return result.to_dict()
        finally:
            conn.close()

    try:
        data = await _run_in_thread(_execute)
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error querying catalog categories: %s", exc)
        return _api_response(
            {"error": "Failed to query categories"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


@router.get("/api/v1/catalog/studios")
async def get_catalog_studios(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    q: str | None = None,
) -> JSONResponse:
    """List studios with bounded pagination and distinct movie counts."""
    try:
        token = _extract_bearer_token(request)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            _verify_client_session(conn, token)
            clean_q = _clean_str_filter(q, "q")
            result = query_catalog_studios(conn, q=clean_q, offset=offset, limit=limit)
            return result.to_dict()
        finally:
            conn.close()

    try:
        data = await _run_in_thread(_execute)
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error querying catalog studios: %s", exc)
        return _api_response(
            {"error": "Failed to query studios"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


@router.get("/api/v1/catalog/tags")
async def get_catalog_tags(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    q: str | None = None,
) -> JSONResponse:
    """List tags with bounded pagination and distinct movie counts."""
    try:
        token = _extract_bearer_token(request)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            _verify_client_session(conn, token)
            clean_q = _clean_str_filter(q, "q")
            result = query_catalog_tags(conn, q=clean_q, offset=offset, limit=limit)
            return result.to_dict()
        finally:
            conn.close()

    try:
        data = await _run_in_thread(_execute)
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error querying catalog tags: %s", exc)
        return _api_response(
            {"error": "Failed to query tags"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


@router.get("/api/v1/catalog/performers")
async def get_catalog_performers(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: str | None = None,
    letter: str | None = None,
    photo_only: bool = Query(False),
    sort: str = Query("name"),
    direction: str | None = Query(None),
) -> JSONResponse:
    """List performers with bounded pagination, filters, and movie counts."""
    try:
        token = _extract_bearer_token(request)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            _verify_client_session(conn, token)

            clean_q = _clean_str_filter(q, "q")
            clean_letter = _clean_str_filter(letter, "letter")

            if sort not in {"name", "video_count", "count"}:
                raise BoundedValidationError("Invalid sort parameter. Must be 'name', 'video_count', or 'count'.")

            clean_direction = direction.lower().strip() if direction is not None else None
            if clean_direction is not None and clean_direction not in {"asc", "desc"}:
                raise BoundedValidationError("Invalid direction parameter. Must be 'asc' or 'desc'.")

            result = query_catalog_performers(
                conn,
                q=clean_q,
                letter=clean_letter,
                photo_only=photo_only,
                sort=sort,
                direction=clean_direction,
                offset=offset,
                limit=limit,
            )
            return result.to_dict()
        finally:
            conn.close()

    try:
        data = await _run_in_thread(_execute)
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error querying catalog performers: %s", exc)
        return _api_response(
            {"error": "Failed to query performers"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


@router.get("/api/v1/catalog/performers/{performer_id:path}")
async def get_catalog_performer_detail(
    request: Request,
    performer_id: str,
) -> JSONResponse:
    """Get metadata, movie count, and biographical details for a performer."""
    try:
        token = _extract_bearer_token(request)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            _verify_client_session(conn, token)

            clean_pid = _clean_str_filter(performer_id, "performer_id")
            if not clean_pid:
                raise BoundedValidationError("performer_id is required")

            detail = query_catalog_performer_detail(conn, clean_pid)
            if detail is None:
                return None
            return detail.to_dict()
        finally:
            conn.close()

    try:
        data = await _run_in_thread(_execute)
        if data is None:
            clean_display = performer_id.strip()
            return _api_response(
                {"error": f"Performer '{clean_display}' not found", "code": "PERFORMER_NOT_FOUND"},
                status_code=status.HTTP_404_NOT_FOUND,
                cache_control="private, no-store",
            )
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error querying catalog performer detail: %s", exc)
        return _api_response(
            {"error": "Failed to query performer detail"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


# ---------------------------------------------------------------------------
# Authenticated Artwork Endpoints
# ---------------------------------------------------------------------------

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


def _local_video_artwork(db_url: str | None, cand: str, media_root: Path) -> bytes | None:
    """Read artwork stored next to a video's .strm file when it is a regular file inside the media root."""
    from resolver.admin_catalog_queries import _get_video_artwork_path
    from common.image_utils import is_black_image

    art_path = _get_video_artwork_path(db_url, cand, media_root)
    if art_path is not None and art_path.is_file():
        res_path = art_path.resolve()
        if res_path.is_relative_to(media_root) and not stat.S_ISLNK(art_path.lstat().st_mode):
            content = res_path.read_bytes()
            if content and not is_black_image(res_path):
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


def _lookup_artwork_asset(
    request: Request,
    token: str,
    entity_type: str,
    raw_id: str,
    kind: str | None,
) -> tuple[str | None, bytes | None, str | None]:
    """Verify the session, check the entity and kind allowlists, and load the artwork bytes."""
    conn = _db_connection(request)
    try:
        _verify_client_session(conn, token)

        # 3. Entity type allowlist
        clean_entity = str(entity_type or "").strip().lower()
        if clean_entity not in _ALLOWED_ARTWORK_ENTITIES:
            return "ENTITY_NOT_FOUND", None, None

        # 4. Kind allowlist
        err, effective_kind = _artwork_kind_or_error(clean_entity, kind)
        if err is not None:
            return err, None, None

        # 5. Identifier validation & path traversal protection
        clean_id = _validate_artwork_id(raw_id, clean_entity)

        # 6. Asset lookup
        raw_bytes = _resolve_artwork_asset(conn, clean_entity, clean_id, effective_kind, request)
        return None, raw_bytes, clean_id
    finally:
        conn.close()


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


async def _handle_artwork_request(
    request: Request,
    entity_type: str,
    raw_id: str,
    kind: str | None,
) -> Response:
    # 1. Bearer token extraction and authentication ALWAYS FIRST
    try:
        token = _extract_bearer_token(request)
    except AuthenticationFailed as exc:
        return _artwork_error_response(
            "Unauthorized",
            status.HTTP_401_UNAUTHORIZED,
            str(exc),
            "UNAUTHORIZED",
        )

    # 2. Database session verification and asset resolution
    try:
        err, raw_bytes, clean_id = await _run_in_thread(_lookup_artwork_asset, request, token, entity_type, raw_id, kind)
        if err == "ENTITY_NOT_FOUND":
            return _artwork_error_response(
                "Entity Not Found",
                status.HTTP_404_NOT_FOUND,
                f"Unsupported artwork entity type '{entity_type}'",
                "ARTWORK_ENTITY_NOT_FOUND",
            )
        if err == "KIND_NOT_FOUND":
            return _artwork_error_response(
                "Artwork Not Found",
                status.HTTP_404_NOT_FOUND,
                f"Unsupported artwork kind '{kind}' for entity '{entity_type}'",
                "ARTWORK_KIND_NOT_FOUND",
            )
        if not raw_bytes:
            return _artwork_error_response(
                "Artwork Not Found",
                status.HTTP_404_NOT_FOUND,
                f"Artwork not found for {entity_type}",
                "ARTWORK_NOT_FOUND",
            )

        return _artwork_success_response(request, raw_bytes)

    except AuthenticationFailed as exc:
        return _artwork_error_response(
            "Unauthorized",
            status.HTTP_401_UNAUTHORIZED,
            str(exc),
            "UNAUTHORIZED",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _artwork_error_response(
            "Invalid Request",
            status.HTTP_400_BAD_REQUEST,
            msg,
            "INVALID_REQUEST",
        )
    except Exception as exc:
        logger.exception("Unexpected error in artwork endpoint: %s", exc)
        return _artwork_error_response(
            "Internal Server Error",
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Failed to serve artwork",
            "INTERNAL_ERROR",
        )


@router.api_route(
    "/api/v1/artwork/video/{video_id:path}/{kind}",
    methods=["GET", "HEAD"],
)
async def get_video_artwork(
    request: Request,
    video_id: str,
    kind: str,
) -> Response:
    return await _handle_artwork_request(request, "video", video_id, kind)


@router.api_route(
    "/api/v1/artwork/performer/{performer_id:path}/{kind}",
    methods=["GET", "HEAD"],
)
async def get_performer_artwork_with_kind(
    request: Request,
    performer_id: str,
    kind: str,
) -> Response:
    return await _handle_artwork_request(request, "performer", performer_id, kind)


@router.api_route(
    "/api/v1/artwork/performer/{performer_id:path}",
    methods=["GET", "HEAD"],
)
async def get_performer_artwork(
    request: Request,
    performer_id: str,
) -> Response:
    return await _handle_artwork_request(request, "performer", performer_id, None)


@router.api_route(
    "/api/v1/artwork/channel/{channel_id:path}/{kind}",
    methods=["GET", "HEAD"],
)
async def get_channel_artwork_with_kind(
    request: Request,
    channel_id: str,
    kind: str,
) -> Response:
    return await _handle_artwork_request(request, "channel", channel_id, kind)


@router.api_route(
    "/api/v1/artwork/channel/{channel_id:path}",
    methods=["GET", "HEAD"],
)
async def get_channel_artwork(
    request: Request,
    channel_id: str,
) -> Response:
    return await _handle_artwork_request(request, "channel", channel_id, None)


@router.api_route(
    "/api/v1/artwork/{entity_type}/{id:path}/{kind}",
    methods=["GET", "HEAD"],
)
async def get_generic_artwork_with_kind(
    request: Request,
    entity_type: str,
    id: str,
    kind: str,
) -> Response:
    return await _handle_artwork_request(request, entity_type, id, kind)


@router.api_route(
    "/api/v1/artwork/{entity_type}/{id:path}",
    methods=["GET", "HEAD"],
)
async def get_generic_artwork(
    request: Request,
    entity_type: str,
    id: str,
) -> Response:
    return await _handle_artwork_request(request, entity_type, id, None)


# ---------------------------------------------------------------------------
# Direct Play Playback Discovery and Streaming Endpoints
# ---------------------------------------------------------------------------


def _parse_play_id(video_id: str) -> tuple[str, str]:
    """Parse provider composite or plain video ID, defaulting to Telegram."""
    if not video_id:
        return "telegram", ""
    text = video_id.strip()
    if ":" in text:
        from resolver.providers import list_providers

        prov, raw_id = text.split(":", 1)
        if prov in list_providers():
            return prov, raw_id
        raise BoundedValidationError(f"Unknown provider prefix '{prov}' in id {video_id!r}")
    return "telegram", text


def _pick_canonical_stream_id(conn: db.Connection, requested_id: str, tables: Any) -> str:
    """Resolve a requested video ID to its canonical ID through source aliases and duplicate decisions."""
    canonical_id = requested_id
    if "video_sources" in tables:
        alias_row = conn.execute(
            "SELECT video_id FROM video_sources WHERE source_id=%s AND video_id!=%s AND is_active=1",
            (requested_id, requested_id),
        ).fetchone()
        if alias_row and alias_row[0]:
            canonical_id = alias_row[0]
    if canonical_id == requested_id and "video_duplicate_decisions" in tables:
        dec_row = conn.execute(
            "SELECT keeper_id FROM video_duplicate_decisions WHERE loser_id=%s AND state='active'",
            (requested_id,),
        ).fetchone()
        if dec_row and dec_row[0]:
            canonical_id = dec_row[0]
    return canonical_id


def _collect_stream_candidate_ids(conn: db.Connection, requested_id: str, canonical_id: str, tables: Any) -> list[str]:
    """Build the ordered candidate list: the canonical ID, its active sources, then the requested ID."""
    candidates = [canonical_id]
    if "video_sources" in tables:
        sources = conn.execute(
            "SELECT source_id FROM video_sources WHERE video_id=%s AND is_active=1 ORDER BY priority ASC, id ASC",
            (canonical_id,),
        ).fetchall()
        for (s,) in sources:
            if s and s not in candidates:
                candidates.append(s)
    if requested_id not in candidates:
        candidates.append(requested_id)
    return candidates


def _get_stream_candidate_ids(requested_id: str, conn: db.Connection | None = None) -> list[str]:
    """Look up canonical video ID and all active failover sources from the database."""
    close_conn = False
    if conn is None:
        url = _canonical_production_db_url()
        if not url:
            return [requested_id]
        conn = db.connect(url)
        close_conn = True
    try:
        tables = db.table_names(conn)
        canonical_id = _pick_canonical_stream_id(conn, requested_id, tables)
        return _collect_stream_candidate_ids(conn, requested_id, canonical_id, tables)
    except Exception as exc:
        logger.debug("Source candidate lookup error for %s: %s", requested_id, exc)
        return [requested_id]
    finally:
        if close_conn:
            conn.close()


def _is_video_visible_and_playable(conn: db.Connection, canonical_id: str) -> bool:
    """Verify that video exists, is playable, not dead, and not quarantined."""
    tables = db.table_names(conn)
    if "videos" not in tables:
        return True
    row = conn.execute(
        "SELECT playable FROM videos WHERE id = %s",
        (canonical_id,),
    ).fetchone()
    if row is None:
        return False
    if row[0] is not None and row[0] == 0:
        return False
    if "dead_streams" in tables:
        dead = conn.execute(
            "SELECT 1 FROM dead_streams WHERE video_id = %s",
            (canonical_id,),
        ).fetchone()
        if dead:
            return False
    if "video_duplicate_decisions" in tables:
        quarantined = conn.execute(
            "SELECT 1 FROM video_duplicate_decisions WHERE loser_id = %s AND keeper_id = %s AND state = 'active'",
            (canonical_id, canonical_id),
        ).fetchone()
        if quarantined:
            return False
    return True


async def _fetch_telegram_variants(cand_id: str, raw_id: str):
    """Query Telegram provider for pre-existing variants. Returns (variants_data, error)."""
    channel_ref = (raw_id or "").split(":", 1)[0]
    try:
        from resolver.providers import get_provider

        tg_prov = get_provider("telegram")
    except KeyError:
        return None, HTTPException(status_code=502, detail="Telegram provider not available")

    auth_check = getattr(tg_prov, "is_channel_authorized", None)
    if callable(auth_check) and not auth_check(channel_ref):
        return None, HTTPException(status_code=403, detail="Telegram channel is not authorized")

    variants_fn = getattr(tg_prov, "get_video_variants", None)
    if not callable(variants_fn):
        return None, None

    try:
        res = await variants_fn(raw_id)
        if isinstance(res, dict):
            res["video_id"] = cand_id
        return res, None
    except HTTPException as exc:
        return None, exc
    except ValueError:
        return None, HTTPException(status_code=404, detail="Video variants are unavailable")
    except Exception as exc:
        logger.warning("Failed to fetch variants for %s: %s", cand_id, exc)
        return None, exc


def _build_playback_descriptor(
    canonical_id: str,
    variants_data: dict[str, Any] | None,
    media_metadata: dict[str, Any] | None = None,
) -> PlaybackDescriptorDTO:
    """Build a safe client-facing playback descriptor strictly omitting Telegram URLs and credentials."""
    meta = media_metadata or {}
    raw_variants = (variants_data.get("variants", []) if variants_data else []) or []

    rendition_dtos: list[PlaybackRenditionDTO] = []
    seen_ids: set[str] = set()

    for var in raw_variants:
        var_id = str(var.get("id") or "").strip()
        if not var_id or var_id in seen_ids:
            continue
        seen_ids.add(var_id)

        is_orig = bool(var.get("is_original", False))
        h = var.get("height")
        w = var.get("width")
        mime = var.get("mime_type") or "video/mp4"
        size = var.get("file_size")

        if is_orig:
            label = f"Original Direct Play ({h}p)" if h else "Original Direct Play"
            stream_path = f"/api/v1/playback/stream/{canonical_id}"
        else:
            label = f"Telegram Alternate Stream ({h}p)" if h else "Telegram Alternate Stream"
            stream_path = f"/api/v1/playback/stream/{canonical_id}?rendition_id={var_id}"

        rendition_dtos.append(
            PlaybackRenditionDTO(
                id=var_id,
                label=label,
                mime_type=mime,
                file_size_bytes=size,
                width=w,
                height=h,
                is_default=is_orig,
                stream_path=stream_path,
            )
        )

    if not rendition_dtos:
        rendition_dtos.append(
            PlaybackRenditionDTO(
                id="original",
                label="Original Direct Play",
                mime_type=meta.get("mime_type") or "video/mp4",
                file_size_bytes=meta.get("file_size"),
                width=meta.get("width"),
                height=meta.get("height"),
                is_default=True,
                stream_path=f"/api/v1/playback/stream/{canonical_id}",
            )
        )

    # Sort renditions: default (original) first, then alternatives by height descending
    rendition_dtos.sort(key=lambda r: (not r.is_default, -(r.height or 0)))

    default_rendition = next((r for r in rendition_dtos if r.is_default), rendition_dtos[0])
    main_stream_path = default_rendition.stream_path

    return PlaybackDescriptorDTO(
        video_id=canonical_id,
        stream_path=main_stream_path,
        renditions=rendition_dtos,
        mime_type=default_rendition.mime_type or "video/mp4",
        duration_seconds=meta.get("duration"),
        file_size_bytes=default_rendition.file_size_bytes or meta.get("file_size"),
        width=default_rendition.width or meta.get("width"),
        height=default_rendition.height or meta.get("height"),
    )


@router.get("/api/v1/playback/capabilities")
async def get_playback_capabilities(request: Request) -> JSONResponse:
    """Discover playback engine streaming capabilities."""
    if request.query_params:
        return _api_response(
            {"error": "Query parameters are strictly prohibited on capabilities endpoint."},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    try:
        token = _extract_bearer_token(request)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            _verify_client_session(conn, token)
        finally:
            conn.close()

    try:
        await _run_in_thread(_execute)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Error checking auth for playback capabilities: %s", exc)
        return _api_response(
            {"error": "Failed to retrieve playback capabilities"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )

    capabilities = {
        "streaming_protocols": ["http_range_rfc7233"],
        "supported_containers": ["video/mp4", "video/webm", "video/x-matroska"],
        "seek_alignment_bytes": 1048576,
        "pinned_representation_required": True,
        "supports_head_requests": True,
    }
    return _api_response(capabilities, status_code=200, cache_control="private, no-store")


def _unexpected_stream_query_response(request: Request) -> JSONResponse | None:
    """Return a 400 response when a stream request carries any query parameter other than rendition_id."""
    for qkey in request.query_params.keys():
        if qkey != "rendition_id":
            return _api_response(
                {"error": f"Unexpected query parameter '{qkey}'"},
                status_code=status.HTTP_400_BAD_REQUEST,
                cache_control="private, no-store",
            )
    return None


def _parse_stream_request_params(request: Request, video_id: str, rendition_id: str | None) -> tuple[str, str, str | None]:
    """Extract the bearer token, cleaned video ID, and validated rendition ID from a stream request."""
    token = _extract_bearer_token(request)
    clean_id = _clean_str_filter(video_id, "video_id")
    clean_rendition = _clean_str_filter(rendition_id, "rendition_id", max_len=64) if rendition_id else None
    if clean_rendition and not _RENDITION_ID_PATTERN.match(clean_rendition):
        raise BoundedValidationError("rendition_id contains invalid characters")
    return token, clean_id, clean_rendition


def _load_stream_candidates(request: Request, token: str, clean_id: str) -> tuple[str, list[str]] | None:
    """Verify the session and return the canonical ID with its candidate sources, or None when the video is unavailable."""
    conn = _db_connection(request)
    try:
        _verify_client_session(conn, token)
        candidates = _get_stream_candidate_ids(clean_id, conn)
        canonical_id = candidates[0] if candidates else clean_id
        if not _is_video_visible_and_playable(conn, canonical_id):
            return None
        return canonical_id, candidates
    finally:
        conn.close()


async def _resolve_telegram_stream(tg_prov: Any, raw_id: str, chosen_variant: str | None) -> Any:
    """Ask the Telegram provider for a stream URL, passing the rendition variant when one is requested."""
    if chosen_variant and chosen_variant.lower() != "original":
        return await tg_prov.resolve_stream(raw_id, variant=chosen_variant)
    return await tg_prov.resolve_stream(raw_id)


def _telegram_cache_key(raw_id: str, chosen_variant: str | None) -> str:
    """Build the proxy cache key for a Telegram file, including the rendition variant when one is requested."""
    active_cache_key = f"telegram:{raw_id}"
    if chosen_variant and chosen_variant.lower() != "original":
        active_cache_key = f"{active_cache_key}:variant:{chosen_variant}"
    return active_cache_key


async def _pre_play_telegram_stream(candidates: list[str], chosen_variant: str | None) -> tuple[str | None, str | None, Any]:
    """Resolve the first authorized Telegram candidate to a stream URL, returning the URL, cache key, and last error."""
    stream_url = None
    active_cache_key = None
    last_error = None
    for cand_id in candidates:
        try:
            prov_name, raw_id = _parse_play_id(cand_id)
        except Exception as exc:
            last_error = exc
            continue

        if prov_name != "telegram":
            continue

        channel_ref = (raw_id or "").split(":", 1)[0]
        try:
            from resolver.providers import get_provider

            tg_prov = get_provider("telegram")
        except KeyError:
            last_error = HTTPException(status_code=502, detail="Telegram provider not available")
            break

        auth_check = getattr(tg_prov, "is_channel_authorized", None)
        if callable(auth_check) and not auth_check(channel_ref):
            last_error = HTTPException(status_code=403, detail="Telegram channel is not authorized")
            continue

        try:
            cand_stream = await _resolve_telegram_stream(tg_prov, raw_id, chosen_variant)
            if cand_stream:
                stream_url = cand_stream
                active_cache_key = _telegram_cache_key(raw_id, chosen_variant)
                break
        except Exception as exc:
            logger.warning("Pre-play stream resolution failed for candidate %s: %s", cand_id, type(exc).__name__)
            last_error = exc
            continue

    return stream_url, active_cache_key, last_error


def _pre_play_error_response(last_error: Any, chosen_variant: str | None, clean_id: str) -> JSONResponse:
    """Map the last pre-play resolution failure to the client-facing stream error response."""
    if isinstance(last_error, HTTPException):
        if last_error.status_code == 403:
            return _api_response(
                {"error": "Telegram channel is not authorized", "code": "CHANNEL_UNAUTHORIZED"},
                status_code=status.HTTP_403_FORBIDDEN,
                cache_control="private, no-store",
            )
        if last_error.status_code == 404:
            return _api_response(
                {"error": "Stream not found", "code": "NOT_FOUND"},
                status_code=status.HTTP_404_NOT_FOUND,
                cache_control="private, no-store",
            )
    if chosen_variant and chosen_variant.lower() != "original":
        if isinstance(last_error, ValueError) and "not found" in str(last_error).lower():
            return _api_response(
                {"error": f"Rendition '{chosen_variant}' not found for this video", "code": "RENDITION_NOT_FOUND"},
                status_code=status.HTTP_404_NOT_FOUND,
                cache_control="private, no-store",
            )
    return _api_response(
        {
            "type": "https://adulthub.media/errors/video-source-unavailable",
            "title": "Video Source Unavailable",
            "status": 502,
            "detail": f"None of the registered sources for video {clean_id} could be reached.",
            "instance": f"/api/v1/playback/stream/{clean_id}",
            "code": "SOURCE_UNAVAILABLE",
        },
        status_code=status.HTTP_502_BAD_GATEWAY,
        cache_control="private, no-store",
    )


async def _proxy_telegram_stream(request: Request, stream_url: str, active_cache_key: str | None) -> Response:
    """Proxy the resolved Telegram stream to the client with no-store caching."""
    from resolver.providers import get_provider
    from resolver.proxy_utils import _parse_range_request
    from resolver.telegram_proxy import (
        TelegramProxyServices,
        proxy_telegram_response as _proxy_telegram_response_impl,
    )

    services = TelegramProxyServices(
        get_provider=get_provider,
        parse_range_request=_parse_range_request,
        asyncio=asyncio,
        inspect=inspect,
        record_file_size=None,
    )

    response = await _proxy_telegram_response_impl(
        request, stream_url, active_cache_key, services=services
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _proxy_http_error_response(request: Request, exc: HTTPException) -> Response:
    """Build the response for an HTTPException raised while proxying a stream, with an empty body for HEAD."""
    headers = dict(exc.headers or {})
    headers["Cache-Control"] = "private, no-store"
    if (request.method or "GET").upper() == "HEAD":
        return Response(content=b"", status_code=exc.status_code, headers=headers)
    prob = {
        "type": "https://adulthub.media/errors/playback-error",
        "title": "Range Not Satisfiable" if exc.status_code == 416 else "Playback Error",
        "status": exc.status_code,
        "detail": str(exc.detail),
        "code": "RANGE_NOT_SATISFIABLE" if exc.status_code == 416 else "PLAYBACK_ERROR",
    }
    return JSONResponse(status_code=exc.status_code, content=prob, headers=headers)


def _proxy_failure_response(request: Request) -> Response:
    """Build the 502 response for an unexpected stream proxy failure, with an empty body for HEAD."""
    if (request.method or "GET").upper() == "HEAD":
        return Response(content=b"", status_code=status.HTTP_502_BAD_GATEWAY, headers={"Cache-Control": "private, no-store"})
    return _api_response(
        {
            "type": "https://adulthub.media/errors/playback-error",
            "title": "Stream Delivery Error",
            "status": 502,
            "detail": "Failed to proxy media stream from upstream provider.",
            "code": "STREAM_PROXY_ERROR",
        },
        status_code=status.HTTP_502_BAD_GATEWAY,
        cache_control="private, no-store",
    )


@router.api_route("/api/v1/playback/stream/{video_id:path}", methods=["GET", "HEAD"])
async def play_stream_direct(
    request: Request,
    video_id: str,
    rendition_id: str | None = Query(None),
) -> Response:
    """Stream video content with RFC 7233 byte-range support and pre-play source fallback."""
    bad_query = _unexpected_stream_query_response(request)
    if bad_query is not None:
        return bad_query

    try:
        token, clean_id, clean_rendition = _parse_stream_request_params(request, video_id, rendition_id)
    except (AuthenticationFailed, BoundedValidationError, HTTPException) as exc:
        return _auth_validation_error_response(exc)

    if not clean_id:
        return _api_response(
            {"error": "video_id is required"},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    try:
        db_res = await _run_in_thread(_load_stream_candidates, request, token, clean_id)
    except (AuthenticationFailed, BoundedValidationError, HTTPException) as exc:
        return _auth_validation_error_response(exc)
    except Exception as exc:
        logger.exception("Error verifying auth for stream: %s", exc)
        return _api_response(
            {"error": "Database lookup failed"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )

    if db_res is None:
        return _api_response(
            {"error": "Video not found or unavailable"},
            status_code=status.HTTP_404_NOT_FOUND,
            cache_control="private, no-store",
        )

    canonical_id, candidates = db_res

    chosen_variant = clean_rendition
    stream_url, active_cache_key, last_error = await _pre_play_telegram_stream(candidates, chosen_variant)
    if not stream_url:
        return _pre_play_error_response(last_error, chosen_variant, clean_id)

    try:
        return await _proxy_telegram_stream(request, stream_url, active_cache_key)
    except HTTPException as exc:
        return _proxy_http_error_response(request, exc)
    except Exception as exc:
        logger.warning("Unexpected error proxying stream for %s: %s", clean_id, type(exc).__name__)
        return _proxy_failure_response(request)


def _descriptor_meta(conn: db.Connection, canonical_id: str) -> dict[str, Any]:
    """Read duration and file size for a video from the videos table, or return an empty dict when absent."""
    meta = {}
    if "videos" in db.table_names(conn):
        row = conn.execute(
            "SELECT duration, file_size_bytes FROM videos WHERE id = %s",
            (canonical_id,),
        ).fetchone()
        if row:
            meta = {
                "duration": float(row[0]) if row[0] is not None else None,
                "file_size": int(row[1]) if row[1] is not None else None,
                "width": None,
                "height": None,
            }
    return meta


def _load_descriptor_context(request: Request, token: str, clean_id: str) -> tuple[str, list[str], dict[str, Any]] | None:
    """Verify the session and load the canonical ID, candidate sources, and metadata, or None when the video is unavailable."""
    conn = _db_connection(request)
    try:
        _verify_client_session(conn, token)
        candidates = _get_stream_candidate_ids(clean_id, conn)
        canonical_id = candidates[0] if candidates else clean_id
        if not _is_video_visible_and_playable(conn, canonical_id):
            return None
        return canonical_id, candidates, _descriptor_meta(conn, canonical_id)
    finally:
        conn.close()


async def _first_telegram_variants(candidates: list[str]) -> tuple[Any, Any]:
    """Try Telegram candidates in order and return the first variant metadata found, plus the last error seen."""
    variants_data = None
    last_error = None
    for cand_id in candidates:
        try:
            prov_name, raw_id = _parse_play_id(cand_id)
        except Exception as exc:
            last_error = exc
            continue

        if prov_name != "telegram":
            continue

        vdata, error = await _fetch_telegram_variants(cand_id, raw_id)
        if error is not None:
            last_error = error
        if vdata is not None:
            variants_data = vdata
            break
    return variants_data, last_error


def _descriptor_error_response(last_error: Any, clean_id: str) -> JSONResponse:
    """Map the last Telegram variant lookup failure to the playback descriptor error response."""
    if isinstance(last_error, HTTPException):
        if last_error.status_code == 403:
            return _api_response(
                {"error": "Telegram channel is not authorized", "code": "CHANNEL_UNAUTHORIZED"},
                status_code=status.HTTP_403_FORBIDDEN,
                cache_control="private, no-store",
            )
        if last_error.status_code == 404:
            return _api_response(
                {"error": "Video variants unavailable", "code": "NOT_FOUND"},
                status_code=status.HTTP_404_NOT_FOUND,
                cache_control="private, no-store",
            )
    return _api_response(
        {
            "type": "https://adulthub.media/errors/video-source-unavailable",
            "title": "Video Source Unavailable",
            "status": 502,
            "detail": f"None of the registered sources for video {clean_id} could be reached.",
            "code": "SOURCE_UNAVAILABLE",
        },
        status_code=status.HTTP_502_BAD_GATEWAY,
        cache_control="private, no-store",
    )


@router.get("/api/v1/playback/{video_id:path}")
@router.get("/api/v1/playback/renditions/{video_id:path}")
async def get_playback_descriptor_endpoint(request: Request, video_id: str) -> JSONResponse:
    """Discover playback descriptor and available pre-existing Direct Play renditions."""
    if request.query_params:
        return _api_response(
            {"error": "Query parameters are strictly prohibited on playback descriptor endpoint."},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )
    try:
        token = _extract_bearer_token(request)
        clean_id = _clean_str_filter(video_id, "video_id")
        if clean_id.startswith("renditions/"):
            clean_id = clean_id[len("renditions/"):]
    except (AuthenticationFailed, BoundedValidationError, HTTPException) as exc:
        return _auth_validation_error_response(exc)

    if not clean_id:
        return _api_response(
            {"error": "video_id is required"},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    try:
        db_res = await _run_in_thread(_load_descriptor_context, request, token, clean_id)
    except (AuthenticationFailed, BoundedValidationError, HTTPException) as exc:
        return _auth_validation_error_response(exc)
    except Exception as exc:
        logger.exception("Error verifying auth for playback descriptor: %s", exc)
        return _api_response(
            {"error": "Database lookup failed"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )

    if db_res is None:
        return _api_response(
            {"error": "Video not found or unavailable"},
            status_code=status.HTTP_404_NOT_FOUND,
            cache_control="private, no-store",
        )

    canonical_id, candidates, meta = db_res

    variants_data, last_error = await _first_telegram_variants(candidates)
    if variants_data is None and last_error is not None:
        return _descriptor_error_response(last_error, clean_id)

    descriptor = _build_playback_descriptor(canonical_id, variants_data, meta)
    return _api_response(descriptor.to_dict(), status_code=status.HTTP_200_OK, cache_control="private, no-store")


# ---------------------------------------------------------------------------
# User State: Watch Progress Endpoints
# ---------------------------------------------------------------------------


def _resolve_canonical_video_id(conn: db.Connection, requested_id: str) -> str:
    """Resolve video ID to its canonical keeper ID using active duplicate decisions and video_sources."""
    clean_id = str(requested_id).strip()
    if not clean_id:
        return clean_id
    tables = db.table_names(conn)
    current_id = clean_id
    visited: set[str] = set()
    for _ in range(5):
        if current_id in visited:
            break
        visited.add(current_id)
        next_id = None
        if "video_duplicate_decisions" in tables:
            row = conn.execute(
                "SELECT keeper_id FROM video_duplicate_decisions WHERE loser_id = %s AND state = 'active'",
                (current_id,),
            ).fetchone()
            if row and row[0] and str(row[0]) != current_id:
                next_id = str(row[0])
        if not next_id and "video_sources" in tables:
            row = conn.execute(
                "SELECT video_id FROM video_sources WHERE source_id = %s AND video_id != %s AND is_active = 1",
                (current_id, current_id),
            ).fetchone()
            if row and row[0] and str(row[0]) != current_id:
                next_id = str(row[0])
        if next_id:
            current_id = next_id
        else:
            break
    return current_id


def _get_video_alias_ids(conn: db.Connection, requested_id: str) -> tuple[str, list[str]]:
    """Return canonical ID and all associated alias IDs (keeper, loser, sources)."""
    canonical_id = _resolve_canonical_video_id(conn, requested_id)
    alias_ids = {canonical_id, requested_id.strip()}
    tables = db.table_names(conn)
    if "video_duplicate_decisions" in tables:
        rows = conn.execute(
            "SELECT loser_id FROM video_duplicate_decisions WHERE keeper_id = %s AND state = 'active'",
            (canonical_id,),
        ).fetchall()
        for r in rows:
            if r and r[0]:
                alias_ids.add(str(r[0]))
    if "video_sources" in tables:
        rows = conn.execute(
            "SELECT source_id FROM video_sources WHERE video_id = %s AND is_active = 1",
            (canonical_id,),
        ).fetchall()
        for r in rows:
            if r and r[0]:
                alias_ids.add(str(r[0]))
    return canonical_id, list(alias_ids)


def _validate_progress_video_id(video_id: Any) -> str:
    clean_id = _clean_str_filter(video_id, "video_id", max_len=255)
    if not clean_id or "\x00" in clean_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid video_id parameter.",
        )
    return clean_id


@router.get("/api/v1/user/watch/{video_id:path}")
@router.get("/api/v1/me/progress/{video_id:path}")
async def get_user_watch_progress(request: Request, video_id: str) -> JSONResponse:
    """Fetch authenticated owner-scoped watch progress for a video, resolving canonical duplicate aliases."""
    try:
        token = _extract_bearer_token(request)
        clean_id = _validate_progress_video_id(video_id)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            try:
                session = _verify_client_session(conn, token)
            except AuthenticationFailed as exc:
                return _api_response(
                    {"error": str(exc)},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    cache_control="private, no-store",
                )
            user_id = session["user_id"]
            canonical_id, alias_ids = _get_video_alias_ids(conn, clean_id)

            placeholders = ", ".join(["%s"] * len(alias_ids))
            query = (
                f"SELECT video_id, position_seconds, duration_seconds, played_percentage, is_played, last_watched_at "
                f"FROM client_user_watch_progress "
                f"WHERE user_id = %s AND video_id IN ({placeholders}) "
                f"ORDER BY last_watched_at DESC, played_percentage DESC "
                f"LIMIT 1"
            )
            row = conn.execute(query, [user_id] + alias_ids).fetchone()
            if not row:
                return _api_response(
                    {
                        "type": "https://adulthub.media/errors/not-found",
                        "title": "Watch Progress Not Found",
                        "status": 404,
                        "detail": f"No watch progress recorded for video {clean_id}.",
                        "code": "NOT_FOUND",
                    },
                    status_code=status.HTTP_404_NOT_FOUND,
                    cache_control="private, no-store",
                )

            last_watched = row[5]
            if isinstance(last_watched, (int, float)):
                last_watched_str = datetime.datetime.fromtimestamp(last_watched, datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            elif hasattr(last_watched, "isoformat"):
                last_watched_str = last_watched.isoformat().replace("+00:00", "Z")
            else:
                last_watched_str = str(last_watched)
            if not last_watched_str.endswith("Z") and "+" not in last_watched_str[10:]:
                last_watched_str += "Z"

            dto = WatchProgressDTO(
                video_id=canonical_id,
                position_seconds=int(row[1]),
                duration_seconds=int(row[2]),
                played_percentage=float(row[3]),
                is_played=bool(row[4]),
                last_watched_at=last_watched_str,
            )
            return _api_response(dto.to_dict(), status_code=status.HTTP_200_OK, cache_control="private, no-store")
        finally:
            conn.close()

    return await _run_in_thread(_execute)


def _progress_field_error(pos: Any, dur: Any, is_played: Any, pct: Any) -> JSONResponse | None:
    """Return a 400 response for the first invalid watch progress field, or None when every field is valid."""
    if not isinstance(pos, int) or isinstance(pos, bool) or pos < 0:
        return _api_response(
            {"error": "Field 'position_seconds' must be a non-negative integer."},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    if not isinstance(dur, int) or isinstance(dur, bool) or dur < 0:
        return _api_response(
            {"error": "Field 'duration_seconds' must be a non-negative integer."},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    if not isinstance(is_played, bool):
        return _api_response(
            {"error": "Field 'is_played' must be a boolean."},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    if pct is not None:
        if not isinstance(pct, (int, float)) or isinstance(pct, bool) or pct < 0.0 or pct > 100.0:
            return _api_response(
                {"error": "Field 'played_percentage' must be a number between 0.0 and 100.0."},
                status_code=status.HTTP_400_BAD_REQUEST,
                cache_control="private, no-store",
            )
    return None


def _upsert_user_watch_progress(
    request: Request,
    token: str,
    clean_id: str,
    pos: int,
    dur: int,
    played_percentage: float,
    is_played: bool,
    last_watched_at: datetime.datetime,
) -> JSONResponse:
    """Upsert watch progress for the session owner under the canonical video ID."""
    conn = _db_connection(request)
    try:
        try:
            session = _verify_client_session(conn, token)
        except AuthenticationFailed as exc:
            return _api_response(
                {"error": str(exc)},
                status_code=status.HTTP_401_UNAUTHORIZED,
                cache_control="private, no-store",
            )
        user_id = session["user_id"]
        canonical_id = _resolve_canonical_video_id(conn, clean_id)

        conn.execute(
            "INSERT INTO client_user_watch_progress ("
            "user_id, video_id, position_seconds, duration_seconds, played_percentage, is_played, last_watched_at, created_at, updated_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) "
            "ON CONFLICT (user_id, video_id) DO UPDATE SET "
            "position_seconds = EXCLUDED.position_seconds, "
            "duration_seconds = EXCLUDED.duration_seconds, "
            "played_percentage = EXCLUDED.played_percentage, "
            "is_played = EXCLUDED.is_played, "
            "last_watched_at = EXCLUDED.last_watched_at, "
            "updated_at = CURRENT_TIMESTAMP",
            (user_id, canonical_id, pos, dur, played_percentage, is_played, last_watched_at),
        )
        conn.commit()
        return _api_response({"ok": True}, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    finally:
        conn.close()


@router.put("/api/v1/user/watch/{video_id:path}")
@router.put("/api/v1/me/progress/{video_id:path}")
async def put_user_watch_progress(request: Request, video_id: str) -> JSONResponse:
    """Upsert authenticated owner-scoped watch progress for a video, resolving canonical duplicate aliases."""
    try:
        token = _extract_bearer_token(request)
        clean_id = _validate_progress_video_id(video_id)
    except (AuthenticationFailed, BoundedValidationError, HTTPException) as exc:
        return _auth_validation_error_response(exc)

    body_dict, err_resp = await _parse_json_object(request)
    if err_resp is not None:
        return err_resp
    assert body_dict is not None

    pos = body_dict.get("position_seconds")
    dur = body_dict.get("duration_seconds")
    is_played = body_dict.get("is_played")
    field_err = _progress_field_error(pos, dur, is_played, body_dict.get("played_percentage"))
    if field_err is not None:
        return field_err
    played_percentage = round(min(100.0, max(0.0, (pos / dur) * 100.0)), 2) if dur > 0 else 0.0

    if "last_watched_at" in body_dict and body_dict["last_watched_at"] is not None:
        raw_stamp = body_dict["last_watched_at"]
        if not isinstance(raw_stamp, str):
            return _api_response(
                {"error": "Field 'last_watched_at' must be an ISO 8601 string."},
                status_code=status.HTTP_400_BAD_REQUEST,
                cache_control="private, no-store",
            )
        try:
            last_watched_at = datetime.datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
        except Exception:
            return _api_response(
                {"error": "Invalid 'last_watched_at' datetime format."},
                status_code=status.HTTP_400_BAD_REQUEST,
                cache_control="private, no-store",
            )
    else:
        last_watched_at = datetime.datetime.now(datetime.timezone.utc)

    return await _run_in_thread(
        _upsert_user_watch_progress,
        request,
        token,
        clean_id,
        pos,
        dur,
        played_percentage,
        is_played,
        last_watched_at,
    )


@router.delete("/api/v1/user/watch/{video_id:path}")
@router.delete("/api/v1/me/progress/{video_id:path}")
async def delete_user_watch_progress(request: Request, video_id: str) -> JSONResponse:
    """Clear watch progress for a video and any active duplicate aliases."""
    try:
        token = _extract_bearer_token(request)
        clean_id = _validate_progress_video_id(video_id)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            try:
                session = _verify_client_session(conn, token)
            except AuthenticationFailed as exc:
                return _api_response(
                    {"error": str(exc)},
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    cache_control="private, no-store",
                )
            user_id = session["user_id"]
            canonical_id, alias_ids = _get_video_alias_ids(conn, clean_id)
            placeholders = ", ".join(["%s"] * len(alias_ids))
            conn.execute(
                f"DELETE FROM client_user_watch_progress WHERE user_id = %s AND video_id IN ({placeholders})",
                [user_id] + alias_ids,
            )
            conn.commit()
            return _api_response({"deleted": True}, status_code=status.HTTP_200_OK, cache_control="private, no-store")
        finally:
            conn.close()

    return await _run_in_thread(_execute)


# ---------------------------------------------------------------------------
# Favorites Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/v1/user/favorites")
async def get_user_favorites(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    sort: str = Query("latest"),
    direction: str | None = Query(None),
) -> JSONResponse:
    """List authenticated user favorites with deterministic pagination and canonical alias resolution."""
    if sort not in {"latest", "title", "duration"}:
        return _api_response(
            {"error": "Invalid sort parameter. Must be 'latest', 'title', or 'duration'."},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    clean_direction = direction.lower().strip() if direction is not None else None
    if clean_direction is not None and clean_direction not in {"asc", "desc"}:
        return _api_response(
            {"error": "Invalid direction parameter. Must be 'asc' or 'desc'."},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    try:
        token = _extract_bearer_token(request)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            session = _verify_client_session(conn, token)
            user_id = session["user_id"]
            result = query_catalog_videos(
                conn,
                user_id=user_id,
                is_favorite=True,
                sort=sort,
                direction=clean_direction,
                offset=offset,
                limit=limit,
            )
            return result.to_dict()
        finally:
            conn.close()

    try:
        data = await _run_in_thread(_execute)
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error querying user favorites: %s", exc)
        return _api_response(
            {"error": "Failed to query user favorites"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


@router.get("/api/v1/user/favorites/{video_id:path}")
async def get_user_favorite_status(request: Request, video_id: str) -> JSONResponse:
    """Check if video is in authenticated user favorites, resolving canonical duplicate aliases."""
    try:
        token = _extract_bearer_token(request)
        clean_id = _validate_progress_video_id(video_id)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            session = _verify_client_session(conn, token)
            user_id = session["user_id"]
            canonical_id, alias_ids = _get_video_alias_ids(conn, clean_id)
            tables = db.table_names(conn)
            is_fav = False
            if "client_user_favorites" in tables:
                placeholders = ", ".join(["%s"] * len(alias_ids))
                row = conn.execute(
                    f"SELECT 1 FROM client_user_favorites WHERE user_id = %s AND video_id IN ({placeholders}) LIMIT 1",
                    [user_id] + alias_ids,
                ).fetchone()
                is_fav = bool(row)
            return {"video_id": canonical_id, "is_favorite": is_fav}
        finally:
            conn.close()

    try:
        data = await _run_in_thread(_execute)
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error querying favorite status: %s", exc)
        return _api_response(
            {"error": "Failed to query favorite status"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


@router.put("/api/v1/user/favorites/{video_id:path}")
@router.post("/api/v1/user/favorites/{video_id:path}")
async def put_user_favorite(request: Request, video_id: str) -> JSONResponse:
    """Add video to authenticated user favorites, resolving canonical duplicate aliases."""
    try:
        token = _extract_bearer_token(request)
        clean_id = _validate_progress_video_id(video_id)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            session = _verify_client_session(conn, token)
            user_id = session["user_id"]
            canonical_id = _resolve_canonical_video_id(conn, clean_id)
            tables = db.table_names(conn)
            # Verify video exists in catalog
            row = conn.execute("SELECT 1 FROM videos WHERE id = %s", (canonical_id,)).fetchone()
            if not row and "video_sources" in tables:
                row = conn.execute(
                    "SELECT 1 FROM video_sources WHERE source_id = %s OR video_id = %s",
                    (canonical_id, canonical_id),
                ).fetchone()
            if not row:
                return _api_response(
                    {
                        "type": "https://adulthub.media/errors/not-found",
                        "title": "Video Not Found",
                        "status": 404,
                        "detail": f"Video '{clean_id}' was not found.",
                        "code": "NOT_FOUND",
                    },
                    status_code=status.HTTP_404_NOT_FOUND,
                    cache_control="private, no-store",
                )

            if "client_user_favorites" in tables:
                conn.execute(
                    "INSERT INTO client_user_favorites (user_id, video_id, created_at) "
                    "VALUES (%s, %s, CURRENT_TIMESTAMP) "
                    "ON CONFLICT (user_id, video_id) DO NOTHING",
                    (user_id, canonical_id),
                )
                conn.commit()
            return {"video_id": canonical_id, "is_favorite": True}
        finally:
            conn.close()

    try:
        data = await _run_in_thread(_execute)
        if isinstance(data, JSONResponse):
            return data
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error adding favorite: %s", exc)
        return _api_response(
            {"error": "Failed to add favorite"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


@router.delete("/api/v1/user/favorites/{video_id:path}")
async def delete_user_favorite(request: Request, video_id: str) -> JSONResponse:
    """Remove video and any duplicate aliases from authenticated user favorites."""
    try:
        token = _extract_bearer_token(request)
        clean_id = _validate_progress_video_id(video_id)
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except (BoundedValidationError, HTTPException) as exc:
        msg = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _api_response(
            {"error": msg},
            status_code=status.HTTP_400_BAD_REQUEST,
            cache_control="private, no-store",
        )

    def _execute():
        conn = _db_connection(request)
        try:
            session = _verify_client_session(conn, token)
            user_id = session["user_id"]
            canonical_id, alias_ids = _get_video_alias_ids(conn, clean_id)
            tables = db.table_names(conn)
            if "client_user_favorites" in tables:
                placeholders = ", ".join(["%s"] * len(alias_ids))
                conn.execute(
                    f"DELETE FROM client_user_favorites WHERE user_id = %s AND video_id IN ({placeholders})",
                    [user_id] + alias_ids,
                )
                conn.commit()
            return {"video_id": canonical_id, "is_favorite": False}
        finally:
            conn.close()

    try:
        data = await _run_in_thread(_execute)
        return _api_response(data, status_code=status.HTTP_200_OK, cache_control="private, no-store")
    except AuthenticationFailed as exc:
        return _api_response(
            {"error": str(exc)},
            status_code=status.HTTP_401_UNAUTHORIZED,
            cache_control="private, no-store",
        )
    except Exception as exc:
        logger.exception("Unexpected error removing favorite: %s", exc)
        return _api_response(
            {"error": "Failed to remove favorite"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            cache_control="private, no-store",
        )


# ---------------------------------------------------------------------------
# Testable Integration Seam & Opt-in Staging
# ---------------------------------------------------------------------------


def create_desktop_integration_app(db_url: str | None = None) -> FastAPI:
    """Create an isolated FastAPI application mounted with the Desktop API slice.

    Provides a testable integration seam without modifying default production resolver routes.
    Explicit db_url scopes database connection to this app instance only.
    Isolated test apps without an explicit db_url are prevented from connecting to production.
    """
    integration_app = FastAPI(title="Adult-Hub Desktop API", version=APP_VERSION)
    integration_app.state.isolated_desktop_app = True
    integration_app.state.db_url = db_url

    @integration_app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request: Request, exc: RequestValidationError):
        resp = await request_validation_exception_handler(request, exc)
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    @integration_app.middleware("http")
    async def _enforce_no_store_on_api_v1(request: Request, call_next):
        response = await call_next(request)
        if (
            request.url.path == "/api/v1"
            or request.url.path.startswith("/api/v1/")
        ):
            if request.url.path != "/api/v1/version":
                response.headers["Cache-Control"] = "private, no-store"
            elif response.status_code >= 400 or "no-store" in response.headers.get("Cache-Control", ""):
                response.headers["Cache-Control"] = "private, no-store"
        return response

    integration_app.include_router(router)
    return integration_app


def mount_desktop_api(app: FastAPI, *, force: bool = False) -> bool:
    """Mount the Desktop API router on an existing FastAPI app if explicitly opted in.

    Returns True if mounted, False otherwise.
    Ensures mounted routes take precedence over legacy catch-all routes like /api/{rest:path}.
    """
    if force or parse_bool(os.getenv("ADULT_HUB_ENABLE_DESKTOP_API", "false")):
        existing_paths = {getattr(r, "path", None) for r in app.router.routes}
        if "/api/v1/version" in existing_paths:
            return True

        catch_all_idx = None
        for i, r in enumerate(app.router.routes):
            if getattr(r, "path", "") in ("/api/{rest:path}", "/api/{rest}"):
                catch_all_idx = i
                break

        before_count = len(app.router.routes)
        app.include_router(router)
        new_routes = app.router.routes[before_count:]

        if catch_all_idx is not None and catch_all_idx < before_count:
            del app.router.routes[before_count:]
            for r in reversed(new_routes):
                app.router.routes.insert(catch_all_idx, r)
        return True
    return False
