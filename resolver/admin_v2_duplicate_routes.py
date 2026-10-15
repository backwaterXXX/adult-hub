"""Admin v2 routes for duplicate suggestions and decisions."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request


MAX_DUPLICATE_ACTION_BYTES = 8192


def register_duplicate_routes(router: APIRouter, admin) -> None:
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

