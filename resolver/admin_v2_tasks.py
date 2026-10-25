"""A safe, paginated projection over the durable Admin and pHash queues."""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, Query, Request
from admin_v2_errors import V2ErrorRoute
from admin_v2_jobs import JOB_ACTIONS


_STATES = {"", "active", "queued", "processing", "done", "failed", "cancelled"}
_PHASH_ERRORS = {
    "attempts_exhausted", "worker_restarted", "video_missing", "file_missing",
    "unsupported_format", "decode_failed", "timeout", "storage_failed",
    "fingerprint_conflict", "generation_failed", "daily_transfer_budget_exhausted",
    "transfer_budget_unavailable", "byte_budget_exhausted", "source_unavailable",
    "invalid_video",
}


def _where(state: str, action: str | None = None, job_id: str | None = None):
    clauses, params = [], []
    if state == "active":
        clauses.append("state IN ('queued','processing')")
    elif state:
        clauses.append("state=%s")
        params.append(state)
    if action:
        clauses.append("action=%s")
        params.append(action)
    if job_id:
        clauses.append("id=%s")
        params.append(job_id)
    return (" WHERE " + " AND ".join(clauses) if clauses else "", params)


def _count(conn, table: str, state: str, action: str | None = None,
           job_id: str | None = None) -> int:
    where, params = _where(state, action, job_id)
    return int(conn.execute(f"SELECT count(*) FROM {table}{where}", params).fetchone()[0])


def _list_tasks(db_url: str | None, *, queue: str, state: str, action: str,
                offset: int, limit: int, job_id: str | None = None) -> dict:
    from admin_services import _job_undo_available, _jobs_connection, _public_job
    from admin_v2_jobs import _safe_job
    from common import db

    conn = _jobs_connection(db_url)
    if conn is None:
        return {"available": False, "tasks": [], "total": None, "active_total": None}
    try:
        tables = db.table_names(conn)
        if "video_phash_jobs" not in tables or "videos" not in tables:
            return {"available": False, "tasks": [], "total": None, "active_total": None}

        include_jobs = queue in {"all", "jobs"} and action != "phash_generation"
        include_phash = queue in {"all", "phash"} and action in {"", "phash_generation"}
        jobs_action = action if include_jobs and action != "phash_generation" else None
        phash_action = None
        job_where, job_params = _where(state, jobs_action, job_id) if include_jobs else (" WHERE false", [])
        phash_where, phash_params = _where(state, phash_action, job_id) if include_phash else (" WHERE false", [])

        jobs_total = _count(conn, "video_duplicate_jobs", state, jobs_action, job_id) if include_jobs else 0
        phash_total = _count(conn, "video_phash_jobs", state, None, job_id) if include_phash else 0
        jobs_active = _count(conn, "video_duplicate_jobs", "active", jobs_action, job_id) if include_jobs else 0
        phash_active = _count(conn, "video_phash_jobs", "active", None, job_id) if include_phash else 0

        branches = []
        if include_jobs:
            branches.append(
                "SELECT 'jobs'::text AS queue,id,created_at AS sort_at "
                f"FROM video_duplicate_jobs{job_where}"
            )
        if include_phash:
            branches.append(
                "SELECT 'phash'::text AS queue,id,created_at AS sort_at "
                f"FROM video_phash_jobs{phash_where}"
            )
        total = jobs_total + phash_total
        if not branches:
            rows = []
        else:
            rows = conn.execute(
                "WITH task_keys AS (" + " UNION ALL ".join(branches) + ") "
                "SELECT queue,id FROM task_keys ORDER BY sort_at DESC,id DESC,queue "
                "LIMIT %s OFFSET %s",
                (*job_params, *phash_params, limit, offset),
            ).fetchall()

        ids = {"jobs": [], "phash": []}
        for row in rows:
            ids[row[0]].append(row[1])

        general = {}
        if ids["jobs"]:
            placeholders = ",".join(["%s"] * len(ids["jobs"]))
            for raw in conn.execute(
                f"SELECT * FROM video_duplicate_jobs WHERE id IN ({placeholders})", ids["jobs"],
            ).fetchall():
                job = _public_job(raw, include_failure_code=True)
                job["undo_available"] = _job_undo_available(conn, job)
                projected = _safe_job(job)
                projected["queue"] = "jobs"
                general[str(projected["id"])] = projected

        phash = {}
        if ids["phash"]:
            placeholders = ",".join(["%s"] * len(ids["phash"]))
            from common.phash_jobs import _processing_time_select, _public_job as phash_public_job
            for raw in conn.execute(
                "SELECT j.*,v.title," + _processing_time_select("j") +
                " FROM video_phash_jobs j JOIN videos v ON v.id=j.video_id "
                f"WHERE j.id IN ({placeholders})", ids["phash"],
            ).fetchall():
                item = phash_public_job(conn, raw)
                error_code = item.get("error_code")
                phash[str(item["id"])] = {
                    "queue": "phash", "id": str(item["id"]),
                    "action": "phash_generation", "state": str(item["state"]),
                    "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
                    "title": item.get("title"), "video_id": str(item["video_id"]),
                    "attempts": int(item.get("attempts") or 0),
                    "processing_time_ms": int(item.get("processing_time_ms") or 0),
                    "thumbnail_ready": item.get("thumbnail_ready") is True,
                    "error_code": error_code if error_code in _PHASH_ERRORS else None,
                    "error_detail": str(item.get("error_detail") or "")[:2000],
                    "stage": str(item["state"]), "events": [], "undo_available": False,
                }

        tasks = [general.get(str(row[1])) if row[0] == "jobs" else phash.get(str(row[1])) for row in rows]
        tasks = [task for task in tasks if task is not None]
        return {
            "available": True, "tasks": tasks, "total": total,
            "active_total": jobs_active + phash_active, "offset": offset,
            "limit": limit, "truncated": offset + len(tasks) < total,
        }
    finally:
        conn.close()


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/tasks")
    async def list_tasks(
        request: Request,
        queue: str = Query("all", max_length=10),
        state: str = Query("", max_length=20),
        action: str = Query("", max_length=40),
        offset: int = Query(0, ge=0, le=2_147_483_647),
        limit: int = Query(50, ge=1, le=100),
        job_id: str = Query("", max_length=128),
    ):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        if queue not in {"all", "jobs", "phash"}:
            return admin.JSONResponse({"error": "Invalid task queue filter."}, status_code=400)
        if state not in _STATES:
            return admin.JSONResponse({"error": "Invalid task state filter."}, status_code=400)
        if action and action != "phash_generation" and action not in JOB_ACTIONS:
            return admin.JSONResponse({"error": "Invalid task action filter."}, status_code=400)
        if action == "phash_generation" and queue == "jobs":
            return {"ok": True, "available": True, "tasks": [], "total": 0,
                    "active_total": 0, "offset": offset, "limit": limit, "truncated": False}
        if job_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
            return admin.JSONResponse({"error": "Invalid task ID."}, status_code=400)
        try:
            result = await admin.asyncio.to_thread(
                _list_tasks, admin.HUB_DB_URL, queue=queue, state=state, action=action,
                offset=offset, limit=limit, job_id=job_id or None,
            )
            if not result["available"]:
                return admin.JSONResponse({"error": "Task queues are unavailable."}, status_code=503)
            from common.worker_capabilities import read_worker_capabilities
            worker = read_worker_capabilities(os.getenv(
                "PHASH_WORKER_CAPABILITIES_PATH", "/data/cache/phash-worker-capabilities.json"))
            return {"ok": True, **result, "workers": {"phash": worker}}
        except Exception:
            return admin.JSONResponse({"error": "Could not load task history."}, status_code=503)

    @router.get("/api/v2/tasks/{queue}/{task_id}")
    async def read_task(request: Request, queue: str, task_id: str):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        if queue not in {"jobs", "phash"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            return admin.JSONResponse({"error": "Invalid task reference."}, status_code=400)
        try:
            result = await admin.asyncio.to_thread(
                _list_tasks, admin.HUB_DB_URL, queue=queue, state="", action="",
                offset=0, limit=1, job_id=task_id,
            )
            if not result["available"]:
                return admin.JSONResponse({"error": "Task queues are unavailable."}, status_code=503)
            if not result["tasks"]:
                return admin.JSONResponse({"error": "Task not found."}, status_code=404)
            return {"ok": True, "task": result["tasks"][0]}
        except Exception:
            return admin.JSONResponse({"error": "Could not load task details."}, status_code=503)

    return router
