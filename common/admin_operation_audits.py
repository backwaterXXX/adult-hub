"""Preview and revision checks for catalog audit file operations."""

from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path

from psycopg.types.json import Jsonb

from common import db
from common.admin_operations import (
    MAX_AUDIT_REPAIR_BATCH,
    PLAN_TTL_SECONDS,
    REPAIRABLE_AUDIT_FINDINGS,
    _canonical,
    _video_snapshots,
)


def _audit_repair_state(conn, audit_id: str, finding_ids: list[int]) -> dict | None:
    """Return a revision checked, path-free snapshot for selected local file findings."""
    audit = conn.execute(
        "SELECT id FROM video_duplicate_jobs WHERE id=%s AND action='consistency_audit' AND state='done'",
        (audit_id,),
    ).fetchone()
    if audit is None:
        return None
    rows = conn.execute(
        "SELECT id,video_id,kind,status FROM admin_audit_items WHERE job_id=%s AND id=ANY(%s) ORDER BY id",
        (audit_id, finding_ids),
    ).fetchall()
    if len(rows) != len(finding_ids) or any(
            row[1] is None or row[2] not in REPAIRABLE_AUDIT_FINDINGS or row[3] != "open"
            for row in rows):
        return None
    grouped: dict[str, list[dict]] = {}
    for finding_id, video_id, kind, _status in rows:
        grouped.setdefault(str(video_id), []).append({"finding_id": int(finding_id), "kind": str(kind)})
    from common.video_metadata import load_metadata_states
    video_ids = sorted(grouped)
    snapshots = _video_snapshots(conn, video_ids)
    metadata_states = load_metadata_states(conn, video_ids)
    stream_paths_by_video: dict[str, list[str]] = {}
    for video_id, file_path in conn.execute(
        "SELECT video_id,file_path FROM synced_files "
        "WHERE video_id=ANY(%s) AND lower(file_path) LIKE '%%.strm' "
        "ORDER BY video_id,file_path",
        (video_ids,),
    ).fetchall():
        stream_paths_by_video.setdefault(str(video_id), []).append(str(file_path))
    revisions = {}
    items = []
    total_streams = 0
    for video_id in video_ids:
        findings = grouped[video_id]
        snapshot_revision, snapshot = snapshots.get(video_id, (None, None))
        state = metadata_states.get(video_id)
        if snapshot is None or state is None:
            return None
        if any(row["kind"] == "missing_artwork" for row in findings) and not str(state["source"].get("thumbnail") or "").strip():
            return None
        stream_paths = sorted(set(stream_paths_by_video.get(video_id, [])))
        if not stream_paths or len(stream_paths) > 1000:
            return None
        total_streams += len(stream_paths)
        if total_streams > 100:
            return None
        video_revision = hashlib.sha256(_canonical({
            "catalog": snapshot_revision,
            "metadata_revision": state["revision"],
            "source": state["source"],
            "effective": state["metadata"],
            "stream_paths": stream_paths,
            "findings": findings,
        }).encode("utf-8")).hexdigest()
        revisions[video_id] = video_revision
        items.append({"video_id": video_id, "title": str(state["metadata"].get("title") or "Untitled"),
                      "finding_count": len(findings), "file_count": len(stream_paths),
                      "artwork_available": bool(str(state["source"].get("thumbnail") or "").strip()),
                      "findings": findings})
    return {"items": items, "revisions": revisions}

def preview_audit_repair(db_url: str | None, audit_id: str, finding_ids: list[int]) -> dict:
    if (not isinstance(audit_id, str) or not audit_id or len(audit_id) > 100
            or not isinstance(finding_ids, list) or not 1 <= len(finding_ids) <= MAX_AUDIT_REPAIR_BATCH
            or any(type(value) is not int or value <= 0 for value in finding_ids)
            or len(set(finding_ids)) != len(finding_ids)):
        return {"outcome": "invalid", "message": f"Select between 1 and {MAX_AUDIT_REPAIR_BATCH} valid findings."}
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        state = _audit_repair_state(conn, audit_id, finding_ids)
        if state is None:
            conn.rollback()
            return {"outcome": "conflict", "message": "The selected findings are no longer eligible for repair."}
        plan_id = uuid.uuid4().hex
        now = int(time.time())
        expires_at = now + PLAN_TTL_SECONDS
        conn.execute(
            "INSERT INTO admin_operation_plans "
            "(plan_id,operation_type,state,scope,revisions,preview,created_at,expires_at) "
            "VALUES (%s,'audit_repair_nfo','previewed',%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
            (plan_id, Jsonb({"audit_id": audit_id, "finding_ids": finding_ids}),
             Jsonb(state["revisions"]), Jsonb({"items": state["items"]}), now, expires_at),
        )
        conn.commit()
        return {"outcome": "previewed", "plan_id": plan_id, "expires_at": expires_at,
                "operation_type": "audit_repair_nfo", "items": state["items"]}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _audit_import_refresh_state(conn, audit_id: str, finding_ids: list[int]) -> dict | None:
    audit = conn.execute(
        "SELECT 1 FROM video_duplicate_jobs WHERE id=%s AND action='consistency_audit' AND state='done'",
        (audit_id,),
    ).fetchone()
    rows = conn.execute(
        "SELECT id,video_id,kind,status FROM admin_audit_items WHERE job_id=%s AND id=ANY(%s) ORDER BY id",
        (audit_id, finding_ids),
    ).fetchall() if audit else []
    if (len(rows) != len(finding_ids) or any(
            row[1] is None or row[2] not in {"nfo_import_state_missing", "nfo_import_state_mismatch"}
            or row[3] != "open" for row in rows)):
        return None
    from common.video_metadata import load_metadata_states
    grouped: dict[str, list[dict]] = {}
    for finding_id, video_id, kind, _status in rows:
        grouped.setdefault(str(video_id), []).append({"finding_id": int(finding_id), "kind": str(kind)})
    revisions, items = {}, []
    video_ids = sorted(grouped)
    snapshots = _video_snapshots(conn, video_ids)
    metadata_states = load_metadata_states(conn, video_ids)
    for video_id in video_ids:
        findings = grouped[video_id]
        catalog_revision, snapshot = snapshots.get(video_id, (None, None))
        state = metadata_states.get(video_id)
        if snapshot is None or state is None or not snapshot["files"]:
            return None
        revision = hashlib.sha256(_canonical({
            "catalog": catalog_revision, "metadata_revision": state["revision"],
            "source": state["source"], "effective": state["metadata"], "findings": findings,
        }).encode("utf-8")).hexdigest()
        revisions[video_id] = revision
        items.append({"video_id": video_id, "title": str(state["metadata"].get("title") or "Untitled"),
                      "finding_count": len(findings), "findings": findings})
    return {"items": items, "revisions": revisions}

def preview_audit_import_refresh(db_url: str | None, audit_id: str, finding_ids: list[int]) -> dict:
    """Persist a plan to request a Jellyfin refresh for explicit import findings."""
    if (not isinstance(audit_id, str) or not audit_id or len(audit_id) > 100
            or not isinstance(finding_ids, list) or not 1 <= len(finding_ids) <= MAX_AUDIT_REPAIR_BATCH
            or any(type(value) is not int or value <= 0 for value in finding_ids)
            or len(set(finding_ids)) != len(finding_ids)):
        return {"outcome": "invalid", "message": f"Select between 1 and {MAX_AUDIT_REPAIR_BATCH} valid findings."}
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        state = _audit_import_refresh_state(conn, audit_id, finding_ids)
        if state is None:
            conn.rollback()
            return {"outcome": "conflict", "message": "The selected import findings are no longer eligible."}
        items, revisions = state["items"], state["revisions"]
        plan_id = uuid.uuid4().hex
        now, expires_at = int(time.time()), int(time.time()) + PLAN_TTL_SECONDS
        conn.execute(
            "INSERT INTO admin_operation_plans "
            "(plan_id,operation_type,state,scope,revisions,preview,created_at,expires_at) "
            "VALUES (%s,'audit_import_refresh','previewed',%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
            (plan_id, Jsonb({"audit_id": audit_id, "finding_ids": finding_ids}), Jsonb(revisions),
             Jsonb({"items": items}), now, expires_at),
        )
        conn.commit()
        return {"outcome": "previewed", "plan_id": plan_id, "expires_at": expires_at,
                "operation_type": "audit_import_refresh", "items": items}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _safe_orphan_files(raw, media_root: Path) -> list[dict] | None:
    """Compatibility facade for orphan file validation."""
    from common import admin_operation_orphan_previews as implementation
    return implementation._safe_orphan_files(raw, media_root)


def _orphan_finding_state(conn, audit_id: str, finding_ids: list[int], media_root: Path) -> dict | None:
    """Compatibility facade for orphan finding snapshots."""
    from common import admin_operation_orphan_previews as implementation
    return implementation._orphan_finding_state(conn, audit_id, finding_ids, media_root)


def preview_orphan_file_quarantine(db_url: str | None, audit_id: str, finding_ids: list[int]) -> dict:
    """Compatibility facade for orphan file quarantine previews."""
    from common import admin_operation_orphan_previews as implementation
    return implementation.preview_orphan_file_quarantine(db_url, audit_id, finding_ids)


def preview_orphan_file_restore(db_url: str | None, operation_id: str) -> dict:
    """Compatibility facade for orphan file restore previews."""
    from common import admin_operation_orphan_previews as implementation
    return implementation.preview_orphan_file_restore(db_url, operation_id)

