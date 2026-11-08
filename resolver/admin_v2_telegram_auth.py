"""Admin v2 Telegram web authentication endpoints."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from admin_v2_errors import V2ErrorRoute
from providers.telegram_account import DEFAULT_ACCOUNT_ID, validate_account_id
from providers.telegram_login import get_telegram_login_manager

logger = logging.getLogger(__name__)


def _json_resp(admin: Any, content: Any, status_code: int = 200) -> Any:
    factory = getattr(admin, "JSONResponse", None) or JSONResponse
    return factory(content, status_code=status_code)


def _validate_endpoint_account(account_id_raw: Any, admin: Any):
    """Validate account_id format for Admin web endpoints."""
    raw_str = str(account_id_raw or DEFAULT_ACCOUNT_ID).strip() or DEFAULT_ACCOUNT_ID
    try:
        valid_id = validate_account_id(raw_str)
    except ValueError as exc:
        return None, _json_resp(
            admin,
            {"ok": False, "code": "invalid_account_id", "error": str(exc)},
            status_code=400,
        )
    return valid_id, None


def _verify_write_access(admin: Any, request: Request):
    """Verify write authorization and CSRF protection for sensitive Telegram auth mutations."""
    if admin._admin_blocked(request):
        return _json_resp(admin, {"error": "Admin is only available locally."}, status_code=403)

    token = admin._write_token()
    if token is None:
        return _json_resp(
            admin,
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
        return _json_resp(admin, {"error": message, "read_only": False}, status_code=403)

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
            return _json_resp(admin, {"error": "Admin is only available locally."}, status_code=403)
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
        return _json_resp(admin,
            {"ok": False, "code": "missing_phone", "error": "Phone number is required."},
            status_code=400,
        )
    res = await manager.start_login(phone, account_id=account)
    return _json_resp(admin, res, status_code=200 if res.get("ok") else 400)


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
        return _json_resp(admin,
            {"ok": False, "code": "missing_code", "error": "Verification code is required."},
            status_code=400,
        )
    res = await manager.submit_code(code, account_id=account)
    if res.get("ok") and res.get("state") == "authorized":
        res["sync_notified"] = _wake_sync_worker_if_possible(admin)
    return _json_resp(admin, res, status_code=200 if res.get("ok") else 400)


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
        return _json_resp(admin,
            {"ok": False, "code": "missing_password", "error": "2FA password is required."},
            status_code=400,
        )
    res = await manager.submit_password(password, account_id=account)
    if res.get("ok") and res.get("state") == "authorized":
        res["sync_notified"] = _wake_sync_worker_if_possible(admin)
    return _json_resp(admin, res, status_code=200 if res.get("ok") else 400)


async def _handle_resend(admin: Any, manager: Any, request: Request) -> Any:
    denied = _verify_write_access(admin, request)
    if denied:
        return denied
    data = await _parse_json_body(request)
    account, err = _validate_endpoint_account(data.get("account_id"), admin)
    if err:
        return err
    res = await manager.resend_code(account_id=account)
    return _json_resp(admin, res, status_code=200 if res.get("ok") else 400)


async def _handle_cancel(admin: Any, manager: Any, request: Request) -> Any:
    denied = _verify_write_access(admin, request)
    if denied:
        return denied
    data = await _parse_json_body(request)
    account, err = _validate_endpoint_account(data.get("account_id"), admin)
    if err:
        return err
    res = await manager.cancel_login(account_id=account)
    return _json_resp(admin, res, status_code=200)


async def _handle_list_accounts(admin: Any, manager: Any, request: Request) -> Any:
    if admin._admin_blocked(request):
        return _json_resp(admin, {"error": "Admin is only available locally."}, status_code=403)

    from providers.telegram_account_store import list_accounts, list_channel_access
    from providers.telegram_router import get_telegram_router

    router = get_telegram_router()
    check_live = request.query_params.get("check_live", "false").lower() in ("true", "1", "yes")

    accounts = list_accounts()
    access_records = list_channel_access()
    access_by_account: dict[str, list[dict]] = {}
    for r in access_records:
        access_by_account.setdefault(r.account_id, []).append(r.to_dict())

    account_list = []
    for acc in accounts:
        live_status = await manager.get_status(acc.account_id, check_live=check_live)
        cooldown_remaining = router.get_cooldown_remaining(acc.account_id)
        effective_status = "cooling_down" if cooldown_remaining > 0 else live_status.get("status", "unknown")
        if not acc.enabled:
            effective_status = "disabled"

        phone_mask = acc.phone_mask or live_status.get("phone_mask", "")
        account_list.append({
            "account_id": acc.account_id,
            "label": acc.label,
            "phone_mask": phone_mask,
            "enabled": acc.enabled,
            "status": effective_status,
            "authorized": bool(live_status.get("authorized", False)),
            "cooldown_seconds": cooldown_remaining,
            "has_session_file": bool(live_status.get("has_session_file", False)),
            "api_configured": bool(live_status.get("api_configured", True)),
            "channels": access_by_account.get(acc.account_id, []),
            "created_at": acc.created_at.isoformat() if acc.created_at else None,
            "updated_at": acc.updated_at.isoformat() if acc.updated_at else None,
        })

    return _json_resp(admin, {"ok": True, "accounts": account_list})


async def _handle_add_account(admin: Any, request: Request) -> Any:
    denied = _verify_write_access(admin, request)
    if denied:
        return denied
    data = await _parse_json_body(request)
    account_raw = data.get("account_id")
    account, err = _validate_endpoint_account(account_raw, admin)
    if err:
        return err

    from providers.telegram_account_store import create_account
    label = str(data.get("label") or "").strip() or account
    record = create_account(account, label=label, enabled=True)
    if not record:
        return _json_resp(admin,
            {"ok": False, "code": "account_exists", "error": f"Account '{account}' already exists."},
            status_code=409,
        )

    return _json_resp(admin, {"ok": True, "account": record.to_dict()}, status_code=201)


async def _handle_update_account(admin: Any, request: Request, account_id: str) -> Any:
    denied = _verify_write_access(admin, request)
    if denied:
        return denied
    account, err = _validate_endpoint_account(account_id, admin)
    if err:
        return err

    from providers.telegram_account_store import get_account, update_account
    existing = get_account(account)
    if not existing:
        return _json_resp(admin,
            {"ok": False, "code": "not_found", "error": f"Account '{account}' not found."},
            status_code=404,
        )

    data = await _parse_json_body(request)
    label = data.get("label")
    enabled = data.get("enabled")
    if enabled is not None:
        if not isinstance(enabled, bool):
            return _json_resp(admin,
                {"ok": False, "code": "invalid_enabled", "error": "Field 'enabled' must be a boolean."},
                status_code=400,
            )
    if label is not None:
        if not isinstance(label, str):
            return _json_resp(admin,
                {"ok": False, "code": "invalid_label", "error": "Field 'label' must be a string."},
                status_code=400,
            )
        label = label.strip() or existing.label

    updated = update_account(account, label=label, enabled=enabled)
    return _json_resp(admin, {"ok": True, "account": updated.to_dict() if updated else existing.to_dict()})


async def _handle_delete_account(admin: Any, request: Request, account_id: str) -> Any:
    denied = _verify_write_access(admin, request)
    if denied:
        return denied
    account, err = _validate_endpoint_account(account_id, admin)
    if err:
        return err

    data = await _parse_json_body(request)
    confirmed = (
        data.get("confirm") is True
        or data.get("confirm_account_id") == account
        or request.query_params.get("confirm", "").lower() in ("true", "1", "yes")
    )
    if not confirmed:
        return _json_resp(admin,
            {
                "ok": False,
                "code": "confirmation_required",
                "error": f"Explicit confirmation is required to delete account '{account}'. Pass confirm: true.",
            },
            status_code=400,
        )

    from providers.telegram_account_store import delete_account
    from providers.telegram_router import get_telegram_router

    router = get_telegram_router()
    await router.reset_account_client(account)
    try:
        deleted = delete_account(account)
    except Exception as exc:
        return _json_resp(admin,
            {"ok": False, "code": "delete_failed", "error": f"Failed to delete account '{account}': {exc}"},
            status_code=500,
        )
    if not deleted:
        return _json_resp(admin,
            {"ok": False, "code": "not_found", "error": f"Account '{account}' not found."},
            status_code=404,
        )
    return _json_resp(admin, {"ok": True, "account_id": account, "deleted": True})


async def _handle_test_account(admin: Any, manager: Any, request: Request, account_id: str) -> Any:
    denied = _verify_write_access(admin, request)
    if denied:
        return denied
    account, err = _validate_endpoint_account(account_id, admin)
    if err:
        return err

    from providers.telegram_config import _get_channels
    from providers.telegram_router import get_telegram_router

    router = get_telegram_router()
    status = await manager.get_status(account, check_live=True)
    channel_results = {}
    if status.get("authorized"):
        configured_channels = _get_channels() or []
        for ch in configured_channels:
            ch_status = await router.verify_channel_access(account, ch)
            channel_results[ch] = ch_status

    return _json_resp(admin, {
        "ok": True,
        "account_id": account,
        "authorized": bool(status.get("authorized", False)),
        "status": status.get("status", "unknown"),
        "channels": channel_results,
    })


async def _handle_channels_access(admin: Any, request: Request) -> Any:
    if admin._admin_blocked(request):
        return _json_resp(admin, {"error": "Admin is only available locally."}, status_code=403)

    from providers.telegram_account_store import list_channel_access
    records = list_channel_access()
    return _json_resp(admin, {"ok": True, "channels": [r.to_dict() for r in records]})


def _register_account_routes(router: APIRouter, admin: Any, manager: Any) -> None:
    @router.get("/api/v2/telegram/accounts")
    async def list_accounts_endpoint(request: Request):
        return await _handle_list_accounts(admin, manager, request)

    @router.post("/api/v2/telegram/accounts")
    async def add_account_endpoint(request: Request):
        return await _handle_add_account(admin, request)

    @router.patch("/api/v2/telegram/accounts/{account_id}")
    async def update_account_endpoint(request: Request, account_id: str):
        return await _handle_update_account(admin, request, account_id)

    @router.delete("/api/v2/telegram/accounts/{account_id}")
    async def delete_account_endpoint(request: Request, account_id: str):
        return await _handle_delete_account(admin, request, account_id)

    @router.post("/api/v2/telegram/accounts/{account_id}/test")
    async def test_account_endpoint(request: Request, account_id: str):
        return await _handle_test_account(admin, manager, request, account_id)

    @router.get("/api/v2/telegram/channels/access")
    async def channels_access_endpoint(request: Request):
        return await _handle_channels_access(admin, request)


def _register_auth_routes(router: APIRouter, admin: Any, manager: Any) -> None:
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


def build_router(admin: Any) -> APIRouter:
    router = APIRouter(route_class=V2ErrorRoute)
    manager = get_telegram_login_manager()
    _register_account_routes(router, admin, manager)
    _register_auth_routes(router, admin, manager)
    return router
