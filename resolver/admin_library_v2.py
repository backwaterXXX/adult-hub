"""Read-only catalog endpoints for the next-generation admin interface.

The admin module is injected into :func:`build_router` so access checks and
patch points stay attached to whichever import path loaded the admin app.
This module intentionally performs no migrations or writes during reads.
"""

from __future__ import annotations

import os  # noqa: F401
import re

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse
from common import db  # noqa: F401
from admin_v2_errors import V2ErrorRoute
from admin_catalog_serialization import _channel_id, _local_file_status, _safe_thumbnail, _serialize_video  # noqa: F401
from admin_catalog_pagination import (
    SORTS as _SORTS,
    decode_catalog_cursor as _decode_catalog_cursor,
)

__all__ = ["_channel_id", "_local_file_status", "_safe_thumbnail", "_serialize_video", "build_router", "db", "os"]


from admin_catalog_queries import (
    CatalogUnavailable,
    _MISSING_FIELDS,
    _VIDEO_OPTIONAL_COLUMNS,  # noqa: F401 - compatibility alias used by existing callers
    _availability_clauses,  # noqa: F401 - compatibility alias
    _cursor_condition,  # noqa: F401 - compatibility alias
    _cursor_value_expression,  # noqa: F401 - compatibility alias
    _effective_text,  # noqa: F401 - compatibility alias
    _effective_value,  # noqa: F401 - compatibility alias
    _encode_catalog_cursor,  # noqa: F401 - compatibility alias
    _get_video,
    _get_video_artwork_path,
    _get_target_artwork_path,
    _missing_clause,  # noqa: F401 - compatibility alias
    _missing_effective_clause,  # noqa: F401 - compatibility alias
    _reverse_sort_clause,  # noqa: F401 - compatibility alias
    _run_catalog_query,
    _select_video_fields,  # noqa: F401 - compatibility alias
    _sort_clause,  # noqa: F401 - compatibility alias
    _sort_value_expression,  # noqa: F401 - compatibility alias
    _tables_and_columns,  # noqa: F401 - compatibility alias
)


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/library/videos")
    async def admin_v2_library_videos(
        request: Request,
        q: str = "",
        channel_id: str = "",
        studio: str = "",
        performer: str = "",
        tag: str = "",
        availability: str = "all",
        missing: str = "",
        sort: str = "last_seen",
        offset: str = "0",
        limit: str = "50",
        exact_total: str = "true",
        cursor: str = "",
        cursor_direction: str = "after",
    ):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally"}, status_code=403)
        try:
            page_offset = int(offset)
            page_limit = int(limit)
            missing_fields = [v.strip() for v in missing.split(",") if v.strip()]
            if page_offset < 0 or page_limit < 1:
                raise ValueError
            if exact_total not in {"true", "false"}:
                raise ValueError
            if cursor_direction not in {"after", "before"}:
                raise ValueError
            if not cursor and cursor_direction != "after":
                raise ValueError
            if availability not in {"all", "available", "unavailable", "unknown"}:
                raise ValueError
            if sort not in _SORTS:
                raise ValueError
            cursor_data = _decode_catalog_cursor(cursor, sort) if cursor else None
            if len(q) > 200 or len(studio) > 200 or len(performer) > 200 or len(tag) > 200:
                raise ValueError
            if channel_id and not re.fullmatch(r"(?:telegram:)?[-A-Za-z0-9_]+", channel_id):
                raise ValueError
            if channel_id.startswith("telegram:"):
                channel_id = channel_id[len("telegram:"):]
            if any(field not in _MISSING_FIELDS for field in missing_fields):
                raise ValueError
            if len(missing_fields) != len(set(missing_fields)):
                missing_fields = list(dict.fromkeys(missing_fields))
        except (TypeError, ValueError):
            return admin.JSONResponse({"ok": False, "error": "Invalid catalog filter."}, status_code=400)

        query = {
            "q": q.strip(), "channel_id": channel_id, "studio": studio.strip(),
            "performer": performer.strip(), "tag": tag.strip(),
            "availability": availability, "missing": missing_fields,
            "sort": sort, "offset": page_offset, "limit": min(page_limit, 100),
            "total_exact": exact_total == "true",
            "cursor_data": cursor_data, "cursor_direction": cursor_direction,
        }
        try:
            return await admin.asyncio.to_thread(
                _run_catalog_query, admin.HUB_DB_URL, query, admin.MEDIA_DIR,
            )
        except CatalogUnavailable:
            return admin.JSONResponse({"ok": False, "error": "Catalog database is unavailable."}, status_code=503)

    @router.api_route("/api/v2/library/videos/{video_id:path}/artwork", methods=["GET", "HEAD"])
    async def admin_v2_library_video_artwork(request: Request, video_id: str):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally"}, status_code=403)
        if not video_id or len(video_id) > 1000:
            return admin.JSONResponse({"ok": False, "error": "Invalid video ID."}, status_code=400)
        try:
            artwork_path = await admin.asyncio.to_thread(_get_video_artwork_path, admin.HUB_DB_URL, video_id, admin.MEDIA_DIR)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not access media library."}, status_code=503)
        if artwork_path is None or not artwork_path.is_file():
            try:
                video = await admin.asyncio.to_thread(_get_video, admin.HUB_DB_URL, video_id, admin.MEDIA_DIR)
                if video and video.get("thumbnail"):
                    thumb_str = str(video["thumbnail"]).strip()
                    if thumb_str.startswith("http") and "/admin/api/thumb" not in thumb_str:
                        return RedirectResponse(thumb_str, status_code=307)
            except Exception:
                pass
            return admin.JSONResponse({"ok": False, "error": "Artwork not found."}, status_code=404)
        return FileResponse(
            str(artwork_path),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @router.post("/api/v2/library/videos/{video_id:path}/generate-artwork")
    async def admin_v2_library_video_generate_artwork(request: Request, video_id: str):
        if getattr(admin, "_write_denied", None):
            denied = admin._write_denied(request)
            if denied:
                return denied
        elif admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally"}, status_code=403)
        if not video_id or len(video_id) > 1000:
            return admin.JSONResponse({"ok": False, "error": "Invalid video ID."}, status_code=400)
        offset_seconds = 60
        try:
            body = await request.json()
            if isinstance(body, dict) and "offset_seconds" in body:
                val = int(body["offset_seconds"])
                if val > 0:
                    offset_seconds = val
        except Exception:
            pass

        try:
            dest_path = await admin.asyncio.to_thread(
                _get_target_artwork_path, admin.HUB_DB_URL, video_id, admin.MEDIA_DIR
            )
        except Exception as e:
            return admin.JSONResponse({"ok": False, "error": f"Database lookup failed: {e}"}, status_code=500)

        if dest_path is None:
            return admin.JSONResponse({"ok": False, "error": "Video or target media file not found in library."}, status_code=404)

        from urllib.parse import quote
        stream_url = f"http://127.0.0.1:8090/play/{quote(video_id, safe='')}/stream"
        from common.image_utils import extract_video_frame
        success, err = await admin.asyncio.to_thread(
            extract_video_frame, stream_url, dest_path, offset_seconds=offset_seconds
        )
        if not success:
            return admin.JSONResponse({"ok": False, "error": f"Failed to generate artwork: {err}"}, status_code=500)

        import time
        artwork_url = f"/admin/api/v2/library/videos/{quote(video_id, safe='')}/artwork?t={int(time.time())}"
        return {"ok": True, "artwork_url": artwork_url}


    @router.get("/api/v2/library/videos/{video_id:path}")
    async def admin_v2_library_video(request: Request, video_id: str):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally"}, status_code=403)
        if not video_id or len(video_id) > 1000:
            return admin.JSONResponse({"ok": False, "error": "Invalid video ID."}, status_code=400)
        try:
            video = await admin.asyncio.to_thread(_get_video, admin.HUB_DB_URL, video_id, admin.MEDIA_DIR)
        except CatalogUnavailable:
            return admin.JSONResponse({"ok": False, "error": "Catalog database is unavailable."}, status_code=503)
        if video is None:
            return admin.JSONResponse({"ok": False, "error": "Video not found."}, status_code=404)
        return {"ok": True, "video": video}

    return router
