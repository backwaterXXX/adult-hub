"""Admin domain services, independent of HTTP handlers."""
import json
import time
from common import db
from common.job_stages import ALL_STAGES
from common.job_failures import classify_job_failure


_JOB_PROGRESS_STAGES = ALL_STAGES

def _safe_job_failure_code(result, action: str | None = None) -> str | None:
    """Classify persisted failures without returning exception text or secrets."""
    return classify_job_failure(action, result)


def _safe_job_progress(result: dict) -> tuple[dict | None, list[dict]]:
    raw = result.get("progress") if isinstance(result, dict) else None
    progress = None
    if isinstance(raw, dict) and raw.get("stage") in _JOB_PROGRESS_STAGES:
        progress = {"stage": raw["stage"]}
        stamp = raw.get("updated_at")
        if isinstance(stamp, int) and stamp >= 0:
            progress["updated_at"] = stamp
        for key in ("completed", "total"):
            value = raw.get(key)
            if isinstance(value, int) and value >= 0:
                progress[key] = value
    events = []
    raw_events = result.get("events") if isinstance(result, dict) else None
    if isinstance(raw_events, list):
        for event in raw_events[-20:]:
            if not isinstance(event, dict) or event.get("stage") not in _JOB_PROGRESS_STAGES:
                continue
            safe = {"stage": event["stage"]}
            stamp = event.get("updated_at")
            if isinstance(stamp, int) and stamp >= 0:
                safe["updated_at"] = stamp
            for key in ("completed", "total"):
                value = event.get(key)
                if isinstance(value, int) and value >= 0:
                    safe[key] = value
            events.append(safe)
    return progress, events


def _jobs_connection(db_url: str | None) -> db.Connection | None:
    """Inspection never initializes a DB or writes schema/data."""
    try:
        conn = db.connect(db_url)
    except Exception:
        return None
    conn.row_factory = db.Record
    try:
        if not db.table_exists(conn, "video_duplicate_jobs"):
            conn.close()
            return None
        return conn
    except Exception:
        conn.close()
        raise


def _public_job(row: db.Record, *, include_failure_code: bool = False) -> dict:
    item = dict(row)
    # An ownership token is internal coordination, never part of the API contract.
    item.pop("lease_token", None)
    item["payload"] = json.loads(item["payload"])
    item["payload"].pop("fingerprints", None)
    item["result"] = json.loads(item["result"]) if item["result"] else None
    item["progress"], item["events"] = _safe_job_progress(item["result"] or {})
    if include_failure_code:
        item["failure_code"] = _safe_job_failure_code(item["result"], item.get("action"))
    if item["result"] and "snapshots" in item["result"]:
        item["result"] = {"operation_id": item["result"].get("operation_id"), "recovery": "in_progress"}
    if isinstance(item["result"], dict) and item["result"].get("error"):
        item["result"]["error"] = "Job failed. Inspect local worker logs for details."
    # Worker exception text can contain remote URLs, local paths and credentials.
    if item.get("error"):
        item["error"] = "Job failed. Inspect local worker logs for details."
    return item


def get_job_v2(db_url: str | None, job_id: str) -> dict:
    """Read one job while distinguishing an empty result from unavailable data."""
    conn = _jobs_connection(db_url)
    if conn is None:
        return {"available": False, "job": None}
    try:
        row = conn.execute("SELECT * FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        if row is None:
            return {"available": True, "job": None}
        job = _public_job(row, include_failure_code=True)
        job["undo_available"] = _job_undo_available(conn, job)
        return {"available": True, "job": job}
    finally:
        conn.close()


def _job_undo_available(conn, job: dict) -> bool:
    """Check that this completed decision is still active and has no pending undo."""
    mapping = {
        "hide": ("undo", "video_duplicate_decisions", "state='active'"),
        "quarantine": ("undo", "video_duplicate_decisions", "state='active'"),
        "dismiss": ("undismiss", "video_duplicate_dismissals", "active=1"),
    }
    entry = mapping.get(str(job.get("action") or ""))
    if job.get("state") != "done" or not entry:
        return False
    undo_action, table, condition = entry
    if table not in db.table_names(conn):
        return False
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    operation_id = result.get("operation_id") or job.get("id")
    active = conn.execute(
        f"SELECT 1 FROM {table} WHERE operation_id=%s AND {condition} LIMIT 1", (operation_id,)
    ).fetchone()
    if not active:
        return False
    for (payload,) in conn.execute(
        "SELECT payload FROM video_duplicate_jobs WHERE action=%s AND state IN ('queued','processing')",
        (undo_action,),
    ):
        try:
            if json.loads(payload).get("operation_id") == operation_id:
                return False
        except (ValueError, TypeError, AttributeError):
            continue
    return True


def sync_backlog(db_url: str | None, *, now: int | None = None) -> dict:
    """Read queued sync work and its age without initializing or changing schema."""
    conn = _jobs_connection(db_url)
    if conn is None:
        return {"available": False, "queued": 0, "processing": 0,
                "oldest_queued_at": None, "oldest_queued_age_seconds": None}
    try:
        rows = conn.execute(
            "SELECT state, count(*), min(created_at) FROM video_duplicate_jobs "
            "WHERE action='sync' AND state IN ('queued','processing') GROUP BY state"
        ).fetchall()
        counts = {"queued": 0, "processing": 0}
        oldest = None
        for state, count, created_at in rows:
            counts[str(state)] = int(count or 0)
            if str(state) == "queued" and created_at is not None:
                oldest = int(created_at)
        current = int(time.time()) if now is None else int(now)
        return {
            "available": True,
            "queued": counts["queued"],
            "processing": counts["processing"],
            "oldest_queued_at": oldest,
            "oldest_queued_age_seconds": max(0, current - oldest) if oldest is not None else None,
        }
    finally:
        conn.close()


def job_listing(db_url: str | None, limit: int = 50, state: str = "", action: str = "", offset: int = 0,
                *, include_failure_code: bool = False) -> dict:
    conn = _jobs_connection(db_url)
    if conn is None:
        return {"jobs": [], "total": 0, "active_total": 0, "truncated": False, "available": False}
    try:
        # Counts and returned rows must describe one snapshot for fail-closed UI.
        # (psycopg already runs these in a single transaction.)
        tables = db.table_names(conn)
        active = {"hide": set(), "dismiss": set(), "quarantine": set()}
        for decision_action, table, condition in (("hide", "video_duplicate_decisions", "state='active'"),
                                      ("quarantine", "video_duplicate_decisions", "state='active'"),
                                      ("dismiss", "video_duplicate_dismissals", "active=1")):
            if table in tables:
                active[decision_action] = {r[0] for r in conn.execute(f"SELECT DISTINCT operation_id FROM {table} WHERE {condition}")}
        pending = {"undo": set(), "undismiss": set()}
        for pending_action, payload in conn.execute("SELECT action,payload FROM video_duplicate_jobs WHERE action IN ('undo','undismiss') AND state IN ('queued','processing')"):
            try:
                pending[pending_action].add(json.loads(payload).get("operation_id"))
            except (ValueError, TypeError, AttributeError):
                pass
        clauses, params = [], []
        if state == "active":
            clauses.append("state IN ('queued','processing')")
        elif state:
            clauses.append("state=%s")
            params.append(state)
        if action:
            clauses.append("action=%s")
            params.append(action)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = conn.execute("SELECT count(*) FROM video_duplicate_jobs" + where, params).fetchone()[0]
        active_total = conn.execute("SELECT count(*) FROM video_duplicate_jobs WHERE state IN ('queued','processing')").fetchone()[0]
        jobs = []
        safe_limit = max(1, min(int(limit), 1000))
        safe_offset = max(0, int(offset))
        for row in conn.execute(
            "SELECT * FROM video_duplicate_jobs" + where
            + " ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
            (*params, safe_limit, safe_offset),
        ):
            item = _public_job(row, include_failure_code=include_failure_code)
            operation = (item["result"] or {}).get("operation_id") or item["id"]
            undo = {"hide": "undo", "dismiss": "undismiss", "quarantine": "undo"}.get(item["action"])
            item["undo_available"] = bool(item["state"] == "done" and undo
                and operation in active[item["action"]] and operation not in pending[undo])
            jobs.append(item)
        return {
            "jobs": jobs,
            "total": total,
            "active_total": active_total,
            "truncated": safe_offset + len(jobs) < total,
            "available": True,
        }
    finally:
        conn.close()


def unresolved_failed_task_summary(db_url: str | None) -> dict:
    """Count actionable failed task groups without paging through full history."""
    conn = _jobs_connection(db_url)
    if conn is None:
        return {"available": False, "count": None}
    try:
        row = conn.execute("""
            WITH job_keys AS (
                SELECT id, action, state, updated_at,
                    CASE WHEN jsonb_typeof(payload::jsonb) = 'object'
                         THEN payload::jsonb - ARRAY['fingerprints', '_resume']::text[]
                         ELSE jsonb_build_array('invalid_payload_job', id)
                    END AS payload_key
                FROM video_duplicate_jobs
                WHERE state IN ('failed', 'done')
            ), latest_by_state AS (
                SELECT action, state, payload_key, max(updated_at) AS last_updated
                FROM job_keys
                GROUP BY action, state, payload_key
            )
            SELECT count(*)
            FROM latest_by_state AS failed
            WHERE failed.state = 'failed'
              AND NOT EXISTS (
                SELECT 1 FROM latest_by_state AS done
                WHERE done.state = 'done'
                  AND done.action = failed.action
                  AND done.payload_key = failed.payload_key
                  AND done.last_updated > failed.last_updated
              )
        """).fetchone()
        return {"available": True, "count": int(row[0] or 0)}
    except Exception:
        # Invalid historical payloads or a database error must remain unknown,
        # never turn into a false zero on Home.
        return {"available": False, "count": None}
    finally:
        conn.close()


def unresolved_failed_task_listing(db_url: str | None, limit: int = 1000) -> dict:
    """List actionable failed-job groups after checking the complete history."""
    conn = _jobs_connection(db_url)
    if conn is None:
        return {"available": False, "jobs": [], "total": None, "truncated": False}
    try:
        safe_limit = max(1, min(int(limit), 1000))
        rows = conn.execute("""
            WITH job_keys AS (
                SELECT id, action, state, created_at, updated_at,
                    CASE WHEN jsonb_typeof(payload::jsonb) = 'object'
                         THEN payload::jsonb - ARRAY['fingerprints', '_resume']::text[]
                         ELSE jsonb_build_array('invalid_payload_job', id)
                    END AS payload_key
                FROM video_duplicate_jobs
                WHERE state IN ('failed', 'done')
            ), failed_groups AS (
                SELECT action, payload_key, count(*) AS attempts,
                       max(updated_at) AS failed_at
                FROM job_keys
                WHERE state = 'failed'
                GROUP BY action, payload_key
            ), latest_failed AS (
                SELECT id, action, payload_key, created_at, updated_at,
                       row_number() OVER (
                           PARTITION BY action, payload_key
                           ORDER BY updated_at DESC, created_at DESC, id DESC
                       ) AS rank
                FROM job_keys
                WHERE state = 'failed'
            ), actionable AS (
                SELECT latest.id, latest.action, latest.created_at,
                       groups.failed_at AS updated_at, groups.attempts
                FROM failed_groups AS groups
                JOIN latest_failed AS latest
                  ON latest.action = groups.action
                 AND latest.payload_key = groups.payload_key
                 AND latest.rank = 1
                WHERE NOT EXISTS (
                    SELECT 1 FROM job_keys AS completed
                    WHERE completed.state = 'done'
                      AND completed.action = groups.action
                      AND completed.payload_key = groups.payload_key
                      AND completed.updated_at > groups.failed_at
                )
            )
            SELECT id, action, created_at, updated_at, attempts,
                   count(*) OVER() AS total
            FROM actionable
            ORDER BY updated_at DESC, id DESC
            LIMIT %s
        """, (safe_limit,)).fetchall()
        jobs = [{
            "id": str(row[0]), "action": str(row[1]),
            "created_at": row[2], "updated_at": row[3],
            "attempts": int(row[4] or 0),
        } for row in rows]
        total = int(rows[0][5] or 0) if rows else 0
        return {
            "available": True, "jobs": jobs, "total": total,
            "truncated": len(jobs) < total,
        }
    except Exception:
        # Invalid legacy payloads or database errors must not look like zero issues.
        return {"available": False, "jobs": [], "total": None, "truncated": False}
    finally:
        conn.close()
