"""Admin v2 Telegram web authentication endpoints."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Request
from admin_v2_errors import V2ErrorRoute
from providers.telegram_account import DEFAULT_ACCOUNT_ID, validate_account_id
from providers.telegram_login import get_telegram_login_manager

logger = logging.getLogger(__name__)


def _validate_endpoint_account(account_id_raw: Any, admin: Any):
    """Validate account_id format and restrict current Admin web endpoints to default account."""
    raw_str = str(account_id_raw or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
    try:
        valid_id = validate_account_id(raw_str)
    except ValueError as exc:
        return None, admin.JSONResponse(
            {"ok": False, "code": "invalid_account_id", "error": str(exc)},
            status_code=400,
        )
    if valid_id != DEFAULT_ACCOUNT_ID:
        return None, admin.JSONResponse(
            {
                "ok": False,
                "code": "unsupported_account",
                "error": f"Multi-account support is not yet enabled; only '{DEFAULT_ACCOUNT_ID}' is supported.",
            },
            status_code=400,
        )
    return valid_id, None


def _verify_write_access(admin: Any, request: Request):
    """Verify write authorization and CSRF protection for sensitive Telegram auth mutations."""
    if admin._admin_blocked(request):
        return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)

    token = admin._write_token()
    if token is None:
        return admin.JSONResponse(
            {"error": "Admin write token configuration is invalid.", "read_only": True},
            status_code=403,
        )

    import admin_security

    if not admin_security.write_authorized(
        request,
        blocked=admin._admin_blocked(request),
        public=admin.ADMIN_PUBLIC,
        token=token,
    ):
        message = "Authorization and matching Origin required." if token else "Matching Origin required."
        return admin.JSONResponse({"error": message, "read_only": False}, status_code=403)

    return None


async def _parse_json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.body()
        if not body:
            return {}
        data = json.loads(body.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _wake_sync_worker_if_possible(admin: Any) -> bool:
    if hasattr(admin, "_wake_worker") and callable(admin._wake_worker):
        try:
            admin._wake_worker("sync")
            logger.info("Signalled sync runner wake signal.")
            return True
        except Exception as exc:
            logger.warning("Could not wake sync runner: %s", exc)
            return False
    logger.info("Sync runner wake hook unavailable on admin module.")
    return False


async def _handle_status(admin: Any, manager: Any, request: Request, account_id: str) -> Any:
    if admin._admin_blocked(request):
        client_host = getattr(getattr(request, "client", None), "host", "") or ""
        is_local = client_host in ("127.0.0.1", "::1", "localhost", "testclient", "testserver")
        if not is_local:
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
    account, err = _validate_endpoint_account(account_id, admin)
    if err:
        return err
    check_live_param = request.query_params.get("check_live", "true").lower() in ("true", "1", "yes")
    return await manager.get_status(account, check_live=check_live_param)


async def _handle_start(admin: Any, manager: Any, request: Request) -> Any:
    denied = _verify_write_access(admin, request)
    if denied:
        return denied
    data = await _parse_json_body(request)
    account, err = _validate_endpoint_account(data.get("account_id"), admin)
    if err:
        return err
    phone = str(data.get("phone") or "").strip()
    if not phone:
        return admin.JSONResponse(
            {"ok": False, "code": "missing_phone", "error": "Phone number is required."},
            status_code=400,
        )
    res = await manager.start_login(phone, account_id=account)
    return admin.JSONResponse(res, status_code=200 if res.get("ok") else 400)


async def _handle_code(admin: Any, manager: Any, request: Request) -> Any:
    denied = _verify_write_access(admin, request)
    if denied:
        return denied
    data = await _parse_json_body(request)
    account, err = _validate_endpoint_account(data.get("account_id"), admin)
    if err:
        return err
    code = str(data.get("code") or "").strip()
    if not code:
        return admin.JSONResponse(
            {"ok": False, "code": "missing_code", "error": "Verification code is required."},
            status_code=400,
        )
    res = await manager.submit_code(code, account_id=account)
    if res.get("ok") and res.get("state") == "authorized":
        res["sync_notified"] = _wake_sync_worker_if_possible(admin)
    return admin.JSONResponse(res, status_code=200 if res.get("ok") else 400)


async def _handle_password(admin: Any, manager: Any, request: Request) -> Any:
    denied = _verify_write_access(admin, request)
    if denied:
        return denied
    data = await _parse_json_body(request)
    account, err = _validate_endpoint_account(data.get("account_id"), admin)
    if err:
        return err
    password = data.get("password")
    if password is None or not isinstance(password, str) or len(password) == 0:
        return admin.JSONResponse(
            {"ok": False, "code": "missing_password", "error": "2FA password is required."},
            status_code=400,
        )
    res = await manager.submit_password(password, account_id=account)
    if res.get("ok") and res.get("state") == "authorized":
        res["sync_notified"] = _wake_sync_worker_if_possible(admin)
    return admin.JSONResponse(res, status_code=200 if res.get("ok") else 400)


async def _handle_resend(admin: Any, manager: Any, request: Request) -> Any:
    denied = _verify_write_access(admin, request)
    if denied:
        return denied
    data = await _parse_json_body(request)
    account, err = _validate_endpoint_account(data.get("account_id"), admin)
    if err:
        return err
    res = await manager.resend_code(account_id=account)
    return admin.JSONResponse(res, status_code=200 if res.get("ok") else 400)


async def _handle_cancel(admin: Any, manager: Any, request: Request) -> Any:
    denied = _verify_write_access(admin, request)
    if denied:
        return denied
    data = await _parse_json_body(request)
    account, err = _validate_endpoint_account(data.get("account_id"), admin)
    if err:
        return err
    res = await manager.cancel_login(account_id=account)
    return admin.JSONResponse(res, status_code=200)


def build_router(admin: Any) -> APIRouter:
    router = APIRouter(route_class=V2ErrorRoute)
    manager = get_telegram_login_manager()

    @router.get("/api/v2/telegram/auth/status")
    async def get_status(request: Request, account_id: str = DEFAULT_ACCOUNT_ID):
        return await _handle_status(admin, manager, request, account_id)

    @router.get("/internal/telegram/auth/status")
    async def get_internal_status(request: Request, account_id: str = DEFAULT_ACCOUNT_ID):
        account, err = _validate_endpoint_account(account_id, admin)
        if err:
            return err
        check_live_param = request.query_params.get("check_live", "false").lower() in ("true", "1", "yes")
        return await manager.get_status(account, check_live=check_live_param)

    @router.post("/api/v2/telegram/auth/start")
    async def start_login(request: Request):
        return await _handle_start(admin, manager, request)

    @router.post("/api/v2/telegram/auth/code")
    async def submit_code(request: Request):
        return await _handle_code(admin, manager, request)

    @router.post("/api/v2/telegram/auth/password")
    async def submit_password(request: Request):
        return await _handle_password(admin, manager, request)

    @router.post("/api/v2/telegram/auth/resend")
    async def resend_code(request: Request):
        return await _handle_resend(admin, manager, request)

    @router.post("/api/v2/telegram/auth/cancel")
    async def cancel_login(request: Request):
        return await _handle_cancel(admin, manager, request)

    return router
