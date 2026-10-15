"""Preview plans for quarantine and restore operations."""

from __future__ import annotations

import hashlib
import time
import uuid

from psycopg.types.json import Jsonb

from common import db
from common.admin_operations import (
    MAX_QUARANTINE_BATCH,
    PLAN_TTL_SECONDS,
    _active_quarantine_jobs,
    _active_undo_job,
    _canonical,
    _restore_snapshot_rows,
    _video_snapshots,
)


def preview_quarantine(db_url: str | None, video_ids: list[str], config_path: str | None = None) -> dict:
    """Persist a bounded explicit-list quarantine plan without mutating catalog data."""
    if not isinstance(video_ids, list) or not 1 <= len(video_ids) <= MAX_QUARANTINE_BATCH:
        return {"outcome": "invalid", "message": f"Select between 1 and {MAX_QUARANTINE_BATCH} items."}
    ids = [str(value).strip() for value in video_ids]
    if any(not value or len(value) > 512 for value in ids) or len(set(ids)) != len(ids):
        return {"outcome": "invalid", "message": "Item IDs must be unique, non-empty values."}
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        config = None
        if config_path:
            from common.config_loader import load_config
            config = load_config(config_path, force_reload=True)
        revisions: dict[str, str] = {}
        items = []
        conn.execute("DELETE FROM admin_operation_plans WHERE state='expired' AND expires_at < to_timestamp(%s)",
                     (int(time.time()) - 30 * 86400,))
        snapshots = _video_snapshots(conn, ids, config)
        active_quarantine_jobs = _active_quarantine_jobs(conn, ids)
        for video_id in ids:
            revision, snapshot = snapshots.get(video_id, (None, None))
            if revision is None:
                conn.rollback()
                return {"outcome": "not_found", "video_id": video_id}
            if snapshot["active_decision"]:
                conn.rollback()
                return {"outcome": "conflict", "video_id": video_id, "message": "An item is already quarantined."}
            if snapshot["source_disabled"]:
                conn.rollback()
                return {"outcome": "conflict", "video_id": video_id, "message": "An item belongs to a disabled source."}
            if snapshot["shared_paths"]:
                conn.rollback()
                return {"outcome": "conflict", "video_id": video_id, "message": "An item shares a library file with another item."}
            revisions[video_id] = revision
            video = snapshot["video"]
            items.append({
                "video_id": video_id,
                "title": str(video.get("title") or "Untitled"),
                "file_count": len(snapshot["files"]),
                "will_move_files": bool(snapshot["files"]),
                "already_queued": video_id in active_quarantine_jobs,
            })

        plan_id = uuid.uuid4().hex
        now = int(time.time())
        expires_at = now + PLAN_TTL_SECONDS
        conn.execute(
            "INSERT INTO admin_operation_plans "
            "(plan_id,operation_type,state,scope,revisions,preview,created_at,expires_at) "
            "VALUES (%s,'quarantine','previewed',%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
            (plan_id, Jsonb({"video_ids": ids}), Jsonb(revisions), Jsonb({"items": items}), now, expires_at),
        )
        conn.commit()
        return {"outcome": "previewed", "plan_id": plan_id, "expires_at": expires_at,
                "operation_type": "quarantine", "items": items, "reversible": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def preview_restore(db_url: str | None, operation_id: str) -> dict:
    """Persist a bounded preview for restoring one recorded quarantine/hide operation."""
    operation_id = str(operation_id or "").strip()
    if not operation_id or len(operation_id) > 100:
        return {"outcome": "invalid", "message": "Specify a valid recorded operation."}
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        snapshots = _restore_snapshot_rows(conn, operation_id)
        if not snapshots:
            conn.rollback()
            return {"outcome": "not_found", "message": "No valid restorable decision was found, or the restore group exceeds the safe limit."}
        revision = hashlib.sha256(_canonical([
            {"video_id": video_id, "snapshot": snapshot} for video_id, snapshot in snapshots
        ]).encode("utf-8")).hexdigest()
        items = [{"video_id": video_id,
                  "title": str(snapshot["video"].get("title") or "Untitled"),
                  "file_count": len(snapshot.get("files", [])),
                  "already_queued": _active_undo_job(conn, operation_id) is not None}
                 for video_id, snapshot in snapshots]
        plan_id = uuid.uuid4().hex
        now = int(time.time())
        expires_at = now + PLAN_TTL_SECONDS
        conn.execute(
            "INSERT INTO admin_operation_plans "
            "(plan_id,operation_type,state,scope,revisions,preview,created_at,expires_at) "
            "VALUES (%s,'restore','previewed',%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
            (plan_id, Jsonb({"operation_id": operation_id}), Jsonb({"operation_id": revision}),
             Jsonb({"items": items}), now, expires_at),
        )
        conn.commit()
        return {"outcome": "previewed", "plan_id": plan_id, "expires_at": expires_at,
                "operation_type": "restore", "items": items, "reversible": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
