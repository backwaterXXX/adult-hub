"""Bounded playback availability preview, durable execution, and evidence API."""

from __future__ import annotations

from fastapi import APIRouter, Request
from admin_v2_errors import V2ErrorRoute
from common import db

MAX_ITEMS = 20
_STATUS_LABELS = {
    "available": "A media byte range was served by the resolver playback chain.",
    "unavailable": "The resolver confirmed a missing or non-playable upstream item.",
    "transient_error": "The playback probe failed because of a transient network error.",
    "unknown": "The playback probe was inconclusive; no availability change was made.",
}


def _safe_status(value):
    return value if isinstance(value, str) and value in _STATUS_LABELS else "unknown"


def _parse_ids(body):
    if not isinstance(body, dict) or set(body) != {"video_ids"}:
        return None
    values = body.get("video_ids")
    if (not isinstance(values, list) or not 1 <= len(values) <= MAX_ITEMS
            or any(not isinstance(value, str) or not value or len(value) > 1000 for value in values)
            or len(set(values)) != len(values)):
        return None
    return values


def _preview(db_url, video_ids):
    conn = db.connect(db_url)
    try:
        rows = conn.execute(
            "SELECT v.id,v.title,v.playable,d.video_id IS NOT NULL,latest.status,latest.checked_at "
            "FROM videos v "
            "LEFT JOIN dead_streams d ON d.video_id=v.id "
            "LEFT JOIN LATERAL ("
            "SELECT status,checked_at FROM video_availability_checks "
            "WHERE video_id=v.id ORDER BY checked_at DESC,id DESC LIMIT 1"
            ") latest ON TRUE "
            "WHERE v.id = ANY(%s)",
            (video_ids,),
        ).fetchall()
        rows_by_id = {str(row[0]): row for row in rows}
        items = []
        missing = []
        for video_id in video_ids:
            row = rows_by_id.get(video_id)
            if row is None:
                missing.append(video_id)
                continue
            state = "unavailable" if row[3] or row[2] == 0 else "available" if row[2] == 1 else "unknown"
            items.append({"video_id": row[0], "title": row[1], "current": state,
                          "last_check": _safe_status(row[4]) if row[4] is not None else None,
                          "last_checked_at": row[5].isoformat() if row[5] else None})
        return {"items": items, "missing_count": len(missing), "max_items": MAX_ITEMS}
    finally:
        conn.close()


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.post("/api/v2/availability/preview")
    async def preview(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            body = None
        video_ids = _parse_ids(body)
        if video_ids is None:
            return admin.JSONResponse({"ok": False, "error": f"Choose 1 to {MAX_ITEMS} distinct videos."}, status_code=400)
        try:
            return {"ok": True, **await admin.asyncio.to_thread(_preview, admin.HUB_DB_URL, video_ids)}
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not preview availability checks."}, status_code=503)

    @router.post("/api/v2/availability/execute")
    async def execute(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except Exception:
            body = None
        video_ids = _parse_ids(body)
        if video_ids is None:
            return admin.JSONResponse({"ok": False, "error": f"Choose 1 to {MAX_ITEMS} distinct videos."}, status_code=400)
        try:
            preview = await admin.asyncio.to_thread(_preview, admin.HUB_DB_URL, video_ids)
            if preview["missing_count"]:
                return admin.JSONResponse({"ok": False, "error": "One or more videos no longer exist. Refresh the catalog."}, status_code=409)
            from common.job_queue import enqueue_job
            job_id = await admin.asyncio.to_thread(
                enqueue_job, admin.HUB_DB_URL, "availability_check", {"video_ids": video_ids}
            )
            admin._wake_worker("jobs")
            return admin.JSONResponse({"ok": True, "job_id": job_id, "preview": preview}, status_code=202)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not queue availability check."}, status_code=503)

    @router.get("/api/v2/availability/history")
    async def history(request: Request, video_id: str, limit: int = 10):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        if not video_id or len(video_id) > 1000 or limit < 1 or limit > 50:
            return admin.JSONResponse({"ok": False, "error": "Invalid availability history query."}, status_code=400)
        conn = None
        try:
            conn = db.connect(admin.HUB_DB_URL)
            rows = conn.execute(
                "SELECT status,checked_at,evidence FROM video_availability_checks "
                "WHERE video_id=%s ORDER BY checked_at DESC,id DESC LIMIT %s", (video_id, limit)
            ).fetchall()
            items = []
            for row in rows:
                status = _safe_status(row[0])
                items.append({"status": status, "checked_at": row[1].isoformat(),
                              "evidence": _STATUS_LABELS[status]})
            return {"ok": True, "items": items}
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Availability history is unavailable."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()

    return router
