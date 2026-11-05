"""Admin v2 routes for duplicate suggestions and decisions."""
from __future__ import annotations
import json
import logging

from fastapi import APIRouter, Query, Request

logger = logging.getLogger(__name__)


MAX_DUPLICATE_ACTION_BYTES = 8192


def register_duplicate_routes(router: APIRouter, admin) -> None:
    _route_visual_duplicates(router, admin)
    _route_visual_sheet(router, admin)
    _route_visual_verify(router, admin)
    _route_duplicate_candidates(router, admin)
    _route_linked_oshash_duplicates(router, admin)
    _route_duplicate_action(router, admin)


def _visual_items(rows):
    items = []
    for row in rows:
        item = dict(row)
        if isinstance(item.get("evidence"), str):
            try:
                item["evidence"] = json.loads(item["evidence"])
            except (TypeError, ValueError):
                item["evidence"] = {}
        items.append(item)
    return items


def _visual_duplicates_snapshot(conn, limit):
    state = conn.execute("SELECT report_started_at,automatic_enabled_at FROM video_visual_state WHERE id=1").fetchone()
    rows = conn.execute("""SELECT c.id,c.left_id,c.right_id,c.state,c.evidence,c.updated_at,
                    l.title AS left_title,l.duration AS left_duration,r.title AS right_title,r.duration AS right_duration
                    FROM video_visual_comparisons c JOIN videos l ON l.id=c.left_id
                    JOIN videos r ON r.id=c.right_id
                    WHERE NOT EXISTS (SELECT 1 FROM video_duplicate_dismissals d
                        WHERE d.left_id=c.left_id AND d.right_id=c.right_id AND d.active=1)
                    ORDER BY c.updated_at DESC LIMIT %s""", (limit,)).fetchall()
    total = conn.execute("""SELECT count(*) FROM video_visual_comparisons c WHERE NOT EXISTS
                    (SELECT 1 FROM video_duplicate_dismissals d WHERE d.left_id=c.left_id
                     AND d.right_id=c.right_id AND d.active=1)""").fetchone()[0]
    stats = conn.execute("""SELECT count(*) FILTER (WHERE state='queued') queued,
                    count(*) FILTER (WHERE state='processing') processing,
                    count(*) FILTER (WHERE state='review') review,
                    count(*) FILTER (WHERE state='confirmed') confirmed FROM video_visual_comparisons c
                    WHERE NOT EXISTS (SELECT 1 FROM video_duplicate_dismissals d
                        WHERE d.left_id=c.left_id AND d.right_id=c.right_id AND d.active=1)""").fetchone()
    pending_signatures = conn.execute("""SELECT count(*) FROM video_visual_jobs
                    WHERE kind='signature' AND state IN ('queued','processing')""").fetchone()[0]
    ready_signatures = conn.execute("SELECT count(*) FROM video_visual_signatures WHERE algorithm_version='visual-sequence-v1'").fetchone()[0]
    total_signatures = conn.execute("SELECT count(*) FROM videos WHERE provider='telegram' AND duration>0").fetchone()[0]
    items = _visual_items(rows)
    return {"ok": True, "items": items, "total": total,
            "state": dict(state) if state else None,
            "queue": {**dict(stats), "signatures_pending": pending_signatures,
                      "signatures_ready": ready_signatures, "signatures_total": total_signatures}}


def _visual_duplicates_payload(admin, limit):
    from common import db
    conn = db.connect(admin.HUB_DB_URL)
    try:
        conn.row_factory = db.Record
        return _visual_duplicates_snapshot(conn, limit)
    finally:
        conn.close()


def _route_visual_duplicates(router, admin):
    @router.get("/api/v2/duplicates/visual")
    async def admin_v2_visual_duplicates(request: Request, limit: int = Query(50, ge=1, le=200)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            return _visual_duplicates_payload(admin, limit)
        except Exception:
            logger.warning("Request failed in admin_v2_visual_duplicates", exc_info=True)
            return admin.JSONResponse({"error": "Could not load visual duplicate comparisons."}, status_code=503)


def _route_visual_sheet(router, admin):
    @router.get("/api/v2/duplicates/visual/{comparison_id}/sheet")
    async def admin_v2_visual_sheet(request: Request, comparison_id: str):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            from common import db
            conn = db.connect(admin.HUB_DB_URL)
            try:
                row = conn.execute("SELECT frame_sheet FROM video_visual_comparisons WHERE id=%s", (comparison_id,)).fetchone()
            finally:
                conn.close()
            if not row or not row[0]:
                return admin.JSONResponse({"error": "Frame comparison is not available."}, status_code=404)
            from fastapi.responses import Response
            return Response(content=bytes(row[0]), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
        except Exception:
            logger.warning("Request failed in admin_v2_visual_sheet", exc_info=True)
            return admin.JSONResponse({"error": "Could not load frame comparison."}, status_code=503)


def _route_visual_verify(router, admin):
    @router.post("/api/v2/duplicates/visual/verify")
    async def admin_v2_visual_verify(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
            left, right = body.get("left_id"), body.get("right_id")
            if not isinstance(left, str) or not isinstance(right, str) or left == right or max(len(left), len(right)) > 512:
                return admin.JSONResponse({"error": "Specify two different video IDs."}, status_code=400)
            from common.video_visual_jobs import enqueue_pair
            job_id = await admin.asyncio.to_thread(enqueue_pair, admin.HUB_DB_URL, left, right, priority=5)
            if not job_id:
                return admin.JSONResponse({"error": "Both items must be Telegram videos with known durations."}, status_code=400)
            return {"ok": True, "job_id": job_id, "state": "queued"}
        except Exception:
            logger.warning("Request failed in admin_v2_visual_verify", exc_info=True)
            return admin.JSONResponse({"error": "Could not queue visual comparison."}, status_code=503)


def _route_duplicate_candidates(router, admin):
    @router.get("/api/v2/duplicates")
    async def admin_v2_duplicate_candidates(
        request: Request,
        limit: int = Query(25, ge=1, le=200),
        offset: int = Query(0, ge=0, le=2_147_483_647),
        category: str = Query("", max_length=20),
        include_same_telegram_file: bool = True,
    ):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        if category not in {"", "same_file", "strong", "review"}:
            return admin.JSONResponse({"error": "Invalid category filter."}, status_code=400)
        try:
            from video_duplicates import find_duplicate_candidates
            result = await admin.asyncio.to_thread(
                find_duplicate_candidates, admin.HUB_DB_URL, limit, offset, category,
                include_same_telegram_file=include_same_telegram_file,
            )
            # Preserve the bounded candidate set while omitting backend diagnostics.
            allowed = {"pairs", "total", "scanned", "candidate_limit_reached", "complete", "offset", "limit", "rules_version"}
            return {"ok": True, **{key: value for key, value in result.items() if key in allowed}}
        except Exception:
            logger.warning("Request failed in admin_v2_duplicate_candidates", exc_info=True)
            return admin.JSONResponse({"error": "Could not scan for duplicate videos."}, status_code=503)


def _route_linked_oshash_duplicates(router, admin):
    @router.get("/api/v2/duplicates/linked")
    async def admin_v2_linked_oshash_duplicates(
        request: Request,
        limit: int = Query(25, ge=1, le=200),
        offset: int = Query(0, ge=0, le=2_147_483_647),
    ):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            from video_duplicates import find_active_oshash_merges
            result = await admin.asyncio.to_thread(
                find_active_oshash_merges, admin.HUB_DB_URL, limit, offset
            )
            return {"ok": True, **result}
        except Exception:
            logger.warning("Request failed in admin_v2_linked_oshash_duplicates", exc_info=True)
            return admin.JSONResponse({"error": "Could not load OSHASH-linked videos."}, status_code=503)


async def _read_duplicate_action_body(admin, request):
    """Return (body, rejection). A non-None rejection must be returned to the client."""
    length = request.headers.get("content-length")
    try:
        if length is not None and int(length) > MAX_DUPLICATE_ACTION_BYTES:
            return None, admin.JSONResponse({"error": "Request is too large."}, status_code=413)
    except ValueError:
        return None, admin.JSONResponse({"error": "Invalid request size."}, status_code=400)
    raw_body = bytearray()
    try:
        async for chunk in request.stream():
            if len(raw_body) + len(chunk) > MAX_DUPLICATE_ACTION_BYTES:
                return None, admin.JSONResponse({"error": "Request is too large."}, status_code=413)
            raw_body.extend(chunk)
        body = json.loads(raw_body)
    except Exception:
        body = None
    return body, None


def _duplicate_action_rejection(admin, body):
    """Return a 400 response for a malformed action body, or None when it is valid."""
    if not isinstance(body, dict) or not isinstance(body.get("action"), str):
        return admin.JSONResponse({"error": "Invalid request data."}, status_code=400)
    action = body["action"]
    if action == "dismiss":
        if (set(body) != {"action", "left_id", "right_id"}
                or not isinstance(body.get("left_id"), str)
                or not isinstance(body.get("right_id"), str)
                or len(body["left_id"]) > 512 or len(body["right_id"]) > 512):
            return admin.JSONResponse({"error": "Invalid duplicate selection."}, status_code=400)
    elif action in {"undo", "undismiss"}:
        if (set(body) != {"action", "operation_id"}
                or not isinstance(body.get("operation_id"), str)
                or not 1 <= len(body["operation_id"]) <= 100):
            return admin.JSONResponse({"error": "Invalid decision ID."}, status_code=400)
    else:
        return admin.JSONResponse({"error": "Unknown action."}, status_code=400)
    return None


def _scan_pair_matches(pair, ids):
    members = pair.get("members") or [pair["left"], pair["right"]]
    if pair.get("category") == "Same file":
        member_ids = {member["id"] for member in members}
        dismissed = {tuple(sorted(value)) for value in pair.get("dismissed_pairs", [])}
        return ids[0] in member_ids and ids[1] in member_ids and tuple(ids) not in dismissed
    return ids == sorted((pair["left"]["id"], pair["right"]["id"]))


def _visual_pair_is_active(admin, ids):
    from common import db
    conn = db.connect(admin.HUB_DB_URL)
    try:
        return bool(conn.execute("""SELECT 1 FROM video_visual_comparisons c
                            WHERE c.left_id=%s AND c.right_id=%s AND NOT EXISTS
                            (SELECT 1 FROM video_duplicate_dismissals d WHERE d.left_id=c.left_id
                             AND d.right_id=c.right_id AND d.active=1)""", ids).fetchone())
    finally:
        conn.close()


async def _pair_is_active_suggestion(admin, ids, scan_duplicate_index):
    scan = await admin.asyncio.to_thread(scan_duplicate_index, admin.HUB_DB_URL)
    if any(_scan_pair_matches(pair, ids) for pair in scan["pairs"]):
        return True
    return _visual_pair_is_active(admin, ids)


async def _enqueue_dismiss(admin, body, enqueue_job, scan_duplicate_index):
    """Return (job_id, None) on success or (None, rejection response)."""
    ids = sorted([body["left_id"].strip(), body["right_id"].strip()])
    if not ids[0] or ids[0] == ids[1]:
        return None, admin.JSONResponse({"error": "Specify two different items."}, status_code=400)
    if not await _pair_is_active_suggestion(admin, ids, scan_duplicate_index):
        return None, admin.JSONResponse({"error": "This pair is no longer an active suggestion."}, status_code=409)
    job_id = enqueue_job(admin.HUB_DB_URL, "dismiss", {"left_id": ids[0], "right_id": ids[1]})
    return job_id, None


def _enqueue_undo(admin, action, body, enqueue_job):
    """Return (job_id, None) on success or (None, rejection response)."""
    operation_id = body["operation_id"]
    from common import db
    conn = db.connect(admin.HUB_DB_URL)
    try:
        table, predicate = (("video_duplicate_decisions", "state='active'") if action == "undo"
                            else ("video_duplicate_dismissals", "active=1"))
        exists = conn.execute(
            f"SELECT 1 FROM {table} WHERE operation_id=%s AND {predicate} LIMIT 1",
            (operation_id,),
        ).fetchone()
        if not exists:
            return None, admin.JSONResponse(
                {"error": "This decision has already been undone or does not exist."}, status_code=409)
    finally:
        conn.close()
    job_id = enqueue_job(admin.HUB_DB_URL, action, {"operation_id": operation_id})
    return job_id, None


async def _execute_duplicate_action(admin, body):
    action = body["action"]
    try:
        from video_duplicates import enqueue_job, scan_duplicate_index
        if action == "dismiss":
            job_id, rejection = await _enqueue_dismiss(admin, body, enqueue_job, scan_duplicate_index)
        elif action in {"undo", "undismiss"}:
            job_id, rejection = _enqueue_undo(admin, action, body, enqueue_job)
        else:
            return admin.JSONResponse({"error": "Unknown action."}, status_code=400)
        if rejection is not None:
            return rejection
        admin._wake_worker("jobs")
        return {"ok": True, "job_id": job_id, "state": "queued"}
    except (ValueError, TypeError):
        return admin.JSONResponse({"error": "Invalid request data."}, status_code=400)
    except Exception:
        logger.warning("Request failed in admin_v2_duplicate_action", exc_info=True)
        return admin.JSONResponse({"error": "Failed to save decision."}, status_code=503)


def _route_duplicate_action(router, admin):
    @router.post("/api/v2/duplicates/actions")
    async def admin_v2_duplicate_action(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        body, rejection = await _read_duplicate_action_body(admin, request)
        if rejection is not None:
            return rejection
        rejection = _duplicate_action_rejection(admin, body)
        if rejection is not None:
            return rejection
        return await _execute_duplicate_action(admin, body)
