"""A safe, paginated projection over the durable Admin and pHash queues."""

from __future__ import annotations

import os
import re
import logging
from typing import NamedTuple

from fastapi import APIRouter, Query, Request
from resolver.admin_v2_errors import V2ErrorRoute
from resolver.admin_v2_jobs import JOB_ACTIONS

logger = logging.getLogger(__name__)


_STATES = {"", "active", "history", "queued", "processing", "done", "failed", "cancelled"}
_VIDEO_ACTIONS = {"phash_generation": "phash", "thumbnail_generation": "thumbnail",
                  "phash_thumbnail_legacy": "combined"}
_VISUAL_ACTIONS = {"visual_signature": "signature", "visual_compare": "verify_pair"}
_PHASH_ERRORS = {
    "attempts_exhausted", "worker_restarted", "video_missing", "file_missing",
    "unsupported_format", "decode_failed", "timeout", "storage_failed",
    "fingerprint_conflict", "generation_failed", "daily_transfer_budget_exhausted",
    "transfer_budget_unavailable", "byte_budget_exhausted", "source_unavailable",
    "invalid_video",
}


def _where(state: str, action: str | None = None, job_id: str | None = None,
           action_column: str = "action"):
    clauses, params = [], []
    if state == "active":
        clauses.append("state IN ('queued','processing')")
    elif state == "history":
        clauses.append("state IN ('done','failed','cancelled')")
    elif state:
        clauses.append("state=%s")
        params.append(state)
    if action:
        clauses.append(f"{action_column}=%s")
        params.append(action)
    if job_id:
        clauses.append("id=%s")
        params.append(job_id)
    return (" WHERE " + " AND ".join(clauses) if clauses else "", params)


def _count(conn, table: str, state: str, action: str | None = None,
           job_id: str | None = None, action_column: str = "action") -> int:
    where, params = _where(state, action, job_id, action_column)
    return int(conn.execute(f"SELECT count(*) FROM {table}{where}", params).fetchone()[0])


class _TaskScope(NamedTuple):
    """Which queues a task listing reads, and the action filter each one uses."""
    state: str
    job_id: str | None
    jobs: bool
    jobs_action: str | None
    phash: bool
    phash_action: str | None
    visual: bool
    visual_action: str | None


def _task_scope(queue: str, action: str, state: str, job_id: str | None) -> _TaskScope:
    include_jobs = queue in {"all", "jobs"} and action not in {*_VIDEO_ACTIONS, *_VISUAL_ACTIONS}
    include_phash = queue in {"all", "phash"} and action in {"", *_VIDEO_ACTIONS}
    include_visual = queue in {"all", "visual"} and action in {"", *_VISUAL_ACTIONS}
    return _TaskScope(
        state=state,
        job_id=job_id,
        jobs=include_jobs,
        jobs_action=action if include_jobs else None,
        phash=include_phash,
        phash_action=_VIDEO_ACTIONS.get(action),
        visual=include_visual,
        visual_action=_VISUAL_ACTIONS.get(action),
    )


def _count_tasks(conn, scope: _TaskScope) -> dict:
    """Return the matching total and the active total across the scoped queues."""
    jobs_total = _count(conn, "video_duplicate_jobs", scope.state, scope.jobs_action, scope.job_id) if scope.jobs else 0
    phash_total = _count(conn, "video_phash_jobs", scope.state, scope.phash_action, scope.job_id, "job_kind") if scope.phash else 0
    visual_total = _count(conn, "video_visual_jobs", scope.state, scope.visual_action, scope.job_id, "kind") if scope.visual else 0
    jobs_active = _count(conn, "video_duplicate_jobs", "active", scope.jobs_action, scope.job_id) if scope.jobs else 0
    phash_active = _count(conn, "video_phash_jobs", "active", scope.phash_action, scope.job_id, "job_kind") if scope.phash else 0
    visual_active = _count(conn, "video_visual_jobs", "active", scope.visual_action, scope.job_id, "kind") if scope.visual else 0
    return {
        "total": jobs_total + phash_total + visual_total,
        "active_total": jobs_active + phash_active + visual_active,
    }


def _task_rows(conn, scope: _TaskScope, limit: int, offset: int) -> list:
    """Return (queue, id) pairs for one page of tasks, newest first."""
    branches = []
    params = []
    if scope.jobs:
        job_where, job_params = _where(scope.state, scope.jobs_action, scope.job_id)
        branches.append(
            "SELECT 'jobs'::text AS queue,id,created_at AS sort_at "
            f"FROM video_duplicate_jobs{job_where}"
        )
        params.extend(job_params)
    if scope.phash:
        phash_where, phash_params = _where(scope.state, scope.phash_action, scope.job_id, "job_kind")
        branches.append(
            "SELECT 'phash'::text AS queue,id,created_at AS sort_at "
            f"FROM video_phash_jobs{phash_where}"
        )
        params.extend(phash_params)
    if scope.visual:
        visual_where, visual_params = _where(scope.state, scope.visual_action, scope.job_id, "kind")
        branches.append("SELECT 'visual'::text AS queue,id,created_at AS sort_at "
                        f"FROM video_visual_jobs{visual_where}")
        params.extend(visual_params)
    if not branches:
        return []
    return conn.execute(
        "WITH task_keys AS (" + " UNION ALL ".join(branches) + ") "
        "SELECT queue,id FROM task_keys ORDER BY sort_at DESC,id DESC,queue "
        "LIMIT %s OFFSET %s",
        (*params, limit, offset),
    ).fetchall()


def _hydrate_jobs(conn, job_ids: list) -> dict:
    from resolver.admin_services import _job_undo_available, _public_job
    from resolver.admin_v2_jobs import _safe_job

    general = {}
    if not job_ids:
        return general
    placeholders = ",".join(["%s"] * len(job_ids))
    for raw in conn.execute(
        f"SELECT * FROM video_duplicate_jobs WHERE id IN ({placeholders})", job_ids,
    ).fetchall():
        job = _public_job(raw, include_failure_code=True)
        job["undo_available"] = _job_undo_available(conn, job)
        projected = _safe_job(job)
        projected["queue"] = "jobs"
        general[str(projected["id"])] = projected
    return general


def _hydrate_phash(conn, job_ids: list) -> dict:
    phash = {}
    if not job_ids:
        return phash
    from common.phash_jobs import _processing_time_select, _public_job as phash_public_job

    placeholders = ",".join(["%s"] * len(job_ids))
    for raw in conn.execute(
        "SELECT j.*,v.title," + _processing_time_select("j") +
        " FROM video_phash_jobs j JOIN videos v ON v.id=j.video_id "
        f"WHERE j.id IN ({placeholders})", job_ids,
    ).fetchall():
        item = phash_public_job(conn, raw)
        error_code = item.get("error_code")
        phash[str(item["id"])] = {
            "queue": "phash", "id": str(item["id"]),
            "action": {value: key for key, value in _VIDEO_ACTIONS.items()}.get(item.get("job_kind"), "phash_thumbnail_legacy"), "state": str(item["state"]),
            "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
            "title": item.get("title"), "video_id": str(item["video_id"]),
            "attempts": int(item.get("attempts") or 0),
            "processing_time_ms": int(item.get("processing_time_ms") or 0),
            "thumbnail_ready": item.get("thumbnail_ready") is True,
            "error_code": error_code if error_code in _PHASH_ERRORS else None,
            "error_detail": str(item.get("error_detail") or "")[:2000],
            "stage": str(item["state"]), "events": [], "undo_available": False,
        }
    return phash


def _hydrate_visual(conn, job_ids: list) -> dict:
    visual = {}
    if not job_ids:
        return visual
    placeholders = ",".join(["%s"] * len(job_ids))
    for raw in conn.execute(
        "SELECT id,kind,state,created_at,updated_at,attempts,video_id,left_id,right_id,error_code "
        f"FROM video_visual_jobs WHERE id IN ({placeholders})", job_ids,
    ).fetchall():
        identifier, kind, status, created, updated, attempts, video, left, right, error = raw
        visual[str(identifier)] = {
            "queue": "visual", "id": str(identifier),
            "action": "visual_signature" if kind == "signature" else "visual_compare",
            "state": status, "created_at": created, "updated_at": updated,
            "stage": status, "progress": {}, "events": [], "undo_available": False,
            "parameters": {"attempts": attempts, **({"video_id": video} if video else {"left_id": left, "right_id": right})},
            "error": "Visual processing failed. Review worker logs for details." if error else None,
        }
    return visual


def _list_tasks(db_url: str | None, *, queue: str, state: str, action: str,
                offset: int, limit: int, job_id: str | None = None) -> dict:
    from resolver.admin_services import _jobs_connection
    from common import db

    conn = _jobs_connection(db_url)
    if conn is None:
        return {"available": False, "tasks": [], "total": None, "active_total": None}
    try:
        tables = db.table_names(conn)
        if "video_phash_jobs" not in tables or "videos" not in tables:
            return {"available": False, "tasks": [], "total": None, "active_total": None}

        scope = _task_scope(queue, action, state, job_id)
        counts = _count_tasks(conn, scope)
        rows = _task_rows(conn, scope, limit, offset)

        ids = {"jobs": [], "phash": [], "visual": []}
        for row in rows:
            ids[row[0]].append(row[1])
        queues = {
            "jobs": _hydrate_jobs(conn, ids["jobs"]),
            "phash": _hydrate_phash(conn, ids["phash"]),
            "visual": _hydrate_visual(conn, ids["visual"]),
        }
        tasks = [queues[row[0]].get(str(row[1])) for row in rows]
        tasks = [task for task in tasks if task is not None]
        total = counts["total"]
        return {
            "available": True, "tasks": tasks, "total": total,
            "active_total": counts["active_total"], "offset": offset,
            "limit": limit, "truncated": offset + len(tasks) < total,
        }
    finally:
        conn.close()


def _route_list_tasks(router, admin):
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
        if queue not in {"all", "jobs", "phash", "visual"}:
            return admin.JSONResponse({"error": "Invalid task queue filter."}, status_code=400)
        if state not in _STATES:
            return admin.JSONResponse({"error": "Invalid task state filter."}, status_code=400)
        if action and action not in {*_VIDEO_ACTIONS, *_VISUAL_ACTIONS} and action not in JOB_ACTIONS:
            return admin.JSONResponse({"error": "Invalid task action filter."}, status_code=400)
        if action in _VIDEO_ACTIONS and queue == "jobs":
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
            logger.warning("Request failed in list_tasks", exc_info=True)
            return admin.JSONResponse({"error": "Could not load task history."}, status_code=503)


def _route_read_task(router, admin):
    @router.get("/api/v2/tasks/{queue}/{task_id}")
    async def read_task(request: Request, queue: str, task_id: str):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        if queue not in {"jobs", "phash", "visual"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
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
            logger.warning("Request failed in read_task", exc_info=True)
            return admin.JSONResponse({"error": "Could not load task details."}, status_code=503)


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)
    _route_list_tasks(router, admin)
    _route_read_task(router, admin)
    return router
