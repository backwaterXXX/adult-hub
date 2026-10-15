"""Explicit V2 maintenance actions retained from the classic admin panel."""

from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, Request
from admin_v2_errors import V2ErrorRoute


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.post("/api/v2/maintenance/sync")
    async def trigger_sync(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Specify a valid sync request."}, status_code=400)
        if not isinstance(body, dict) or set(body) - {"source", "sync_mode"}:
            return admin.JSONResponse({"ok": False, "error": "Specify a valid sync request."}, status_code=400)
        source_value = body.get("source")
        if source_value is not None and not isinstance(source_value, str):
            return admin.JSONResponse({"ok": False, "error": "Source must be a configured source identifier."}, status_code=400)
        source = str(source_value or "").strip()
        if source_value is not None and not source:
            return admin.JSONResponse({"ok": False, "error": "Source must be a configured source identifier."}, status_code=400)
        sync_mode = str(body.get("sync_mode") or "scheduled").strip()
        if sync_mode not in {"scheduled", "new", "backfill"}:
            return admin.JSONResponse({"ok": False, "error": "Sync mode must be scheduled, new, or backfill."}, status_code=400)
        if sync_mode == "backfill" and (not source or source == "telegram"):
            return admin.JSONResponse({"ok": False, "error": "Choose one Telegram channel for history backfill."}, status_code=400)
        if source:
            try:
                from common.config_loader import load_config

                config = load_config(str(admin.CONFIG_PATH))
                allowed = {item.provider for item in config.get_enabled_sources()}
                telegram = config.get_source("telegram")
                telegram_channel_ids = set()
                if telegram:
                    for channel in telegram.channels:
                        if channel.enabled:
                            telegram_channel_ids.add(channel.id)
                            allowed.add(channel.id)
                            allowed.add(f"telegram:{channel.id}")
                if sync_mode == "backfill" and source not in telegram_channel_ids | {
                        f"telegram:{value}" for value in telegram_channel_ids}:
                    return admin.JSONResponse({"ok": False, "error": "History backfill is available only for Telegram channels."}, status_code=400)
                if source not in allowed:
                    return admin.JSONResponse({"ok": False, "error": "Source is not enabled in configuration."}, status_code=400)
            except Exception:
                return admin.JSONResponse({"ok": False, "error": "Could not validate the source configuration."}, status_code=400)
        try:
            from video_duplicates import enqueue_job

            job_id = await admin.asyncio.to_thread(
                enqueue_job, admin.HUB_DB_URL, "sync",
                {"source": source or None, "sync_mode": sync_mode},
            )
            admin._wake_worker("sync", source=source or None)
            return admin.JSONResponse({"ok": True, "job_id": job_id, "state": "queued",
                                       "source": source or None, "sync_mode": sync_mode}, status_code=202)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not queue the sync."}, status_code=503)

    @router.post("/api/v2/maintenance/cache/clear")
    async def clear_resolver_cache(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        clear = getattr(request.app.state, "admin_cache_clear", None)
        if not callable(clear):
            return admin.JSONResponse(
                {"ok": False, "error": "Cache clearing is unavailable."}, status_code=503
            )
        try:
            count = await admin.asyncio.to_thread(clear)
            return {"ok": True, "cleared": max(0, int(count or 0))}
        except Exception:
            return admin.JSONResponse(
                {"ok": False, "error": "Could not clear the resolver cache."}, status_code=503
            )

    @router.post("/api/v2/maintenance/jellyfin/refresh")
    async def refresh_jellyfin_library(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        url = os.getenv("JELLYFIN_URL", "").strip().rstrip("/")
        api_key = os.getenv("JELLYFIN_API_KEY", "").strip()
        if not url or not api_key:
            return admin.JSONResponse(
                {"ok": False, "error": "Jellyfin refresh is not configured."}, status_code=503
            )
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    f"{url}/Library/Refresh",
                    headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
                )
            if response.status_code not in (200, 204):
                return admin.JSONResponse(
                    {"ok": False, "error": "Jellyfin did not accept the library refresh request."},
                    status_code=502,
                )
            return {"ok": True, "requested": True}
        except Exception:
            return admin.JSONResponse(
                {"ok": False, "error": "Could not request a Jellyfin library refresh."},
                status_code=502,
            )

    @router.post("/api/v2/maintenance/collections/refresh")
    async def refresh_collections(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        url = os.getenv("JELLYFIN_URL", "").strip().rstrip("/")
        api_key = os.getenv("JELLYFIN_API_KEY", "").strip()
        if not url or not api_key:
            return admin.JSONResponse(
                {"ok": False, "error": "Jellyfin collection refresh is not configured."},
                status_code=503,
            )
        try:
            from video_duplicates import enqueue_job

            job_id = await admin.asyncio.to_thread(
                enqueue_job, admin.HUB_DB_URL, "collections_refresh", {}
            )
            admin._wake_worker("jobs")
            return admin.JSONResponse(
                {"ok": True, "queued": True, "job_id": job_id, "state": "queued"},
                status_code=202,
            )
        except Exception:
            return admin.JSONResponse(
                {"ok": False, "error": "Could not queue a collections refresh."}, status_code=503
            )

    return router


__all__ = ["build_router"]
