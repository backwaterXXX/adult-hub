"""Read-only Admin v2 status for external metadata queues."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from admin_v2_errors import V2ErrorRoute


MAX_METADATA_SCAN_REQUEST_BYTES = 4096


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/metadata/enrichment")
    async def metadata_enrichment(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            from common.config_loader import load_config
            from common.admin_external_metadata import enrichment_status
            config = await admin.asyncio.to_thread(load_config, str(admin.CONFIG_PATH))
            result = await admin.asyncio.to_thread(enrichment_status, admin.HUB_DB_URL)
            result["batch_limits"] = {
                "videos_per_run": int(config.sync.scene_backfill_per_run),
                "pbc_actors_per_run": int(config.sync.pbc_backfill_per_run),
                "theporndb_actors_per_run": int(config.sync.tpdb_backfill_per_run),
                "stashdb_actors_per_run": int(config.sync.tpdb_backfill_per_run),
                "portraits_per_run": int(config.sync.photo_backfill_per_run),
            }
            daily_at = config.sync.daily_at
            result["sync_schedule"] = {
                "mode": "daily" if daily_at else "interval",
                "daily_at": str(daily_at) if daily_at else None,
                "interval_minutes": None if daily_at else int(config.sync.interval_minutes),
                "run_on_startup": bool(config.sync.run_on_startup),
            }
            return {"ok": True, **result}
        except Exception:
            return admin.JSONResponse({"error": "External metadata status is unavailable."}, status_code=503)

    @router.post("/api/v2/metadata/enrichment/scan")
    async def queue_metadata_scan(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        length = request.headers.get("content-length")
        try:
            if length is not None and int(length) > MAX_METADATA_SCAN_REQUEST_BYTES:
                return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
        except ValueError:
            return admin.JSONResponse({"error": "Invalid request size."}, status_code=400)
        raw_body = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw_body) + len(chunk) > MAX_METADATA_SCAN_REQUEST_BYTES:
                    return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
                raw_body.extend(chunk)
            body = json.loads(raw_body)
        except Exception:
            return admin.JSONResponse({"error": "Specify an empty scan request."}, status_code=400)
        if body != {}:
            return admin.JSONResponse({"error": "Specify an empty scan request."}, status_code=400)
        try:
            from video_duplicates import enqueue_job
            from common.job_queue import read_job
            job_id = await admin.asyncio.to_thread(
                enqueue_job, admin.HUB_DB_URL, "metadata_backfill", {}
            )
            try:
                job = await admin.asyncio.to_thread(read_job, admin.HUB_DB_URL, job_id)
            except Exception:
                # Queue acceptance has already committed; a status lookup must
                # not turn that success into a misleading enqueue failure.
                job = None
            state = job.get("state") if isinstance(job, dict) else None
            if state not in {"queued", "processing", "done", "failed", "cancelled"}:
                state = "accepted"
            admin._wake_worker("jobs")
            return admin.JSONResponse(
                {"ok": True, "job_id": job_id, "state": state}, status_code=202
            )
        except Exception:
            return admin.JSONResponse({"error": "Could not queue an external metadata scan."}, status_code=503)

    return router
