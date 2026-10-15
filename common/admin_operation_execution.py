"""Execution path for revision-checked Admin v2 operation plans."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from psycopg.types.json import Jsonb

from common import db
from common.admin_operations import (
    MAX_AUDIT_REPAIR_BATCH,
    MAX_ORPHAN_QUARANTINE_BATCH,
    MAX_QUARANTINE_BATCH,
    _active_hide_job,
    _active_hide_jobs,
    _active_metadata_job,
    _active_metadata_jobs,
    _active_orphan_file_job,
    _active_quarantine_job,
    _active_quarantine_jobs,
    _active_undo_job,
    _audit_import_refresh_state,
    _audit_repair_state,
    _canonical,
    _orphan_finding_state,
    _record_operation_queue_audit,
    _restore_snapshot_rows,
    _video_snapshot,
    _video_snapshots,
)

__all__ = [
    "execute_operation", "hashlib", "json", "os", "time", "uuid", "Path", "Jsonb",
    "db", "MAX_AUDIT_REPAIR_BATCH", "MAX_ORPHAN_QUARANTINE_BATCH", "MAX_QUARANTINE_BATCH",
    "_active_hide_job", "_active_metadata_job", "_active_orphan_file_job",
    "_active_hide_jobs",
    "_active_metadata_jobs",
    "_active_quarantine_job", "_active_quarantine_jobs", "_active_undo_job", "_audit_import_refresh_state",
    "_audit_repair_state", "_canonical", "_orphan_finding_state",
    "_insert_queued_jobs", "_record_operation_queue_audit", "_restore_snapshot_rows",
    "_video_snapshot", "_video_snapshots",
]


def _insert_queued_jobs(conn, action: str, entries: list[tuple[str, str]], now: int) -> None:
    """Insert bounded same-action job batches in one database round trip."""
    if not entries:
        return
    values = ",".join(["(%s,%s,'queued',%s,NULL,to_timestamp(%s),to_timestamp(%s))"] * len(entries))
    params = []
    for job_id, payload in entries:
        params.extend((job_id, action, payload, now, now))
    conn.execute(
        "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
        f"VALUES {values}",
        params,
    )


def execute_operation(db_url: str | None, plan_id: str, config_path: str | None = None) -> dict:
    """Consume a preview and enqueue every item atomically, idempotently."""
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        config = None
        if config_path:
            from common.config_loader import load_config
            config = load_config(config_path, force_reload=True)
        row = conn.execute(
            "SELECT operation_type,state,scope,revisions,preview,result,expires_at "
            "FROM admin_operation_plans WHERE plan_id=%s FOR UPDATE", (plan_id,)
        ).fetchone()
        if row is None:
            conn.commit()
            return {"outcome": "not_found"}
        state = str(row["state"])
        if state == "queued":
            result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"] or "{}")
            conn.commit()
            return {"outcome": "queued", "plan_id": plan_id,
                    "jobs": result.get("jobs", []), "reused": True}
        if state != "previewed":
            conn.commit()
            return {"outcome": "conflict", "message": "This operation plan is no longer available."}
        scope = row["scope"] if isinstance(row["scope"], dict) else json.loads(row["scope"])
        revisions = row["revisions"] if isinstance(row["revisions"], dict) else json.loads(row["revisions"])
        expires_at = row["expires_at"].timestamp() if hasattr(row["expires_at"], "timestamp") else float(row["expires_at"])
        now = int(time.time())
        if expires_at <= now:
            conn.execute("UPDATE admin_operation_plans SET state='expired' WHERE plan_id=%s", (plan_id,))
            conn.commit()
            return {"outcome": "expired"}
        from common import admin_operation_handlers as handlers
        operation_context = (conn, db_url, plan_id, row, config, scope, revisions, now)
        for handler in (
            handlers.handle_orphan_file_operation,
            handlers.handle_restore,
            handlers.handle_hide,
            handlers.handle_hide_batch,
            handlers.handle_metadata_update_batch,
            handlers.handle_metadata_update,
            handlers.handle_performer_merge,
            handlers.handle_audit_repair_nfo,
            handlers.handle_audit_import_refresh,
            handlers.handle_quarantine,
        ):
            result = handler(*operation_context)
            if result is not None:
                return result
        return {"outcome": "invalid", "message": "Unsupported operation type."}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
