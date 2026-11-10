"""Persistence and safe projections for external metadata review candidates."""

from __future__ import annotations

import hashlib
import json
import time

from psycopg.types.json import Jsonb

from common import db
from common.json_helpers import json_object as _json_object
from common.video_metadata import (
    MetadataValidationError, metadata_source_fingerprint, load_metadata_states, validate_override_patch,
)


def store_metadata_review_proposal(conn, video_id: str, changes: dict, *, source: str,
                                   external_id: str, source_url: str, confidence: float) -> bool:
    """Store a distinct proposal bundle once, retaining no upstream response body."""
    try:
        normalized, resets = validate_override_patch(changes, [])
    except MetadataValidationError:
        return False
    if resets or not normalized:
        return False
    state = load_metadata_states(conn, [str(video_id)]).get(str(video_id))
    if state is None:
        return False
    canonical = json.dumps({"source": str(source or "")[:80],
                            "external_id": str(external_id or "")[:255],
                            "changes": normalized}, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    try:
        safe_confidence = min(1.0, max(0.0, float(confidence)))
    except (TypeError, ValueError):
        safe_confidence = 0.0
    with conn:
        cursor = conn.execute(
            """INSERT INTO metadata_review_proposals
                   (video_id,candidate_fingerprint,changes,source,external_id,source_url,
                    confidence,expected_revision,expected_source_fingerprint,created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s))
               ON CONFLICT(video_id,candidate_fingerprint) DO NOTHING""",
            (str(video_id), fingerprint, Jsonb(normalized), str(source or "")[:80],
             str(external_id or "")[:255], str(source_url or "")[:2048], safe_confidence,
             state["revision"], metadata_source_fingerprint(state["source"]), int(time.time())),
        )
    return cursor.rowcount == 1


def list_metadata_review_proposals(db_url: str | None, *, limit: int = 30, offset: int = 0) -> dict:
    safe_limit = max(1, min(int(limit), 100))
    safe_offset = max(0, min(int(offset), 1_000_000))
    conn = db.connect(db_url)
    try:
        total = int(conn.execute(
            "SELECT count(*) FROM metadata_review_proposals WHERE status='pending'"
        ).fetchone()[0])
        rows = conn.execute(
            """SELECT p.id,p.video_id,p.changes,p.source,p.external_id,p.source_url,p.confidence,
                      p.expected_revision,p.expected_source_fingerprint,p.created_at,v.title
               FROM metadata_review_proposals p JOIN videos v ON v.id=p.video_id
               WHERE p.status='pending' ORDER BY p.created_at,p.id LIMIT %s OFFSET %s""",
            (safe_limit, safe_offset),
        ).fetchall()
        ids = [str(row[1]) for row in rows]
        states = load_metadata_states(conn, ids)
        items = []
        for row in rows:
            proposal_id, video_id, raw_changes, source, external_id, source_url, confidence, revision, fingerprint, created_at, title = row
            vid = str(video_id)
            changes = raw_changes if isinstance(raw_changes, dict) else _json_object(raw_changes)
            state = states.get(vid)
            stale = (state is None or int(revision) != state["revision"]
                     or str(fingerprint) != metadata_source_fingerprint(state["source"]))
            current = state["metadata"] if state else {}
            items.append({
                "id": int(proposal_id), "video_id": vid,
                "title": str(current.get("title") or title or "Untitled")[:500],
                "current": {field: current.get(field) for field in changes},
                "changes": changes, "source": str(source)[:80],
                "external_id": str(external_id)[:255],
                "source_url": str(source_url)[:2048],
                "confidence": float(confidence or 0),
                "revision": state["revision"] if state else None,
                "stale": bool(stale), "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
            })
        return {"items": items, "total": total, "limit": safe_limit, "offset": safe_offset}
    finally:
        conn.close()


def refresh_metadata_review_proposal(db_url: str | None, proposal_id: int) -> dict:
    """Rebase a pending candidate onto current metadata without applying it.

    A later preview and execution still verify both the revision and source
    fingerprint. Refreshing never changes the candidate or video metadata.
    """
    if type(proposal_id) is not int or proposal_id <= 0:
        return {"outcome": "invalid"}
    conn = db.connect(db_url)
    try:
        row = conn.execute(
            "SELECT video_id,status FROM metadata_review_proposals WHERE id=%s FOR UPDATE", (proposal_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return {"outcome": "not_found"}
        if row[1] != "pending":
            conn.rollback()
            return {"outcome": "conflict"}
        state = load_metadata_states(conn, [str(row[0])]).get(str(row[0]))
        if state is None:
            conn.rollback()
            return {"outcome": "not_found"}
        revision = state["revision"]
        fingerprint = metadata_source_fingerprint(state["source"])
        conn.execute(
            "UPDATE metadata_review_proposals SET expected_revision=%s,expected_source_fingerprint=%s WHERE id=%s",
            (revision, fingerprint, proposal_id),
        )
        conn.commit()
        return {"outcome": "refreshed", "id": proposal_id, "revision": revision}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reject_metadata_review_proposal(db_url: str | None, proposal_id: int) -> dict:
    if type(proposal_id) is not int or proposal_id <= 0:
        return {"outcome": "invalid"}
    conn = db.connect(db_url)
    try:
        row = conn.execute(
            "UPDATE metadata_review_proposals SET status='rejected',reviewed_at=CURRENT_TIMESTAMP "
            "WHERE id=%s AND status='pending' RETURNING id", (proposal_id,),
        ).fetchone()
        if row is None:
            exists = conn.execute("SELECT 1 FROM metadata_review_proposals WHERE id=%s", (proposal_id,)).fetchone()
            conn.commit()
            return {"outcome": "conflict" if exists else "not_found"}
        conn.commit()
        return {"outcome": "rejected", "id": int(row[0])}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
