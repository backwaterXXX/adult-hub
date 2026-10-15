"""Admin v2 queue controls and history for generated pHash thumbnails."""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Query, Request
from admin_v2_errors import V2ErrorRoute


def _write_denied(admin, request: Request):
    if not admin.admin_security.write_authorized(
        request, blocked=admin._admin_blocked(request), public=admin.ADMIN_PUBLIC,
        token=admin._write_token(),
    ):
        return admin.JSONResponse({"error": "This panel is read-only or the request origin is invalid."}, status_code=403)
    return None


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/phash-jobs")
    async def list_phash_jobs(
        request: Request,
        state: str = Query("", max_length=20),
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=2_147_483_647),
    ):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        if state not in {"", "active", "queued", "processing", "done", "failed", "cancelled"}:
            return admin.JSONResponse({"error": "Invalid task state filter."}, status_code=400)
        try:
            from common.phash_jobs import list_jobs
            page = await asyncio.to_thread(
                list_jobs, admin.HUB_DB_URL, state=state, limit=limit, offset=offset,
            )
            from common.worker_capabilities import read_worker_capabilities
            worker = read_worker_capabilities(os.getenv("PHASH_WORKER_CAPABILITIES_PATH", "/data/cache/phash-worker-capabilities.json"))
            return {"ok": True, **page, "worker": worker}
        except Exception:
            return admin.JSONResponse({"error": "Could not load pHash task history."}, status_code=503)

    @router.post("/api/v2/phash-jobs/{job_id}/retry")
    async def retry_phash_job(request: Request, job_id: str):
        denied = _write_denied(admin, request)
        if denied is not None:
            return denied
        try:
            from common.phash_jobs import retry_job
            changed = await asyncio.to_thread(retry_job, admin.HUB_DB_URL, job_id)
            if not changed:
                return admin.JSONResponse({"error": "Only failed or cancelled pHash tasks can be retried."}, status_code=409)
            return {"ok": True, "job_id": job_id, "state": "queued"}
        except Exception:
            return admin.JSONResponse({"error": "Could not retry pHash task."}, status_code=503)

    @router.post("/api/v2/phash-jobs/{job_id}/cancel")
    async def cancel_phash_job(request: Request, job_id: str):
        denied = _write_denied(admin, request)
        if denied is not None:
            return denied
        try:
            from common.phash_jobs import cancel_job
            outcome = await asyncio.to_thread(cancel_job, admin.HUB_DB_URL, job_id)
            if outcome == "not_found":
                return admin.JSONResponse({"error": "Task not found."}, status_code=404)
            if outcome == "conflict":
                return admin.JSONResponse({"error": "Only queued or processing pHash tasks can be cancelled."}, status_code=409)
            return {"ok": True, "job_id": job_id, "state": "cancelled"}
        except Exception:
            return admin.JSONResponse({"error": "Could not cancel pHash task."}, status_code=503)

    return router
