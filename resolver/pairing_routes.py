"""FastAPI routes for Adult Hub Direct Play Client API v1 TV pairing.

Enforces:
- Strict Cache-Control: private, no-store on sensitive endpoints
- Zero is_admin exposure in client models
- Rejection of secrets in query parameters to prevent URL leakage in proxy/app access logs
- Strict Admin V2 guard with configured nonempty admin write token (deny if absent, read-only enforcement)
- Correct actual Admin V2 prefix (/admin/api/v2/pairing/approve)
- ASGI trusted-derived peer address only, never manually trusting leftmost forwarded address
- Zero runtime DDL: schema managed exclusively by versioned migrations
- Canonical production DB config with explicit test fixture injection (no TEST_DATABASE_URL fallback in production)
- Offloading sync DB and scrypt execution off async event loop
- Bounded, NUL-free, and JSON object validation
- Multi-dimensional throttling (per-account and per-IP) and zero writes for unknown pairing secrets
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Header, Request, status
from fastapi.responses import JSONResponse

from common import db
from common.client_auth.service import (
    MAX_DEVICE_ID_LEN,
    MAX_DEVICE_NAME_LEN,
    AuthenticationFailed,
    BoundedValidationError,
    PairingAlreadyExchanged,
    PairingNotFoundOrExpired,
    RateLimitExceeded,
    _safe_hash_token,
    _validate_bounds,
    _validate_optional_string,
    approve_pairing,
    initiate_pairing,
    poll_pairing_status,
)
from resolver import admin as admin_module

logger = logging.getLogger("adult_hub.client_auth")

pairing_router = APIRouter(tags=["TV Pairing"])
admin_pairing_router = APIRouter(tags=["Admin Pairing"])

MAX_AUTH_BODY_BYTES: int = 32 * 1024  # 32 KB maximum pre-auth payload size
_TEST_DB_URL: str | None = None


def set_test_db_url(url: str | None) -> None:
    """Explicitly inject a disposable test database URL for testing.

    Production code never falls back to TEST_DATABASE_URL.
    """
    global _TEST_DB_URL
    _TEST_DB_URL = url


def _db_connection(request: Request | None = None) -> db.Connection:
    """Acquire a database connection respecting explicit test fixture injection or canonical production config.

    Never falls back to TEST_DATABASE_URL in production.
    """
    if request is not None and getattr(getattr(request, "app", None), "state", None):
        app_url = getattr(request.app.state, "db_url", None)
        if app_url:
            return db.connect(app_url)
        if getattr(request.app.state, "isolated_desktop_app", False):
            raise RuntimeError("Database connection not configured for isolated test app")
    if _TEST_DB_URL is not None:
        return db.connect(_TEST_DB_URL)
    url = (os.getenv("DATABASE_URL") or os.getenv("STANDALONE_DATABASE_URL") or "").strip()
    if not url:
        url = getattr(admin_module, "HUB_DB_URL", None)
    if url:
        return db.connect(url)
    return db.connect()


async def _run_in_thread(func, *args, **kwargs):
    """Run synchronous blocking DB queries and CPU-bound scrypt hashing off the asyncio event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


def _extract_client_ip(request: Request) -> str:
    """Extract client peer IP using exclusively the ASGI peer address.

    Never manually parses or trusts untrusted or leftmost forwarded headers.
    """
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def _auth_response(data: dict[str, Any], status_code: int = 200) -> JSONResponse:
    """Helper to return JSON response with mandatory private, no-store caching headers."""
    return JSONResponse(
        content=data,
        status_code=status_code,
        headers={"Cache-Control": "private, no-store"},
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
    """Parse request body and guarantee it is a bounded JSON object (dict).

    Rejects non-dict JSON bodies (lists, numbers, strings, booleans, null) with 400 Bad Request.
    """
    try:
        raw_body = await _read_bounded_body(request, max_bytes=MAX_AUTH_BODY_BYTES)
    except BoundedValidationError as exc:
        return None, _auth_response({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
    except Exception:
        return None, _auth_response({"error": "Failed to read request body"}, status_code=status.HTTP_400_BAD_REQUEST)

    if not raw_body or not raw_body.strip():
        return None, _auth_response({"error": "Invalid JSON body"}, status_code=status.HTTP_400_BAD_REQUEST)

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return None, _auth_response({"error": "Invalid JSON body"}, status_code=status.HTTP_400_BAD_REQUEST)
    if not isinstance(data, dict):
        return None, _auth_response(
            {"error": "Request body must be a JSON object"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return data, None


def _admin_write_guard(request: Request) -> JSONResponse | None:
    """Strict Admin V2 write guard.

    Enforces:
    - Admin surface blocked checks
    - Panel read-only enforcement
    - Mandatory nonempty configured admin write token (denies if absent or empty)
    - Strict constant-time Bearer token authorization
    - Never accepts client authentication or Origin only
    """
    if admin_module._admin_blocked(request):
        return JSONResponse(
            {"error": "Admin is only available locally (blocked on the public domain)."},
            status_code=status.HTTP_403_FORBIDDEN,
            headers={"Cache-Control": "private, no-store"},
        )

    caps = admin_module._capabilities()
    read_only_reason = caps.get("read_only_reason")
    if caps.get("read_only") and read_only_reason not in {"worker_unavailable", "worker_incompatible"}:
        return JSONResponse(
            {"error": "Admin panel is read-only.", **caps},
            status_code=status.HTTP_403_FORBIDDEN,
            headers={"Cache-Control": "private, no-store"},
        )

    token = admin_module._write_token()
    if not token or not str(token).strip():
        return JSONResponse(
            {"error": "Admin write token configuration is missing or invalid.", **caps},
            status_code=status.HTTP_403_FORBIDDEN,
            headers={"Cache-Control": "private, no-store"},
        )

    auth_header = request.headers.get("authorization", "")
    scheme, _, supplied = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not supplied.strip():
        return JSONResponse(
            {"error": "Admin write Bearer token required.", **caps},
            status_code=status.HTTP_403_FORBIDDEN,
            headers={"Cache-Control": "private, no-store"},
        )

    if not hmac.compare_digest(supplied.strip().encode("utf-8"), token.strip().encode("utf-8")):
        return JSONResponse(
            {"error": "Invalid admin authorization token.", **caps},
            status_code=status.HTTP_403_FORBIDDEN,
            headers={"Cache-Control": "private, no-store"},
        )

    origin = request.headers.get("origin")
    if origin:
        from resolver import admin_security

        if not admin_security.write_authorized(
            request, blocked=False, public=admin_module.ADMIN_PUBLIC, token=token
        ):
            return JSONResponse(
                {"error": "Authorization and matching Origin required.", **caps},
                status_code=status.HTTP_403_FORBIDDEN,
                headers={"Cache-Control": "private, no-store"},
            )

    return None


async def _parse_optional_json_object(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Parse optional request body and guarantee it is empty or a bounded JSON object (dict)."""
    try:
        raw_body = await _read_bounded_body(request, max_bytes=MAX_AUTH_BODY_BYTES)
    except BoundedValidationError as exc:
        return None, _auth_response({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
    except Exception:
        return None, _auth_response({"error": "Failed to read request body"}, status_code=status.HTTP_400_BAD_REQUEST)

    if not raw_body or not raw_body.strip():
        return {}, None

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return None, _auth_response({"error": "Invalid JSON body"}, status_code=status.HTTP_400_BAD_REQUEST)
    if not isinstance(payload, dict):
        return None, _auth_response({"error": "Request body must be a JSON object"}, status_code=status.HTTP_400_BAD_REQUEST)
    return payload, None


# --- TV Device Pairing Flow ---

@pairing_router.post("/api/v1/auth/pair/init")
async def post_pair_init(request: Request) -> JSONResponse:
    """Initiate a 10-foot TV pairing session, generating a 6-digit code and private secret."""
    if request.query_params:
        return _auth_response(
            {"error": "Query parameters are strictly prohibited on authentication endpoints."},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    payload, err_resp = await _parse_optional_json_object(request)
    if err_resp:
        return err_resp

    device_name = payload.get("device_name", "")
    device_id = payload.get("device_id", "")
    client_ip = _extract_client_ip(request)

    try:
        device_name = _validate_optional_string(device_name, "device_name", MAX_DEVICE_NAME_LEN)
        device_id = _validate_optional_string(device_id, "device_id", MAX_DEVICE_ID_LEN)
    except BoundedValidationError as exc:
        return _auth_response({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)

    def _execute():
        conn = _db_connection(request)
        try:
            return initiate_pairing(
                conn,
                device_name=device_name,
                device_id=device_id,
                client_ip=client_ip,
            )
        finally:
            conn.close()

    try:
        result = await _run_in_thread(_execute)
        return _auth_response(result, status_code=status.HTTP_200_OK)
    except RateLimitExceeded as exc:
        return _auth_response({"error": str(exc)}, status_code=status.HTTP_429_TOO_MANY_REQUESTS)
    except BoundedValidationError as exc:
        return _auth_response({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:
        logger.exception("Unexpected error during pair init: %s", exc)
        return _auth_response({"error": "Pairing initialization failed"}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


async def _read_pair_secret(request: Request, authorization: str | None) -> tuple[str, JSONResponse | None]:
    """Read and validate the pairing secret from the Authorization header or the JSON body.

    Returns (secret, None) on success, or ("", error response) when the request is rejected.
    """
    secret = ""
    if authorization and authorization.lower().startswith("bearer "):
        secret = authorization[7:].strip()
    else:
        payload, err_resp = await _parse_optional_json_object(request)
        if err_resp:
            return "", err_resp
        secret = payload.get("secret", "")

    if not isinstance(secret, str) or not secret:
        return "", _auth_response(
            {"error": "Pairing secret must be provided in Authorization header or JSON body"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    try:
        _safe_hash_token(secret, name="secret")
    except BoundedValidationError as exc:
        return "", _auth_response({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
    return secret, None


@pairing_router.post("/api/v1/auth/pair/status")
async def post_pair_status(
    request: Request,
    authorization: str | None = Header(None),
) -> JSONResponse:
    """Poll TV pairing status. Query parameter transmission of secret is strictly forbidden."""
    if request.query_params:
        return _auth_response(
            {"error": "Transmission of secrets or parameters in query parameters is strictly prohibited"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    secret, err_resp = await _read_pair_secret(request, authorization)
    if err_resp:
        return err_resp

    client_ip = _extract_client_ip(request)

    def _execute():
        conn = _db_connection(request)
        try:
            return poll_pairing_status(conn, secret=secret, client_ip=client_ip)
        finally:
            conn.close()

    try:
        result = await _run_in_thread(_execute)
        return _auth_response(result, status_code=status.HTTP_200_OK)
    except RateLimitExceeded as exc:
        return _auth_response({"error": str(exc)}, status_code=status.HTTP_429_TOO_MANY_REQUESTS)
    except PairingAlreadyExchanged as exc:
        return _auth_response({"error": str(exc)}, status_code=status.HTTP_401_UNAUTHORIZED)
    except PairingNotFoundOrExpired as exc:
        return _auth_response({"error": str(exc)}, status_code=status.HTTP_404_NOT_FOUND)
    except (AuthenticationFailed, BoundedValidationError) as exc:
        return _auth_response({"error": str(exc)}, status_code=status.HTTP_401_UNAUTHORIZED)
    except Exception as exc:
        logger.exception("Unexpected error during pair status poll: %s", exc)
        return _auth_response({"error": "Pairing poll failed"}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


# --- Out-of-band Admin V2 Pairing Approval ---

def _validate_admin_pairing_request(pairing_code: str, user_id) -> JSONResponse | None:
    """Return a 400 response when the approval inputs fail bounds checks, otherwise None."""
    try:
        _validate_bounds(pairing_code, "pairing_code", 1, 32)
        if user_id is not None:
            _validate_bounds(str(user_id), "user_id", 1, 64)
    except BoundedValidationError as exc:
        return _auth_response({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
    return None


async def _handle_admin_pairing_approval(request: Request) -> JSONResponse:
    """Internal handler for Admin V2 pairing approval."""
    denied = _admin_write_guard(request)
    if denied:
        return denied

    payload, err_resp = await _parse_json_object(request)
    if err_resp:
        return err_resp

    pairing_code = payload.get("pairing_code", "")
    user_id = payload.get("user_id")

    if not pairing_code:
        return _auth_response({"error": "pairing_code is required"}, status_code=status.HTTP_400_BAD_REQUEST)

    invalid_request = _validate_admin_pairing_request(pairing_code, user_id)
    if invalid_request is not None:
        return invalid_request

    def _execute():
        conn = _db_connection(request)
        try:
            return approve_pairing(conn, pairing_code=pairing_code, approver_user_id=user_id)
        finally:
            conn.close()

    try:
        result = await _run_in_thread(_execute)
        return _auth_response(result, status_code=status.HTTP_200_OK)
    except PairingNotFoundOrExpired as exc:
        return _auth_response({"error": str(exc)}, status_code=status.HTTP_404_NOT_FOUND)
    except AuthenticationFailed as exc:
        return _auth_response({"error": str(exc)}, status_code=status.HTTP_403_FORBIDDEN)
    except (ValueError, BoundedValidationError) as exc:
        return _auth_response({"error": str(exc)}, status_code=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:
        logger.exception("Unexpected error during admin pair approval: %s", exc)
        return _auth_response({"error": "Pairing approval failed"}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@admin_pairing_router.post("/admin/api/v2/pairing/approve")
async def post_admin_pairing_approve(request: Request) -> JSONResponse:
    """Approve TV pairing session out-of-band under the correct Admin V2 route prefix."""
    return await _handle_admin_pairing_approval(request)
