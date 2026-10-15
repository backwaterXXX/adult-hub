"""Read-only status and history projections for Admin operations."""

from __future__ import annotations

import json
import time

from common import db


def operation_status(db_url: str | None, plan_id: str) -> dict | None:
    """Return the plan with each independently executing item state."""
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        row = conn.execute(
            "SELECT operation_type,state,preview,result,expires_at FROM admin_operation_plans WHERE plan_id=%s",
            (plan_id,),
        ).fetchone()
        if row is None:
            return None
        preview = row["preview"] if isinstance(row["preview"], dict) else json.loads(row["preview"])
        result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"] or "{}")
        jobs = result.get("jobs", []) if isinstance(result, dict) else []
        job_ids = list(dict.fromkeys(
            str(item["job_id"]) for item in jobs
            if isinstance(item, dict) and item.get("job_id")
        ))
        job_rows = {}
        if job_ids:
            job_rows = {
                str(job_id): (str(task_state), updated_at)
                for job_id, task_state, updated_at in conn.execute(
                    "SELECT id,state,updated_at FROM video_duplicate_jobs WHERE id = ANY(%s)",
                    (job_ids,),
                ).fetchall()
            }
        jobs = [
            {**item,
             "state": job_rows.get(str(item.get("job_id") or ""), ("unknown", None))[0],
             "updated_at": job_rows.get(str(item.get("job_id") or ""), ("unknown", None))[1]}
            for item in jobs if isinstance(item, dict)
        ]
        expires = row["expires_at"].timestamp() if hasattr(row["expires_at"], "timestamp") else float(row["expires_at"])
        return {"plan_id": plan_id, "operation_type": row["operation_type"], "state": row["state"],
                "items": preview.get("items", []), "jobs": jobs, "expires_at": int(expires)}
    finally:
        conn.close()

def list_operations(db_url: str | None, limit: int = 20, offset: int = 0) -> dict:
    """List durable operation plans using a safe, bounded history projection."""
    limit = max(1, min(int(limit), 50))
    offset = max(0, int(offset))
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        total = int(conn.execute("SELECT count(*) FROM admin_operation_plans").fetchone()[0])
        rows = conn.execute(
            "SELECT plan_id,operation_type,state,preview,result,created_at,expires_at "
            "FROM admin_operation_plans ORDER BY created_at DESC,plan_id DESC LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
        queued_job_ids = []
        decoded_rows = []
        for row in rows:
            preview = row["preview"] if isinstance(row["preview"], dict) else json.loads(row["preview"] or "{}")
            result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"] or "{}")
            jobs = result.get("jobs", []) if isinstance(result, dict) else []
            decoded_rows.append((row, preview, jobs))
            queued_job_ids.extend(
                str(job["job_id"]) for job in jobs
                if isinstance(job, dict) and job.get("job_id")
            )
        job_states = {}
        if queued_job_ids:
            job_states = {
                str(job_id): str(task_state)
                for job_id, task_state in conn.execute(
                    "SELECT id,state FROM video_duplicate_jobs WHERE id = ANY(%s)",
                    (list(dict.fromkeys(queued_job_ids)),),
                ).fetchall()
            }
        items = []
        for row, preview, jobs in decoded_rows:
            states: dict[str, int] = {}
            for job in jobs:
                task_state = job_states.get(str(job.get("job_id") or ""), "unknown")
                states[task_state] = states.get(task_state, 0) + 1
            created = row["created_at"].timestamp() if hasattr(row["created_at"], "timestamp") else float(row["created_at"])
            expires = row["expires_at"].timestamp() if hasattr(row["expires_at"], "timestamp") else float(row["expires_at"])
            operation_state = str(row["state"])
            if operation_state == "previewed" and expires <= time.time():
                operation_state = "expired"
            items.append({
                "plan_id": str(row["plan_id"]),
                "operation_type": str(row["operation_type"]),
                "state": operation_state,
                "created_at": int(created),
                "expires_at": int(expires),
                "item_count": len(preview.get("items", [])),
                "task_states": states,
            })
        return {"items": items, "total": total, "offset": offset, "limit": limit}
    finally:
        conn.close()
