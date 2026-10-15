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
            return {"ok": True, "items": items}
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
