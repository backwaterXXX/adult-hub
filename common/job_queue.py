"""Shared durable job queue primitives for admin operations.

Backend is the central PostgreSQL server (see ``common.db``). The resolver
produces jobs and the sync worker consumes them. Both images install this
package, so the queue semantics live here instead of in either router or
worker. ``sync`` jobs are consumed by the runner itself (a full sync
cannot run inside the jobs-only child that already holds the sync lock).
"""

from __future__ import annotations

import json
import time
import uuid

from common.db import (
    Connection,
    Record,
    UndefinedTable,
    connect as _db_connect,
)

# Actions the generic jobs-only worker can execute while holding its lock.
WORKER_ACTIONS = (
    "hide", "dismiss", "undo", "undismiss", "performer_merge", "performer_refresh",
    "collections_refresh", "quarantine", "metadata_update", "availability_check",
    "consistency_audit", "metadata_quality_audit", "catalog_full_audit", "audit_repair_nfo", "audit_import_refresh", "orphan_file_operation",
    "backup_create", "backup_restore_test", "metadata_backfill", "telegram_fingerprint_scan", "artwork_download", "artwork_generate", "duplicate_reconcile",
    # One-off triggers for the dependent stages listed in the Schedules UI.
    "history_cleanup", "performer_catalog", "scene_backfill", "telegram_title_cleanup",
    "source_remove", "source_readd",
)
# ``sync`` is claimed by the runner, which owns full-sync execution.
SYNC_ACTION = "sync"
ALL_ACTIONS = (*WORKER_ACTIONS, SYNC_ACTION)
MAX_JOB_RESULT_BYTES = 256 * 1024

def connect_queue(db_url: str | None = None) -> Connection:
    """Open the durable queue connection."""
    return _db_connect(db_url)


# Backwards-compatible alias (older code called ``connect(db_path)``).
connect = connect_queue


def _open(db_url: str | None) -> Connection | None:
    try:
        return _db_connect(db_url)
    except Exception:
        return None


def job_identity(action: str, payload: dict) -> str:
    """Canonical semantics for pending requests; retain hide snapshot identity."""
    normalized = dict(payload)
    if action == "hide":
        normalized["loser_ids"] = sorted(set(payload.get("loser_ids", [])))
    elif action == "dismiss":
        left, right = sorted((payload.get("left_id", ""), payload.get("right_id", "")))
        normalized.update(left_id=left, right_id=right)
    elif action == "performer_merge":
        normalized["merges"] = sorted(
            [{"alias": pair["alias"].strip(), "canonical": pair["canonical"].strip()}
             for pair in payload.get("merges", [])], key=lambda pair: (pair["alias"], pair["canonical"]))
    elif action == SYNC_ACTION:
        normalized["source"] = (payload.get("source") or "").strip() or None
        normalized["sync_mode"] = payload.get("sync_mode") or "scheduled"
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def enqueue_jobs_batch(db_url: str | None, items: list[tuple[str, dict]]) -> list[str]:
    """Atomically enqueue multiple jobs in a single connection and transaction."""
    if not items:
        return []
    from collections import defaultdict
    conn = connect_queue(db_url)
    try:
        actions = sorted({action for action, _ in items})
        for action in actions:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("enqueue_job:" + action,))

        # Lock equivalent rows so concurrent producers cannot double-enqueue.
        existing_by_action: dict[str, dict[str, str]] = defaultdict(dict)
        for action in actions:
            for job_id, stored in conn.execute(
                    "SELECT id, payload FROM video_duplicate_jobs "
                    "WHERE action=%s AND state IN ('queued','processing') "
                    "ORDER BY created_at, id FOR UPDATE",
                    (action,)).fetchall():
                try:
                    ident = job_identity(action, json.loads(stored))
                    existing_by_action[action].setdefault(ident, job_id)
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass

        job_ids = []
        to_insert = []
        now = int(time.time())
        for action, payload in items:
            ident = job_identity(action, payload)
            if ident in existing_by_action[action]:
                job_ids.append(existing_by_action[action][ident])
            else:
                job_id = uuid.uuid4().hex
                job_ids.append(job_id)
                existing_by_action[action][ident] = job_id
                to_insert.append((job_id, action, "queued", json.dumps(payload, ensure_ascii=False), None, now, now))

        if to_insert:
            conn.executemany(
                "INSERT INTO video_duplicate_jobs "
                "(id,action,state,payload,result,created_at,updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)", to_insert)
        conn.commit()
        return job_ids
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def enqueue_job(db_url: str | None, action: str, payload: dict) -> str:
    """Reuse equivalent queued/processing jobs atomically across tabs/processes."""
    return enqueue_jobs_batch(db_url, [(action, payload)])[0]


def read_job(db_url: str | None, job_id: str) -> dict | None:
    conn = _open(db_url)
    if conn is None:
        return None
    try:
        conn.row_factory = Record
        row = conn.execute("SELECT * FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        return dict(row) if row else None
    except UndefinedTable:
        return None
    finally:
        conn.close()


def retry_job(db_url: str | None, job_id: str) -> str | None:
    """Re-queue a recorded job with safe durable stage markers, deduplicated."""
    row = read_job(db_url, job_id)
    if row is None:
        return None
    if str(row.get("state") or "").lower() not in {"failed", "cancelled"}:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or row["action"] not in ALL_ACTIONS:
        return None
    if row["action"] == "metadata_update":
        try:
            result = json.loads(row.get("result") or "{}")
        except (TypeError, ValueError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        # Keep only markers that prove a local stage already completed. The
        # worker still rechecks source fingerprint and override revision.
        resume = payload.get("_resume") if isinstance(payload.get("_resume"), dict) else {}
        for key in ("metadata_revision", "nfo_source_fingerprint", "nfo_written", "local_state"):
            if key in result:
                resume[key] = result[key]
        if resume:
            payload["_resume"] = resume
    elif row["action"] == "performer_merge":
        try:
            result = json.loads(row.get("result") or "{}")
        except (TypeError, ValueError):
            result = {}
        progress = result.get("progress") if isinstance(result, dict) else None
        if isinstance(progress, dict) and progress.get("stage") == "merging_performers":
            resume = payload.get("_resume") if isinstance(payload.get("_resume"), dict) else {}
            resume["merge_started"] = True
            payload["_resume"] = resume
    return enqueue_job(db_url, row["action"], payload)


def cancel_queued_job(db_url: str | None, job_id: str) -> dict:
    """Cancel a queued job only if a worker has not claimed it yet.

    The row lock serializes this operation with ``claim_job``. Processing and
    terminal jobs are deliberately left untouched.
    """
    conn = connect_queue(db_url)
    conn.row_factory = Record
    try:
        row = conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s FOR UPDATE",
            (job_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return {"outcome": "not_found"}
        current = str(row["state"] or "")
        if current != "queued":
            conn.commit()
            return {"outcome": "conflict", "state": current}

        try:
            result = json.loads(row["result"]) if row["result"] else {}
        except (TypeError, ValueError):
            result = {}
        if not isinstance(result, dict):
            result = {}
        now = int(time.time())
        progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}
        progress.update(stage="cancelled", updated_at=now)
        result["progress"] = progress
        events = result.get("events") if isinstance(result.get("events"), list) else []
        events.append({"stage": "cancelled", "updated_at": now})
        result["events"] = events[-20:]
        changed = conn.execute(
            "UPDATE video_duplicate_jobs SET state='cancelled',result=%s,updated_at=%s "
            "WHERE id=%s AND state='queued'",
            (json.dumps(result, ensure_ascii=False), now, job_id),
        ).rowcount
        if changed != 1:
            conn.rollback()
            return {"outcome": "conflict", "state": current}
        conn.commit()
        return {"outcome": "cancelled", "state": "cancelled"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_job(db_url: str | None, action: str) -> dict | None:
    """Atomically claim the oldest queued job for one consumer.

    The returned record carries a fresh ``lease_token``. The other mutators
    (``finish_job``, ``heartbeat_job``) require it, so a worker that lost the
    job (for example after a restart requeued it) cannot finish or keep alive
    the run another process now owns.
    """
    conn = _open(db_url)
    if conn is None:
        return None
    try:
        conn.row_factory = Record
        row = conn.execute(
            "SELECT * FROM video_duplicate_jobs WHERE action=%s AND state='queued' "
            "ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED",
            (action,)).fetchone()
        if row is None:
            conn.commit()
            return None
        record = dict(row)
        token = uuid.uuid4().hex
        changed = conn.execute(
            "UPDATE video_duplicate_jobs SET state='processing', lease_token=%s, updated_at=%s "
            "WHERE id=%s AND state='queued'",
            (token, int(time.time()), record["id"])).rowcount
        if changed != 1:
            conn.rollback()
            return None
        conn.commit()
        record["lease_token"] = token
        return record
    except UndefinedTable:
        conn.rollback()
        return None
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def pending_actions(db_url: str | None) -> set[str]:
    """Inspect durable work without changing state.

    Worker processing rows need another worker invocation for recovery; only
    that worker may requeue them, while holding its existing OS locks.
    """
    conn = _open(db_url)
    if conn is None:
        return set()
    try:
        return {row[0] for row in conn.execute(
            "SELECT DISTINCT action FROM video_duplicate_jobs "
            "WHERE state='queued' OR (state='processing' AND action<>'sync')").fetchall()}
    except UndefinedTable:
        return set()
    finally:
        conn.close()


def finish_job(db_url: str | None, job_id: str, state: str, result: dict, *,
               lease_token: str) -> bool:
    """Finish a job this process claimed, without overwriting another owner's run."""
    if state not in {"done", "failed"}:
        raise ValueError("Job completion state must be 'done' or 'failed'.")
    serialized_result = json.dumps(result, ensure_ascii=False)
    result_bytes = len(serialized_result.encode("utf-8"))
    if result_bytes > MAX_JOB_RESULT_BYTES:
        # Keep the job terminal and make truncation explicit instead of leaving
        # it processing because a diagnostic/result payload grew without bound.
        serialized_result = json.dumps({
            "result_truncated": True,
            "original_size_bytes": result_bytes,
            "message": "Job result exceeded the storage limit; detailed result was omitted.",
        }, ensure_ascii=False)
    conn = connect_queue(db_url)
    try:
        changed = conn.execute(
            "UPDATE video_duplicate_jobs SET state=%s, result=%s, lease_token=NULL, updated_at=%s "
            "WHERE id=%s AND state='processing' AND lease_token=%s",
            (state, serialized_result, int(time.time()), job_id, lease_token),
        ).rowcount
        conn.commit()
        return changed == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def heartbeat_job(db_url: str | None, job_id: str, *, now: int | None = None,
                  lease_token: str) -> bool:
    """Refresh the heartbeat for a job this process still owns."""
    conn = connect_queue(db_url)
    try:
        changed = conn.execute(
            "UPDATE video_duplicate_jobs SET updated_at=%s "
            "WHERE id=%s AND state='processing' AND lease_token=%s",
            (int(time.time()) if now is None else int(now), job_id, lease_token),
        ).rowcount
        conn.commit()
        return changed == 1
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def requeue_processing(db_url: str | None, action: str) -> int:
    """Recover jobs interrupted by a worker restart. Safe only for the owner.

    The caller must hold the exclusive worker lock for ``action`` (the sync
    runner owns ``sync``), because a concurrently live instance keeps its
    in-flight job heartbeated and this blanket flip would double-run it.
    The duplicate decision worker follows the same rule: all of its callers hold
    ``.adult-hub-sync.lock`` exclusively, so any 'processing' row it finds there
    is an orphan of a crashed run.
    """
    conn = _open(db_url)
    if conn is None:
        return 0
    try:
        cur = conn.execute(
            "UPDATE video_duplicate_jobs SET state='queued', lease_token=NULL, updated_at=%s "
            "WHERE action=%s AND state='processing'",
            (int(time.time()), action))
        changed = cur.rowcount
        conn.commit()
        return changed
    except UndefinedTable:
        conn.rollback()
        return 0
    finally:
        conn.close()
