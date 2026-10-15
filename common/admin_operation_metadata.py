"""Revision-checked metadata preview operations for Admin v2."""

from __future__ import annotations

import json
import time
import uuid

from psycopg.types.json import Jsonb

from common import db
from common.admin_operations import MAX_QUARANTINE_BATCH, PLAN_TTL_SECONDS


def preview_metadata_plan(db_url: str | None, video_id: str, expected_revision: int,
                          changes: dict, reset_fields: list, proposal_id: int | None = None) -> dict:
    """Persist a metadata diff as an expiring operation plan."""
    from common.video_metadata import MetadataValidationError, metadata_source_fingerprint, preview_metadata_update
    try:
        preview = preview_metadata_update(db_url, video_id, expected_revision, changes, reset_fields)
    except MetadataValidationError as exc:
        return {"outcome": "invalid", "message": str(exc)}
    if preview["outcome"] != "previewed":
        return preview
    conn = db.connect(db_url)
    conn.row_factory = db.Record
    try:
        # Recheck under the plan creation transaction to avoid persisting a diff
        # after its metadata revision changed between the read and insert.
        from common.video_metadata import load_metadata_state
        current = load_metadata_state(conn, video_id)
        if current is None:
            conn.rollback()
            return {"outcome": "not_found"}
        if current["revision"] != expected_revision:
            conn.rollback()
            return {"outcome": "stale", "revision": current["revision"]}
        source_fingerprint = metadata_source_fingerprint(current["source"])
        if source_fingerprint != preview.get("source_fingerprint"):
            conn.rollback()
            return {"outcome": "stale", "revision": current["revision"]}
        if proposal_id is not None:
            proposal = conn.execute(
                "SELECT video_id,changes,status,expected_revision,expected_source_fingerprint "
                "FROM metadata_review_proposals WHERE id=%s FOR UPDATE", (proposal_id,),
            ).fetchone()
            if proposal is None:
                conn.rollback()
                return {"outcome": "not_found"}
            candidate_changes = proposal[1] if isinstance(proposal[1], dict) else json.loads(proposal[1] or "{}")
            if (str(proposal[0]) != str(video_id) or str(proposal[2]) != "pending"
                    or int(proposal[3]) != int(expected_revision)
                    or str(proposal[4]) != source_fingerprint
                    or set(candidate_changes) != set(preview["changes"]) or preview["reset_fields"]):
                conn.rollback()
                return {"outcome": "stale", "revision": current["revision"]}
        plan_id = uuid.uuid4().hex
        now = int(time.time())
        expires_at = now + PLAN_TTL_SECONDS
        item = {"video_id": video_id,
                "title": str(current["metadata"].get("title") or "Untitled"),
                "file_count": 0}
        scope = {"video_id": video_id, "expected_revision": expected_revision,
                 "source_fingerprint": source_fingerprint,
                 "changes": preview["changes"], "reset_fields": preview["reset_fields"]}
        if proposal_id is not None:
            scope["metadata_proposal_id"] = int(proposal_id)
        conn.execute(
            "INSERT INTO admin_operation_plans "
            "(plan_id,operation_type,state,scope,revisions,preview,created_at,expires_at) "
            "VALUES (%s,'metadata_update','previewed',%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
            (plan_id, Jsonb(scope),
             Jsonb({video_id: expected_revision}), Jsonb({"items": [item], "fields": preview["fields"]}),
             now, expires_at),
        )
        conn.commit()
        return {"outcome": "previewed", "plan_id": plan_id, "expires_at": expires_at,
                "operation_type": "metadata_update", "items": [item], "fields": preview["fields"],
                "reversible": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def preview_metadata_batch_plan(db_url: str | None, video_ids: list[str],
                                changes: dict, reset_fields: list) -> dict:
    """Persist one bounded common metadata patch for an explicit video selection."""
    from common.video_metadata import (
        MetadataValidationError, load_metadata_states, metadata_source_fingerprint,
        resolve_metadata, validate_override_patch,
    )
    if (not isinstance(video_ids, list) or not 1 <= len(video_ids) <= MAX_QUARANTINE_BATCH
            or any(not isinstance(value, str) or not value or len(value) > 1000 for value in video_ids)
            or len(set(video_ids)) != len(video_ids)):
        return {"outcome": "invalid", "message": "Choose between 1 and 25 distinct videos."}
    try:
        normalized, resets = validate_override_patch(changes, reset_fields)
    except MetadataValidationError as exc:
        return {"outcome": "invalid", "message": str(exc)}
    if not normalized and not resets:
        return {"outcome": "invalid", "message": "Choose at least one metadata change."}

    conn = db.connect(db_url)
    try:
        # Use the same catalog-row lock order as the worker so the captured source
        # values and override revisions form one coherent preview snapshot.
        conn.execute(
            "SELECT id FROM videos WHERE id=ANY(%s) ORDER BY id FOR UPDATE",
            (sorted(video_ids),),
        ).fetchall()
        states = load_metadata_states(conn, video_ids)
        missing = [video_id for video_id, state in states.items() if state is None]
        if missing:
            conn.rollback()
            return {"outcome": "not_found", "missing_ids": missing}

        revisions = {video_id: states[video_id]["revision"] for video_id in video_ids}
        fingerprints = {
            video_id: metadata_source_fingerprint(states[video_id]["source"])
            for video_id in video_ids
        }
        items = []
        for video_id in video_ids:
            state = states[video_id]
            after_overrides = dict(state["overrides"])
            after_overrides.update(normalized)
            for field in resets:
                after_overrides.pop(field, None)
            after = resolve_metadata(state["source"], after_overrides)
            fields = {
                field: {"before": state["metadata"].get(field), "after": after.get(field)}
                for field in (*normalized.keys(), *resets)
            }
            items.append({"video_id": video_id,
                          "title": str(state["metadata"].get("title") or "Untitled"),
                          "fields": fields})

        plan_id = uuid.uuid4().hex
        now = int(time.time())
        expires_at = now + PLAN_TTL_SECONDS
        scope = {"video_ids": video_ids, "changes": normalized, "reset_fields": resets,
                 "source_fingerprints": fingerprints}
        conn.execute(
            "INSERT INTO admin_operation_plans "
            "(plan_id,operation_type,state,scope,revisions,preview,created_at,expires_at) "
            "VALUES (%s,'metadata_update_batch','previewed',%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
            (plan_id, Jsonb(scope), Jsonb(revisions), Jsonb({"items": items}), now, expires_at),
        )
        conn.commit()
        return {"outcome": "previewed", "plan_id": plan_id, "expires_at": expires_at,
                "operation_type": "metadata_update_batch", "items": items,
                "reversible": True}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
