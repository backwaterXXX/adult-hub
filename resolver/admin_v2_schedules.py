"""Authorized API for durable maintenance schedules."""

from __future__ import annotations

from fastapi import APIRouter, Request
from admin_v2_errors import V2ErrorRoute
from common import admin_schedules


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/schedules")
    async def list_schedules(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            items = await admin.asyncio.to_thread(admin_schedules.list_schedules, admin.HUB_DB_URL)
            from common.config_loader import load_config
            try:
                config = await admin.asyncio.to_thread(load_config, str(admin.CONFIG_PATH))
                sync = config.sync
                cadence = (f"Daily at {sync.daily_at} (sync host local time)" if sync.daily_at
                           else f"Every {sync.interval_minutes} minutes after a sync")
            except Exception:
                cadence = "Configuration unavailable"
            system = [
                {"name": "Library sync", "group": "system", "cadence": cadence, "source": "config.yaml", "details": "Scans enabled configured sources; next and last run are unavailable here.", "status": "unknown"},
                {"name": "System database backup", "group": "system", "cadence": "Host cron; actual timing unavailable", "source": "Host cron", "details": "The host cron configuration is not exposed to this service.", "status": "unknown"},
                {"name": "Backup history cleanup", "group": "dependent", "cadence": "After a successful system backup", "source": "System database backup", "details": "Removes history older than one calendar month.", "status": "unknown"},
                {"name": "StashDB performer catalog", "group": "dependent", "cadence": "At sync, at most once per 24 hours", "source": "Library sync", "details": "Requires configured credentials; skipped when not due.", "status": "unknown"},
                {"name": "Configured source scans", "group": "dependent", "cadence": "During library sync", "source": "Library sync", "details": "Scans enabled sources; Telegram availability checks run after successful channel scans.", "status": "unknown"},
                {"name": "Telegram availability reconciliation", "group": "dependent", "cadence": "During a successful scheduled sync", "source": "Library sync", "details": "Runs for eligible enabled channels after a successful channel scan.", "status": "unknown"},
                {"name": "Scene identification", "group": "dependent", "cadence": "During library sync", "source": "Library sync", "details": "Backfills scene fingerprints when the optional scraper is installed and work is available.", "status": "unknown"},
                {"name": "Performer and scene metadata backfill", "group": "dependent", "cadence": "During library sync", "source": "Library sync", "details": "Title-based performer discovery and PBC, TPDB and StashDB enrichment; subject to configuration and available work.", "status": "unknown"},
                {"name": "Library maintenance", "group": "dependent", "cadence": "During library sync", "source": "Library sync", "details": "Retention, orphan cleanup and stream maintenance depend on source success and safety guards.", "status": "unknown"},
                {"name": "Jellyfin reconciliation and refresh", "group": "dependent", "cadence": "During library sync", "source": "Library sync", "details": "Subject to configuration and available work.", "status": "unknown"},
            ]
            try:
                from admin_v2_status import _sanitized_sync_status, _sync_stale_after_seconds
                status = await admin.asyncio.to_thread(admin._sync_status)
                stale_after = await admin.asyncio.to_thread(_sync_stale_after_seconds, admin.CONFIG_PATH)
                safe = _sanitized_sync_status(status, stale_after)
                system[0]["details"] = safe["status_text"] + "; next run unavailable."
                system[0]["status"] = safe.get("freshness", "unknown")
                system[0]["problem"] = safe.get("last_success") is False or safe.get("freshness") == "stale"
            except Exception:
                system[0]["details"] = "Sync status and next run unavailable."
            return {"ok": True, "items": items, "system_items": system}
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Schedule list is unavailable."}, status_code=503)

    @router.post("/api/v2/schedules")
    async def create_schedule(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {"name", "action", "schedule_type", "interval_minutes", "daily_at", "timezone", "video_ids"}:
                raise ValueError("Invalid schedule fields.")
            if body.get("timezone", "Europe/Warsaw") != "Europe/Warsaw":
                raise ValueError("Schedules use Europe/Warsaw.")
            if body.get("action") != "availability_check" and "video_ids" in body:
                raise ValueError("Only availability schedules accept video IDs.")
            payload = {"video_ids": body.get("video_ids")} if body.get("action") == "availability_check" else {}
            result = await admin.asyncio.to_thread(
                admin_schedules.create_schedule, admin.HUB_DB_URL,
                name=body.get("name"), action=body.get("action"),
                schedule_type=body.get("schedule_type"),
                interval_minutes=body.get("interval_minutes"), daily_at=body.get("daily_at"),
                payload=payload,
            )
            return admin.JSONResponse({"ok": True, **result}, status_code=201)
        except ValueError as exc:
            return admin.JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not create schedule."}, status_code=503)

    @router.post("/api/v2/schedules/{schedule_id}/enabled")
    async def set_schedule_enabled(request: Request, schedule_id: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"enabled", "revision"}:
                raise ValueError("Provide enabled and revision.")
            result = await admin.asyncio.to_thread(
                admin_schedules.set_enabled, admin.HUB_DB_URL, schedule_id,
                enabled=body["enabled"], revision=body["revision"],
            )
            if result["outcome"] == "not_found":
                return admin.JSONResponse({"ok": False, "error": "Schedule not found."}, status_code=404)
            if result["outcome"] == "conflict":
                return admin.JSONResponse({"ok": False, "error": "Schedule changed. Refresh and try again.", **result}, status_code=409)
            return {"ok": True, **result}
        except ValueError as exc:
            return admin.JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not update schedule."}, status_code=503)

    return router
