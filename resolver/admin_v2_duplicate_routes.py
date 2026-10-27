"""Admin v2 routes for duplicate suggestions and decisions."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request


MAX_DUPLICATE_ACTION_BYTES = 8192


def register_duplicate_routes(router: APIRouter, admin) -> None:
    @router.get("/api/v2/duplicates/visual")
    async def admin_v2_visual_duplicates(request: Request, limit: int = Query(50, ge=1, le=200)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            from common import db
            conn = db.connect(admin.HUB_DB_URL)
            try:
                conn.row_factory = db.Record
                state = conn.execute("SELECT report_started_at,automatic_enabled_at FROM video_visual_state WHERE id=1").fetchone()
                rows = conn.execute("""SELECT c.id,c.left_id,c.right_id,c.state,c.evidence,c.updated_at,
                    l.title AS left_title,l.duration AS left_duration,r.title AS right_title,r.duration AS right_duration
                    FROM video_visual_comparisons c JOIN videos l ON l.id=c.left_id
                    JOIN videos r ON r.id=c.right_id ORDER BY c.updated_at DESC LIMIT %s""", (limit,)).fetchall()
                total = conn.execute("SELECT count(*) FROM video_visual_comparisons").fetchone()[0]
                stats = conn.execute("""SELECT count(*) FILTER (WHERE state='queued') queued,
                    count(*) FILTER (WHERE state='processing') processing,
                    count(*) FILTER (WHERE state='review') review,
                    count(*) FILTER (WHERE state='confirmed') confirmed FROM video_visual_comparisons""").fetchone()
                pending_signatures = conn.execute("""SELECT count(*) FROM video_visual_jobs
                    WHERE kind='signature' AND state IN ('queued','processing')""").fetchone()[0]
                ready_signatures = conn.execute("SELECT count(*) FROM video_visual_signatures WHERE algorithm_version='visual-sequence-v1'").fetchone()[0]
                total_signatures = conn.execute("SELECT count(*) FROM videos WHERE provider='telegram' AND duration>0").fetchone()[0]
                import json
                items = []
                for row in rows:
                    item = dict(row)
                    if isinstance(item.get("evidence"), str):
                        try:
                            item["evidence"] = json.loads(item["evidence"])
                        except (TypeError, ValueError):
                            item["evidence"] = {}
                    items.append(item)
                return {"ok": True, "items": items, "total": total,
                        "state": dict(state) if state else None,
                        "queue": {**dict(stats), "signatures_pending": pending_signatures,
                                  "signatures_ready": ready_signatures, "signatures_total": total_signatures}}
            finally:
                conn.close()
        except Exception:
            return admin.JSONResponse({"error": "Could not load visual duplicate comparisons."}, status_code=503)

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
            return admin.JSONResponse({"error": "Could not load frame comparison."}, status_code=503)

    @router.post("/api/v2/duplicates/visual/verify")
    async def admin_v2_visual_verify(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            import json
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
            return admin.JSONResponse({"error": "Could not queue visual comparison."}, status_code=503)

    @router.get("/api/v2/duplicates")
    async def admin_v2_duplicate_candidates(
        request: Request,
        limit: int = Query(25, ge=1, le=200),
        offset: int = Query(0, ge=0, le=2_147_483_647),
        category: str = Query("", max_length=20),
    ):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        if category not in {"", "same_file", "strong", "review"}:
            return admin.JSONResponse({"error": "Invalid category filter."}, status_code=400)
        try:
            from video_duplicates import find_duplicate_candidates
            result = await admin.asyncio.to_thread(
                find_duplicate_candidates, admin.HUB_DB_URL, limit, offset, category
            )
            # Preserve the bounded candidate set while omitting backend diagnostics.
            allowed = {"pairs", "total", "scanned", "candidate_limit_reached", "complete", "offset", "limit", "rules_version"}
            return {"ok": True, **{key: value for key, value in result.items() if key in allowed}}
        except Exception:
            return admin.JSONResponse({"error": "Could not scan for duplicate videos."}, status_code=503)

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
            return admin.JSONResponse({"error": "Could not load OSHASH-linked videos."}, status_code=503)

    @router.post("/api/v2/duplicates/actions")
    async def admin_v2_duplicate_action(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        length = request.headers.get("content-length")
        try:
            if length is not None and int(length) > MAX_DUPLICATE_ACTION_BYTES:
                return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
        except ValueError:
            return admin.JSONResponse({"error": "Invalid request size."}, status_code=400)
        raw_body = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw_body) + len(chunk) > MAX_DUPLICATE_ACTION_BYTES:
                    return admin.JSONResponse({"error": "Request is too large."}, status_code=413)
                raw_body.extend(chunk)
            import json
            body = json.loads(raw_body)
        except Exception:
            body = None
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
        try:
            from video_duplicates import enqueue_job, scan_duplicate_index
            if action == "dismiss":
                ids = sorted([body["left_id"].strip(), body["right_id"].strip()])
                if not ids[0] or ids[0] == ids[1]:
                    return admin.JSONResponse({"error": "Specify two different items."}, status_code=400)
                scan = await admin.asyncio.to_thread(scan_duplicate_index, admin.HUB_DB_URL)
                found = False
                for pair in scan["pairs"]:
                    members = pair.get("members") or [pair["left"], pair["right"]]
                    if pair.get("category") == "Same file":
                        member_ids = {member["id"] for member in members}
                        dismissed = {tuple(sorted(value)) for value in pair.get("dismissed_pairs", [])}
                        found = ids[0] in member_ids and ids[1] in member_ids and tuple(ids) not in dismissed
                    else:
                        found = ids == sorted((pair["left"]["id"], pair["right"]["id"]))
                    if found:
                        break
                if not found:
                    return admin.JSONResponse({"error": "This pair is no longer an active suggestion."}, status_code=409)
                job_id = enqueue_job(admin.HUB_DB_URL, "dismiss", {"left_id": ids[0], "right_id": ids[1]})
            elif action in {"undo", "undismiss"}:
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
                        return admin.JSONResponse(
                            {"error": "This decision has already been undone or does not exist."}, status_code=409)
                finally:
                    conn.close()
                job_id = enqueue_job(admin.HUB_DB_URL, action, {"operation_id": operation_id})
            else:
                return admin.JSONResponse({"error": "Unknown action."}, status_code=400)
            admin._wake_worker("jobs")
            return {"ok": True, "job_id": job_id, "state": "queued"}
        except (ValueError, TypeError):
            return admin.JSONResponse({"error": "Invalid request data."}, status_code=400)
        except Exception:
            return admin.JSONResponse({"error": "Failed to save decision."}, status_code=503)

