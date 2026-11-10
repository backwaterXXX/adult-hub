"""Revision-checked Admin v2 metadata preview and durable update task routes."""

from __future__ import annotations
import logging

from fastapi import APIRouter, Request
from resolver.admin_v2_errors import V2ErrorRoute

logger = logging.getLogger(__name__)


async def _parse_body(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = None
    required = {"video_id", "expected_revision", "changes", "reset_fields"}
    if (not isinstance(body, dict) or set(body) != required
            or not isinstance(body["video_id"], str) or not body["video_id"]
            or len(body["video_id"]) > 1000
            or type(body["expected_revision"]) is not int or body["expected_revision"] < 0):
        return None
    return body


async def _make_preview(admin, body):
    from common.video_metadata import MetadataValidationError, preview_metadata_update
    try:
        return await admin.asyncio.to_thread(
            preview_metadata_update,
            admin.HUB_DB_URL,
            body["video_id"],
            body["expected_revision"],
            body["changes"],
            body["reset_fields"],
        )
    except MetadataValidationError as exc:
        return admin.JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        logger.warning("Request failed in _make_preview", exc_info=True)
        return admin.JSONResponse({"error": "Could not review metadata changes."}, status_code=503)


def _public_preview(admin, result):
    if isinstance(result, admin.JSONResponse):
        return result
    if result["outcome"] == "not_found":
        return admin.JSONResponse({"error": "Video not found."}, status_code=404)
    if result["outcome"] == "stale":
        return admin.JSONResponse({"error": "Metadata changed. Reload the latest values before saving.",
                                  "revision": result["revision"]}, status_code=409)
    return {"ok": True, **{key: value for key, value in result.items()
                            if key not in {"outcome", "source_fingerprint"}}}


def _route_preview_metadata(router, admin):
    @router.post("/api/v2/metadata/preview")
    async def preview_metadata(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        body = await _parse_body(request)
        if body is None:
            return admin.JSONResponse({"error": "Specify a video, current revision, changes, and fields to reset."}, status_code=400)
        return _public_preview(admin, await _make_preview(admin, body))

    return preview_metadata


def _route_execute_metadata(router, admin):
    @router.post("/api/v2/metadata/execute")
    async def execute_metadata(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        body = await _parse_body(request)
        if body is None:
            return admin.JSONResponse({"error": "Specify a video, current revision, changes, and fields to reset."}, status_code=400)
        preview = await _make_preview(admin, body)
        if isinstance(preview, admin.JSONResponse):
            return preview
        result = _public_preview(admin, preview)
        if isinstance(result, admin.JSONResponse):
            return result
        payload = {
            "video_id": body["video_id"],
            "expected_revision": body["expected_revision"],
            "changes": result["changes"],
            "reset_fields": result["reset_fields"],
        }
        try:
            from common.job_queue import enqueue_job
            job_id = await admin.asyncio.to_thread(
                enqueue_job, admin.HUB_DB_URL, "metadata_update", payload
            )
        except Exception:
            logger.warning("Request failed in execute_metadata", exc_info=True)
            return admin.JSONResponse({"error": "Could not queue the metadata update."}, status_code=503)
        admin._wake_worker("jobs")
        return admin.JSONResponse({"ok": True, "job_id": job_id, "preview": result}, status_code=202)

    return execute_metadata


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)
    _route_preview_metadata(router, admin)
    _route_execute_metadata(router, admin)
    return router
