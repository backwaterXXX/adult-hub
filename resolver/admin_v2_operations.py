"""Preview and execute explicit-list Admin v2 operations."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Query, Request
from admin_v2_errors import V2ErrorRoute

logger = logging.getLogger(__name__)


MAX_OPERATION_REQUEST_BYTES = 16 * 1024
MAX_METADATA_BATCH_REQUEST_BYTES = 256 * 1024


async def _read_operation_body(request: Request):
    max_bytes = MAX_METADATA_BATCH_REQUEST_BYTES
    declared = request.headers.get("content-length")
    try:
        if declared is not None and int(declared) > max_bytes:
            return None, 413
    except ValueError:
        return None, 400
    raw = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw) + len(chunk) > max_bytes:
                return None, 413
            raw.extend(chunk)
        body = json.loads(raw)
    except Exception:
        logger.debug("Ignored exception in _read_operation_body", exc_info=True)
        return None, 413 if len(raw) > MAX_OPERATION_REQUEST_BYTES else 400
    if not isinstance(body, dict):
        return None, 400
    if len(raw) > MAX_OPERATION_REQUEST_BYTES and body.get("type") != "metadata_update_batch":
        return None, 413
    return body, None


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.post("/api/v2/operations/preview")
    async def preview_operation(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        body, error_status = await _read_operation_body(request)
        if error_status:
            return admin.JSONResponse({"error": "Request is too large." if error_status == 413 else "Specify a valid operation request."}, status_code=error_status)
        if not isinstance(body.get("type"), str):
            return admin.JSONResponse({"error": "Specify an operation type and its explicit scope."}, status_code=400)
        try:
            if body["type"] == "quarantine" and set(body) == {"type", "video_ids"}:
                from common.admin_operations import preview_quarantine
                result = await admin.asyncio.to_thread(
                    preview_quarantine, admin.HUB_DB_URL, body.get("video_ids"), str(admin.CONFIG_PATH)
                )
            elif body["type"] == "hide" and set(body) in (
                    {"type", "keeper_id", "loser_ids"},
                    {"type", "keeper_id", "loser_ids", "rules_version"}):
                from common.admin_operations import preview_hide
                rules_version = body.get("rules_version")
                if "rules_version" in body and (type(rules_version) is not int or rules_version < 1):
                    return admin.JSONResponse({"error": "Specify a valid duplicate rules version."}, status_code=400)
                hide_args = [admin.HUB_DB_URL, body.get("keeper_id"), body.get("loser_ids"), str(admin.CONFIG_PATH)]
                if "rules_version" in body:
                    hide_args.append(rules_version)
                result = await admin.asyncio.to_thread(preview_hide, *hide_args)
            elif body["type"] == "metadata_update" and set(body) in (
                    {"type", "video_id", "expected_revision", "changes", "reset_fields"},
                    {"type", "video_id", "expected_revision", "changes", "reset_fields", "proposal_id"}):
                from common.admin_operations import preview_metadata_plan
                if type(body.get("expected_revision")) is not int or body["expected_revision"] < 0:
                    return admin.JSONResponse({"error": "Specify the current metadata revision."}, status_code=400)
                if "proposal_id" in body and (type(body["proposal_id"]) is not int or body["proposal_id"] <= 0):
                    return admin.JSONResponse({"error": "Specify a valid metadata proposal."}, status_code=400)
                result = await admin.asyncio.to_thread(
                    preview_metadata_plan, admin.HUB_DB_URL, body.get("video_id"), body["expected_revision"],
                    body.get("changes"), body.get("reset_fields"), body.get("proposal_id")
                )
            elif body["type"] == "metadata_update_batch" and set(body) == {
                    "type", "video_ids", "changes", "reset_fields"}:
                from common.admin_operations import preview_metadata_batch_plan
                result = await admin.asyncio.to_thread(
                    preview_metadata_batch_plan, admin.HUB_DB_URL, body.get("video_ids"),
                    body.get("changes"), body.get("reset_fields")
                )
            elif body["type"] == "performer_merge" and set(body) == {"type", "alias", "canonical"}:
                from common.admin_operations import preview_performer_merge_plan
                result = await admin.asyncio.to_thread(
                    preview_performer_merge_plan, admin.HUB_DB_URL, body.get("alias"), body.get("canonical")
                )
            elif body["type"] == "auto_merge_exact" and set(body) == {"type"}:
                from common.admin_operations import preview_exact_merge_plan
                result = await admin.asyncio.to_thread(
                    preview_exact_merge_plan, admin.HUB_DB_URL, str(admin.CONFIG_PATH)
                )
            elif body["type"] == "restore" and set(body) == {"type", "operation_id"} \
                    and isinstance(body.get("operation_id"), str):
                from common.admin_operations import preview_restore
                result = await admin.asyncio.to_thread(
                    preview_restore, admin.HUB_DB_URL, body["operation_id"]
                )
            elif body["type"] == "orphan_file_quarantine" and set(body) == {"type", "audit_id", "finding_ids"}:
                from common.admin_operations import preview_orphan_file_quarantine
                result = await admin.asyncio.to_thread(
                    preview_orphan_file_quarantine, admin.HUB_DB_URL, body.get("audit_id"), body.get("finding_ids")
                )
            elif body["type"] == "orphan_file_restore" and set(body) == {"type", "operation_id"}:
                from common.admin_operations import preview_orphan_file_restore
                result = await admin.asyncio.to_thread(
                    preview_orphan_file_restore, admin.HUB_DB_URL, body.get("operation_id")
                )
            else:
                return admin.JSONResponse({"error": "This operation scope is invalid or unsupported."}, status_code=400)
        except Exception:
            logger.warning("Request failed in preview_operation", exc_info=True)
            return admin.JSONResponse({"error": "Could not create the operation preview."}, status_code=503)
        if result["outcome"] == "invalid":
            return admin.JSONResponse({"error": result["message"]}, status_code=400)
        if result["outcome"] == "not_found":
            return admin.JSONResponse({"error": result.get("message", "An item in this selection no longer exists.")}, status_code=404)
        if result["outcome"] == "conflict":
            return admin.JSONResponse({"error": result["message"]}, status_code=409)
        if result["outcome"] == "stale":
            return admin.JSONResponse({"error": result.get("message", "Selected data changed. Create a new preview."),
                                       "revision": result.get("revision")}, status_code=409)
        if result["outcome"] == "empty":
            return {"ok": True, "items": [], "message": result["message"]}
        return {"ok": True, **{key: value for key, value in result.items() if key != "outcome"}}

    @router.post("/api/v2/operations/execute")
    async def execute_operation(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        body, error_status = await _read_operation_body(request)
        if error_status:
            return admin.JSONResponse({"error": "Request is too large." if error_status == 413 else "Specify a valid operation request."}, status_code=error_status)
        if set(body) != {"plan_id"} or not isinstance(body.get("plan_id"), str):
            return admin.JSONResponse({"error": "Specify a saved operation plan."}, status_code=400)
        try:
            from common.admin_operations import execute_operation
            result = await admin.asyncio.to_thread(
                execute_operation, admin.HUB_DB_URL, body["plan_id"], str(admin.CONFIG_PATH)
            )
        except Exception:
            logger.warning("Request failed in execute_operation", exc_info=True)
            return admin.JSONResponse({"error": "Could not execute the operation."}, status_code=503)
        if result["outcome"] == "not_found":
            return admin.JSONResponse({"error": "Operation plan not found."}, status_code=404)
        if result["outcome"] == "expired":
            return admin.JSONResponse({"error": "Operation preview expired. Create a new preview."}, status_code=409)
        if result["outcome"] == "stale":
            return admin.JSONResponse({"error": result["message"], "stale_ids": result["stale_ids"]}, status_code=409)
        if result["outcome"] in {"conflict", "invalid"}:
            return admin.JSONResponse({"error": result.get("message", "Operation plan cannot be executed.")}, status_code=409)
        admin._wake_worker("jobs")
        return admin.JSONResponse({"ok": True, **{key: value for key, value in result.items() if key != "outcome"}}, status_code=202)

    @router.get("/api/v2/operations/{plan_id}")
    async def get_operation(request: Request, plan_id: str):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            from common.admin_operations import operation_status
            result = await admin.asyncio.to_thread(operation_status, admin.HUB_DB_URL, plan_id)
        except Exception:
            logger.warning("Request failed in get_operation", exc_info=True)
            return admin.JSONResponse({"error": "Could not load operation status."}, status_code=503)
        if result is None:
            return admin.JSONResponse({"error": "Operation plan not found."}, status_code=404)
        return {"ok": True, "operation": result}

    @router.get("/api/v2/operations")
    async def list_operation_history(
        request: Request,
        offset: int = Query(0, ge=0, le=2_147_483_647),
        limit: int = Query(20, ge=1, le=50),
    ):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            from common.admin_operations import list_operations
            result = await admin.asyncio.to_thread(list_operations, admin.HUB_DB_URL, limit, offset)
        except Exception:
            logger.warning("Request failed in list_operation_history", exc_info=True)
            return admin.JSONResponse({"error": "Could not load operation history."}, status_code=503)
        return {"ok": True, **result}

    return router
