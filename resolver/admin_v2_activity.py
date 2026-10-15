"""Read-only access to privacy-preserving Admin v2 action history."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from admin_v2_errors import V2ErrorRoute


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/activity")
    async def activity(request: Request, offset: int = Query(0, ge=0, le=2_147_483_647),
                       limit: int = Query(50, ge=1, le=100), method: str = Query(""),
                       outcome: str = Query(""), area: str = Query("")):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            from common.admin_action_events import list_events
            if method.upper() not in {"", "POST", "PUT", "PATCH", "DELETE"}:
                return admin.JSONResponse({"error": "Choose a supported method filter."}, status_code=400)
            if outcome not in {"", "accepted", "completed"}:
                return admin.JSONResponse({"error": "Choose a supported outcome filter."}, status_code=400)
            if area not in {"", "library", "sources", "performers", "quality", "jobs", "system"}:
                return admin.JSONResponse({"error": "Choose a supported section filter."}, status_code=400)
            result = await admin.asyncio.to_thread(
                list_events, admin.HUB_DB_URL, limit=limit, offset=offset,
                method=method, outcome=outcome, area=area,
            )
            return {"ok": True, **result}
        except Exception:
            return admin.JSONResponse({"error": "Admin activity history is unavailable."}, status_code=503)

    @router.get("/api/v2/activity/changes")
    async def changes(request: Request, offset: int = Query(0, ge=0, le=2_147_483_647),
                      limit: int = Query(20, ge=1, le=100), event_type: str = Query(""),
                      object_type: str = Query(""), q: str = Query("", max_length=100)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        conn = None
        try:
            from common import db
            from common.admin_audit_events import list_change_events
            if event_type not in {"", "metadata_update", "source_settings_update", "admin_operation_queued",
                                  "schedule_created", "schedule_enabled", "performer_profile_update"}:
                return admin.JSONResponse({"error": "Choose a supported change type."}, status_code=400)
            if object_type not in {"", "video", "source_configuration", "operation_plan", "schedule", "performer"}:
                return admin.JSONResponse({"error": "Choose a supported object type."}, status_code=400)
            conn = db.connect(admin.HUB_DB_URL)
            return {"ok": True, **list_change_events(
                conn, limit=limit, offset=offset, event_type=event_type,
                object_type=object_type, query=q,
            )}
        except Exception:
            return admin.JSONResponse({"error": "Admin change history is unavailable."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()

    return router
