"""Read-only catalog endpoints for the next-generation admin interface.

The admin module is injected into :func:`build_router` so access checks and
patch points stay attached to whichever import path loaded the admin app.
This module intentionally performs no migrations or writes during reads.
"""

from __future__ import annotations

import os
import re
import logging

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse
from common import db
from resolver.admin_v2_errors import V2ErrorRoute
from resolver.admin_catalog_serialization import _channel_id, _local_file_status, _safe_thumbnail, _serialize_video
from resolver.admin_catalog_pagination import (
    SORTS as _SORTS,
    decode_catalog_cursor as _decode_catalog_cursor,
)

__all__ = ["_channel_id", "_local_file_status", "_safe_thumbnail", "_serialize_video", "build_router", "db", "os"]


from resolver.admin_catalog_queries import (
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

logger = logging.getLogger(__name__)


def _parse_catalog_paging(offset: str, limit: str) -> tuple[int, int]:
    page_offset = int(offset)
    page_limit = int(limit)
    if page_offset < 0 or page_limit < 1:
        raise ValueError
    return page_offset, page_limit


def _check_catalog_choices(exact_total, cursor, cursor_direction, availability, sort) -> None:
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


def _check_catalog_text_lengths(q, studio, performer, tag) -> None:
    if len(q) > 200 or len(studio) > 200 or len(performer) > 200 or len(tag) > 200:
        raise ValueError


def _parse_channel_ids(channel_id: str) -> list[str]:
    channel_ids = []
    if channel_id:
        for raw_cid in channel_id.split(","):
            cid = raw_cid.strip()
            if not cid:
                continue
            if not re.fullmatch(r"(?:telegram:)?[-A-Za-z0-9_]+", cid):
                raise ValueError
            if cid.startswith("telegram:"):
                cid = cid[len("telegram:"):]
            channel_ids.append(cid)
    return list(dict.fromkeys(channel_ids))[:50]


def _validate_missing_fields(missing_fields: list[str]) -> list[str]:
    if any(field not in _MISSING_FIELDS for field in missing_fields):
        raise ValueError
    if len(missing_fields) != len(set(missing_fields)):
        return list(dict.fromkeys(missing_fields))
    return missing_fields


def _parse_catalog_request(
    *, q, channel_id, studio, performer, tag, availability, missing, sort,
    offset, limit, exact_total, cursor, cursor_direction,
):
    """Return validated (offset, limit, missing, channel_ids, cursor_data); raise ValueError when invalid."""
    page_offset, page_limit = _parse_catalog_paging(offset, limit)
    missing_fields = [v.strip() for v in missing.split(",") if v.strip()]
    _check_catalog_choices(exact_total, cursor, cursor_direction, availability, sort)
    cursor_data = _decode_catalog_cursor(cursor, sort) if cursor else None
    _check_catalog_text_lengths(q, studio, performer, tag)
    channel_ids = _parse_channel_ids(channel_id)
    missing_fields = _validate_missing_fields(missing_fields)
    return page_offset, page_limit, missing_fields, channel_ids, cursor_data


async def _artwork_redirect(admin, video_id: str):
    """Return a redirect to a remote thumbnail when one is recorded, otherwise None."""
    try:
        video = await admin.asyncio.to_thread(_get_video, admin.HUB_DB_URL, video_id, admin.MEDIA_DIR)
        if video and video.get("thumbnail"):
            thumb_str = str(video["thumbnail"]).strip()
            if thumb_str.startswith("http") and "/admin/api/thumb" not in thumb_str:
                return RedirectResponse(thumb_str, status_code=307)
    except Exception:
        logger.debug("Ignored exception in admin_v2_library_video_artwork", exc_info=True)
    return None


async def _requested_offset_seconds(request: Request) -> int:
    offset_seconds = 60
    try:
        body = await request.json()
        if isinstance(body, dict) and "offset_seconds" in body:
            val = int(body["offset_seconds"])
            if val > 0:
                offset_seconds = val
    except Exception:
        logger.debug("Ignored exception in admin_v2_library_video_generate_artwork", exc_info=True)
    return offset_seconds


def _duration_us_from_body(body: dict):
    if isinstance(body.get("duration_us"), int) and body["duration_us"] > 0:
        return body["duration_us"]
    if isinstance(body.get("duration_seconds"), (int, float)) and body["duration_seconds"] > 0:
        return int(body["duration_seconds"] * 1_000_000)
    return None


async def _read_phash_options(request: Request):
    """Return (force, requested duration in microseconds or None) from the optional JSON body."""
    force = False
    duration_us = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            force = bool(body.get("force", False))
            duration_us = _duration_us_from_body(body)
    except Exception:
        logger.debug("Ignored exception in admin_v2_library_video_generate_phash", exc_info=True)
    return force, duration_us


def _resolve_phash_duration_us(requested_duration_us, row):
    if requested_duration_us is not None:
        return requested_duration_us
    sec = row["duration"]
    if sec is not None and sec > 0:
        return int(sec * 1_000_000)
    return None


def _clear_phash_state(conn, video_id: str) -> None:
    conn.execute("DELETE FROM video_fingerprints WHERE video_id=%s AND algorithm='PHASH'", (video_id,))
    conn.execute("DELETE FROM video_phash_provenance WHERE video_id=%s", (video_id,))
    conn.execute("DELETE FROM video_phash_thumbnails WHERE video_id=%s", (video_id,))
    conn.execute("DELETE FROM video_phash_jobs WHERE video_id=%s", (video_id,))
    conn.commit()


def _phash_job_result(res: dict) -> dict:
    job = res.get("job") or {}
    return {
        "ok": True,
        "job_id": job.get("id"),
        "state": job.get("state"),
        "already_computed": res["outcome"] == "complete",
        "thumbnail_ready": res["outcome"] == "complete",
    }


def _enqueue_phash(admin, video_id: str, requested_duration_us, force: bool) -> dict:
    conn = db.connect(admin.HUB_DB_URL)
    try:
        conn.row_factory = db.Record
        row = conn.execute(
            "SELECT id, provider, duration FROM videos WHERE id=%s", (video_id,)
        ).fetchone()
        if not row or row["provider"] != "telegram":
            return {"status": 404, "error": "Video not found or not a Telegram video."}

        dur_us = _resolve_phash_duration_us(requested_duration_us, row)
        if not dur_us or not (1 <= dur_us <= 86_400_000_000):
            return {"status": 400, "error": "Video duration is required to calculate pHash."}

        from resolver.admin_v2_phash import PHASH_ALGORITHM_VERSION, _SERVER_PROFILE
        from common import phash_jobs

        if force:
            _clear_phash_state(conn, video_id)

        res = phash_jobs.enqueue(
            admin.HUB_DB_URL,
            video_id=video_id,
            duration_us=dur_us,
            algorithm_version=PHASH_ALGORITHM_VERSION,
            decoder_profile=_SERVER_PROFILE,
            user_id="admin",
        )
        if res["outcome"] == "not_found":
            return {"status": 404, "error": "Video not found."}
        if res["outcome"] == "terminal":
            phash_jobs.retry_job(admin.HUB_DB_URL, res["job"]["id"])
            return {
                "ok": True,
                "job_id": res["job"]["id"],
                "state": "queued",
                "already_computed": False,
            }
        return _phash_job_result(res)
    finally:
        conn.close()


def _route_library_videos(router, admin):
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
            page_offset, page_limit, missing_fields, channel_ids, cursor_data = _parse_catalog_request(
                q=q, channel_id=channel_id, studio=studio, performer=performer, tag=tag,
                availability=availability, missing=missing, sort=sort, offset=offset, limit=limit,
                exact_total=exact_total, cursor=cursor, cursor_direction=cursor_direction,
            )
        except (TypeError, ValueError):
            return admin.JSONResponse({"ok": False, "error": "Invalid catalog filter."}, status_code=400)

        query = {
            "q": q.strip(), "channel_id": channel_id, "channel_ids": channel_ids, "studio": studio.strip(),
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

    return admin_v2_library_videos


def _route_library_video_artwork(router, admin):
    @router.api_route("/api/v2/library/videos/{video_id:path}/artwork", methods=["GET", "HEAD"])
    async def admin_v2_library_video_artwork(request: Request, video_id: str):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally"}, status_code=403)
        if not video_id or len(video_id) > 1000:
            return admin.JSONResponse({"ok": False, "error": "Invalid video ID."}, status_code=400)
        try:
            artwork_path = await admin.asyncio.to_thread(_get_video_artwork_path, admin.HUB_DB_URL, video_id, admin.MEDIA_DIR)
        except Exception:
            logger.warning("Request failed in admin_v2_library_video_artwork", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not access media library."}, status_code=503)
        if artwork_path is None or not artwork_path.is_file():
            redirect = await _artwork_redirect(admin, video_id)
            if redirect is not None:
                return redirect
            return admin.JSONResponse({"ok": False, "error": "Artwork not found."}, status_code=404)
        return FileResponse(
            str(artwork_path),
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    return admin_v2_library_video_artwork


def _route_generate_artwork(router, admin):
    @router.post("/api/v2/library/videos/{video_id:path}/generate-artwork")
    async def admin_v2_library_video_generate_artwork(request: Request, video_id: str):
        denied = admin._write_denied(request)
        if denied:
            return denied
        if not video_id or len(video_id) > 1000:
            return admin.JSONResponse({"ok": False, "error": "Invalid video ID."}, status_code=400)
        offset_seconds = await _requested_offset_seconds(request)

        try:
            dest_path = await admin.asyncio.to_thread(
                _get_target_artwork_path, admin.HUB_DB_URL, video_id, admin.MEDIA_DIR
            )
        except Exception as e:
            logger.warning("Request failed in admin_v2_library_video_generate_artwork", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": f"Database lookup failed: {e}"}, status_code=500)

        if dest_path is None:
            return admin.JSONResponse({"ok": False, "error": "Video or target media file not found in library."}, status_code=404)

        try:
            from common.job_queue import enqueue_job
            job_id = await admin.asyncio.to_thread(
                enqueue_job, admin.HUB_DB_URL, "artwork_generate",
                {"video_id": video_id, "offset_seconds": offset_seconds},
            )
        except Exception:
            logger.warning("Request failed in admin_v2_library_video_generate_artwork", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not queue artwork generation."}, status_code=503)
        admin._wake_worker("jobs")
        return admin.JSONResponse({"ok": True, "job_id": job_id, "state": "queued"}, status_code=202)

    return admin_v2_library_video_generate_artwork


def _route_download_artwork(router, admin):
    @router.post("/api/v2/library/videos/{video_id:path}/download-artwork")
    async def admin_v2_library_video_download_artwork(request: Request, video_id: str):
        denied = admin._write_denied(request)
        if denied:
            return denied
        if not video_id or len(video_id) > 1000:
            return admin.JSONResponse({"ok": False, "error": "Invalid video ID."}, status_code=400)
        try:
            from common.job_queue import enqueue_job
            job_id = await admin.asyncio.to_thread(
                enqueue_job, admin.HUB_DB_URL, "artwork_download", {"video_id": video_id},
            )
        except Exception:
            logger.warning("Request failed in admin_v2_library_video_download_artwork", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": "Could not queue the artwork download."}, status_code=503)
        admin._wake_worker("jobs")
        return admin.JSONResponse({"ok": True, "job_id": job_id, "state": "queued"}, status_code=202)

    return admin_v2_library_video_download_artwork


def _route_generate_phash(router, admin):
    @router.post("/api/v2/library/videos/{video_id:path}/generate-phash")
    async def admin_v2_library_video_generate_phash(request: Request, video_id: str):
        denied = admin._write_denied(request)
        if denied:
            return denied
        if not video_id or len(video_id) > 1000:
            return admin.JSONResponse({"ok": False, "error": "Invalid video ID."}, status_code=400)

        force, duration_us = await _read_phash_options(request)

        try:
            result = await admin.asyncio.to_thread(_enqueue_phash, admin, video_id, duration_us, force)
        except Exception as exc:
            logger.warning("Request failed in admin_v2_library_video_generate_phash", exc_info=True)
            return admin.JSONResponse({"ok": False, "error": f"Database operation failed: {exc}"}, status_code=500)

        if "error" in result:
            return admin.JSONResponse({"ok": False, "error": result["error"]}, status_code=result.get("status", 400))
        return result

    return admin_v2_library_video_generate_phash


def _route_library_video(router, admin):
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

    return admin_v2_library_video


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)
    _route_library_videos(router, admin)
    _route_library_video_artwork(router, admin)
    _route_generate_artwork(router, admin)
    _route_download_artwork(router, admin)
    _route_generate_phash(router, admin)
    _route_library_video(router, admin)
    return router
