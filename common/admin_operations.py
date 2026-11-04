"""Persistent, revision-checked Admin v2 operation previews."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

from psycopg.types.json import Jsonb

from common import db
MAX_QUARANTINE_BATCH = 25
MAX_RESTORE_BATCH = 25
MAX_AUDIT_REPAIR_BATCH = 25
MAX_ORPHAN_QUARANTINE_BATCH = 25
MAX_EXACT_MERGE_SNAPSHOT_VIDEOS = 128
PLAN_TTL_SECONDS = 600
REPAIRABLE_AUDIT_FINDINGS = {"missing_stream", "missing_nfo", "invalid_nfo", "nfo_metadata_mismatch", "missing_artwork"}


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _video_snapshot(conn, video_id: str, config=None) -> tuple[str | None, dict | None]:
    snapshots = _video_snapshots(conn, [video_id], config)
    return snapshots.get(str(video_id), (None, None))


def _video_snapshots(conn, video_ids: list[str], config=None) -> dict[str, tuple[str, dict]]:
    """Build the same revision snapshots with bounded queries for a whole batch."""
    ids = list(dict.fromkeys(str(video_id) for video_id in video_ids))
    if not ids:
        return {}
    columns = db.table_columns(conn, "videos")
    video_rows = conn.execute("SELECT * FROM videos WHERE id = ANY(%s)", (ids,)).fetchall()
    videos = {str(row[columns.index("id")]): dict(zip(columns, row)) for row in video_rows}

    grouped: dict[str, dict[str, list[dict]]] = {
        table: {} for table in ("synced_files", "video_categories", "video_sources")
    }
    table_order = {
        "synced_files": "file_path",
        "video_categories": "category",
        "video_sources": "priority, source_id",
    }
    for table, order in table_order.items():
        table_columns = db.table_columns(conn, table)
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE video_id = ANY(%s) ORDER BY video_id, {order}", (ids,)
        ).fetchall()
        for row in rows:
            record = dict(zip(table_columns, row))
            grouped[table].setdefault(str(record["video_id"]), []).append(record)

    decision_rows = conn.execute(
        "SELECT DISTINCT loser_id FROM video_duplicate_decisions "
        "WHERE state='active' AND loser_id = ANY(%s)", (ids,)
    ).fetchall()
    active_decisions = {str(row[0]) for row in decision_rows}
    all_files = [file for records in grouped["synced_files"].values() for file in records]
    paths = list(dict.fromkeys(file["file_path"] for file in all_files if file.get("file_path")))
    owners_by_path: dict[str, set[str]] = {}
    if paths:
        for path, video_id in conn.execute(
            "SELECT file_path,video_id FROM synced_files WHERE file_path = ANY(%s) ORDER BY file_path,video_id",
            (paths,),
        ).fetchall():
            owners_by_path.setdefault(str(path), set()).add(str(video_id))

    result = {}
    disabled_providers = config.get_disabled_provider_names() if config is not None else set()
    disabled_channels = config.get_disabled_telegram_channel_ids() if config is not None else set()
    for video_id in ids:
        video = videos.get(video_id)
        if video is None:
            continue
        files = grouped["synced_files"].get(video_id, [])
        categories = grouped["video_categories"].get(video_id, [])
        sources = grouped["video_sources"].get(video_id, [])
        shared_paths = []
        for file in files:
            path = file.get("file_path")
            owners = sorted(owner for owner in owners_by_path.get(str(path), set()) if owner != video_id)
            if owners:
                shared_paths.append({"file_path": path, "owners": owners})
        provider = str(video.get("provider") or video_id.split(":", 1)[0])
        parts = video_id.split(":")
        disabled = provider in disabled_providers or (
            provider == "telegram" and len(parts) > 2 and parts[1] in disabled_channels
        )
        snapshot = {
            "video": video,
            "files": files,
            "categories": categories,
            "sources": sources,
            "active_decision": video_id in active_decisions,
            "shared_paths": shared_paths,
            "source_disabled": disabled,
        }
        result[video_id] = (hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest(), snapshot)
    return result


def _active_jobs_with_payload_value(conn, action: str, key: str, value: str):
    if key not in {"video_id", "operation_id", "keeper_id", "mode"}:
        raise ValueError("Unsupported active-job payload key")
    return conn.execute(
        "SELECT id,payload FROM video_duplicate_jobs WHERE action=%s "
        "AND state IN ('queued','processing') "
        "AND CASE WHEN pg_input_is_valid(payload, 'jsonb') "
        f"THEN payload::jsonb->>'{key}' END = %s ORDER BY created_at,id",
        (action, value),
    ).fetchall()


def _active_quarantine_job(conn, video_id: str) -> str | None:
    return _active_quarantine_jobs(conn, [video_id]).get(str(video_id))


def _active_quarantine_jobs(conn, video_ids: list[str]) -> dict[str, str]:
    ids = list(dict.fromkeys(str(video_id) for video_id in video_ids))
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT id,payload FROM video_duplicate_jobs WHERE action='quarantine' "
        "AND state IN ('queued','processing') "
        "AND CASE WHEN pg_input_is_valid(payload, 'jsonb') "
        "THEN payload::jsonb->>'video_id' END = ANY(%s) ORDER BY created_at,id",
        (ids,),
    ).fetchall()
    active: dict[str, str] = {}
    for job_id, raw in rows:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        video_id = str(payload.get("video_id") or "") if isinstance(payload, dict) else ""
        if video_id in ids:
            active.setdefault(video_id, str(job_id))
    return active


def _active_undo_job(conn, operation_id: str) -> str | None:
    for job_id, raw in _active_jobs_with_payload_value(conn, "undo", "operation_id", operation_id):
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and str(payload.get("operation_id") or "") == operation_id:
            return str(job_id)
    return None


def _active_hide_job(conn, keeper_id: str, loser_ids: list[str]) -> str | None:
    target = (str(keeper_id), tuple(sorted(set(map(str, loser_ids)))))
    return _active_hide_jobs(conn, [target]).get(target)


def _active_hide_jobs(conn, targets: list[tuple[str, tuple[str, ...]]]) -> dict[tuple[str, tuple[str, ...]], str]:
    """Find equivalent active hide jobs for several groups in one query."""
    wanted = {
        (str(keeper_id), tuple(sorted(set(map(str, loser_ids)))))
        for keeper_id, loser_ids in targets
    }
    keeper_ids = list(dict.fromkeys(keeper_id for keeper_id, _loser_ids in wanted))
    if not keeper_ids:
        return {}
    rows = conn.execute(
        "SELECT id,payload FROM video_duplicate_jobs WHERE action='hide' "
        "AND state IN ('queued','processing') "
        "AND CASE WHEN pg_input_is_valid(payload, 'jsonb') "
        "THEN payload::jsonb->>'keeper_id' END = ANY(%s) ORDER BY created_at,id",
        (keeper_ids,),
    ).fetchall()
    matches = {}
    for job_id, raw in rows:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        keeper_id = str(payload.get("keeper_id") or "")
        payload_loser_ids = tuple(sorted(set(map(str, payload.get("loser_ids") or []))))
        key = (keeper_id, payload_loser_ids)
        if key in wanted:
            matches.setdefault(key, str(job_id))
    return matches


def _active_orphan_file_job(conn, mode: str, scope: dict, revisions: dict) -> str | None:
    """Reuse an equivalent pending orphan-file operation when previews race."""
    if mode not in {"quarantine", "restore"}:
        return None

    operation_id = str(scope.get("operation_id") or "") if mode == "restore" else ""
    for job_id, raw in _active_jobs_with_payload_value(
        conn, "orphan_file_operation", "mode", mode
    ):
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("mode") != mode:
            continue
        if mode == "restore":
            if _restore_job_matches(payload, operation_id):
                return str(job_id)
            continue
        if _quarantine_plan_matches(conn, payload, scope, revisions):
            return str(job_id)
    return None


def _restore_job_matches(payload: dict, operation_id: str) -> bool:
    return bool(operation_id) and str(payload.get("operation_id") or "") == operation_id


def _quarantine_plan_matches(conn, payload: dict, scope: dict, revisions: dict) -> bool:
    plan_id = str(payload.get("plan_id") or "")
    if not plan_id:
        return False
    plan = conn.execute(
        "SELECT scope,revisions FROM admin_operation_plans WHERE plan_id=%s", (plan_id,)
    ).fetchone()
    if plan is None:
        return False
    other_scope, other_revisions = plan
    try:
        if isinstance(other_scope, str):
            other_scope = json.loads(other_scope)
        if isinstance(other_revisions, str):
            other_revisions = json.loads(other_revisions)
    except (TypeError, ValueError):
        return False
    return (isinstance(other_scope, dict) and isinstance(other_revisions, dict)
            and _canonical(other_scope) == _canonical(scope)
            and _canonical(other_revisions) == _canonical(revisions))


def _active_metadata_job(conn, video_id: str, expected_revision: int, source_fingerprint: str,
                         changes: dict, reset_fields: list) -> str | None:
    return _active_metadata_jobs(conn, {
        video_id: (expected_revision, source_fingerprint, changes, reset_fields),
    }).get(video_id)


def _active_metadata_jobs(conn, targets: dict[str, tuple[int, str, dict, list]]) -> dict[str, str]:
    """Find matching active metadata jobs for a bounded batch in one query."""
    video_ids = list(dict.fromkeys(str(video_id) for video_id in targets))
    if not video_ids:
        return {}
    rows = conn.execute(
        "SELECT id,payload FROM video_duplicate_jobs WHERE action='metadata_update' "
        "AND state IN ('queued','processing') "
        "AND CASE WHEN pg_input_is_valid(payload, 'jsonb') "
        "THEN payload::jsonb->>'video_id' END = ANY(%s) ORDER BY created_at,id",
        (video_ids,),
    ).fetchall()
    matches = {}
    for job_id, raw in rows:
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        video_id = str(payload.get("video_id") or "")
        target = targets.get(video_id)
        if target is None or video_id in matches:
            continue
        expected_revision, source_fingerprint, changes, reset_fields = target
        if (payload.get("expected_revision") == expected_revision
                and payload.get("expected_source_fingerprint") == source_fingerprint
                and payload.get("changes") == changes
                and payload.get("reset_fields") == reset_fields):
            matches[video_id] = str(job_id)
    return matches


def _restore_snapshot_rows(conn, operation_id: str) -> list[tuple[str, dict]]:
    rows = conn.execute(
        "SELECT loser_id,snapshot FROM video_duplicate_decisions "
        "WHERE operation_id=%s AND state='active' ORDER BY loser_id", (operation_id,)
    ).fetchall()
    if len(rows) > MAX_RESTORE_BATCH:
        return []
    result = []
    for video_id, raw in rows:
        try:
            snapshot = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        except (TypeError, ValueError):
            return []
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("video"), dict):
            return []
        if str(snapshot["video"].get("id") or "") != str(video_id):
            return []
        if (not isinstance(snapshot.get("synced_files"), list)
                or not isinstance(snapshot.get("categories"), list)
                or not isinstance(snapshot.get("files"), list)
                or len(snapshot["synced_files"]) > 1000
                or len(snapshot["categories"]) > 1000
                or len(snapshot["files"]) > 4000):
            return []
        if any(not isinstance(record, dict) or not record.get("file_path")
               for record in snapshot["synced_files"]):
            return []
        if any(not isinstance(record, dict) or not record.get("category")
               for record in snapshot["categories"]):
            return []
        if any(not isinstance(move, dict) or not isinstance(move.get("source"), str)
               or not isinstance(move.get("quarantine"), str)
               or not move["source"] or not move["quarantine"]
               for move in snapshot["files"]):
            return []
        result.append((str(video_id), snapshot))
    return result


def preview_quarantine(db_url: str | None, video_ids: list[str], config_path: str | None = None) -> dict:
    """Compatibility facade for :func:`common.admin_operation_quarantine_previews.preview_quarantine`."""
    from common import admin_operation_quarantine_previews as quarantine_previews
    return quarantine_previews.preview_quarantine(db_url, video_ids, config_path)


def preview_hide(db_url: str | None, keeper_id: str, loser_ids: list[str], config_path: str | None = None,
                 rules_version: int | None = None) -> dict:
    """Compatibility facade for :func:`common.admin_operation_duplicate_previews.preview_hide`."""
    from common import admin_operation_duplicate_previews as duplicate_previews
    return duplicate_previews.preview_hide(db_url, keeper_id, loser_ids, config_path, rules_version)


def preview_exact_merge_plan(db_url: str | None, config_path: str | None = None) -> dict:
    """Compatibility facade for :func:`common.admin_operation_duplicate_previews.preview_exact_merge_plan`."""
    from common import admin_operation_duplicate_previews as duplicate_previews
    return duplicate_previews.preview_exact_merge_plan(db_url, config_path)


def preview_metadata_plan(db_url: str | None, video_id: str, expected_revision: int,
                          changes: dict, reset_fields: list, proposal_id: int | None = None) -> dict:
    """Compatibility facade for :func:`common.admin_operation_metadata.preview_metadata_plan`."""
    from common import admin_operation_metadata as metadata
    return metadata.preview_metadata_plan(db_url, video_id, expected_revision, changes, reset_fields, proposal_id)


def preview_metadata_batch_plan(db_url: str | None, video_ids: list[str],
                                changes: dict, reset_fields: list) -> dict:
    """Compatibility facade for :func:`common.admin_operation_metadata.preview_metadata_batch_plan`."""
    from common import admin_operation_metadata as metadata
    return metadata.preview_metadata_batch_plan(db_url, video_ids, changes, reset_fields)


def preview_performer_merge_plan(db_url: str | None, alias: str, canonical: str) -> dict:
    """Persist a revision-bound performer merge preview for the v2 people editor."""
    if (not isinstance(alias, str) or not isinstance(canonical, str)
            or not alias.strip() or not canonical.strip() or len(alias) > 200 or len(canonical) > 200
            or alias.strip().lower() == canonical.strip().lower()):
        return {"outcome": "invalid", "message": "Specify two distinct performer names of at most 200 characters."}
    try:
        from common.fuzzy_performers import preview_performer_merge
    except ImportError:
        from fuzzy_performers import preview_performer_merge
    preview = preview_performer_merge(db_url, alias.strip(), canonical.strip())
    if not preview.get("valid_canonical"):
        return {"outcome": "invalid", "message": "Choose a valid canonical performer name."}
    if preview.get("manual_overrides"):
        return {"outcome": "conflict", "message":
                "Reset manual performer profile overrides before merging these identities.",
                "manual_overrides": preview["manual_overrides"]}
    revision = preview.get("revision")
    if not isinstance(revision, str) or not (revision.startswith("v2:") and len(revision) == 67):
        return {"outcome": "conflict", "message": "Could not fingerprint the current performer records."}
    resolved = str(preview.get("resolved_canonical") or canonical.strip()).strip()
    if not resolved or resolved.lower() == alias.strip().lower():
        return {"outcome": "conflict", "message": "The existing alias chain does not resolve to a distinct performer."}
    item = {"alias": alias.strip(), "canonical": resolved,
            "alias_videos": int(preview.get("alias_videos") or 0),
            "canonical_videos": int(preview.get("canonical_videos") or 0),
            "aliases_affected": int(preview.get("aliases_affected") or 0),
            "has_photo": bool(preview.get("has_photo")),
            "canonical_has_photo": bool(preview.get("canonical_has_photo")),
            "has_bio": bool(preview.get("has_bio")),
            "canonical_has_bio": bool(preview.get("canonical_has_bio"))}
    conn = db.connect(db_url)
    try:
        plan_id = uuid.uuid4().hex
        now, expires_at = int(time.time()), int(time.time()) + PLAN_TTL_SECONDS
        conn.execute(
            "INSERT INTO admin_operation_plans "
            "(plan_id,operation_type,state,scope,revisions,preview,created_at,expires_at) "
            "VALUES (%s,'performer_merge','previewed',%s,%s,%s,to_timestamp(%s),to_timestamp(%s))",
            (plan_id, Jsonb({"alias": alias.strip(), "canonical": resolved}),
             Jsonb({"performer_merge": revision}), Jsonb({"items": [item], "warnings": preview.get("warnings", [])}),
             now, expires_at),
        )
        conn.commit()
        return {"outcome": "previewed", "plan_id": plan_id, "expires_at": expires_at,
                "operation_type": "performer_merge", "items": [item],
                "warnings": preview.get("warnings", [])}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def preview_restore(db_url: str | None, operation_id: str) -> dict:
    """Compatibility facade for :func:`common.admin_operation_quarantine_previews.preview_restore`."""
    from common import admin_operation_quarantine_previews as quarantine_previews
    return quarantine_previews.preview_restore(db_url, operation_id)


def _audit_repair_state(conn, audit_id: str, finding_ids: list[int]) -> dict | None:
    """Compatibility facade for :func:`common.admin_operation_audits._audit_repair_state`."""
    from common import admin_operation_audits as collect
    return collect._audit_repair_state(conn, audit_id, finding_ids)


def preview_audit_repair(db_url: str | None, audit_id: str, finding_ids: list[int]) -> dict:
    """Compatibility facade for :func:`common.admin_operation_audits.preview_audit_repair`."""
    from common import admin_operation_audits as collect
    return collect.preview_audit_repair(db_url, audit_id, finding_ids)


def _audit_import_refresh_state(conn, audit_id: str, finding_ids: list[int]) -> dict | None:
    """Compatibility facade for :func:`common.admin_operation_audits._audit_import_refresh_state`."""
    from common import admin_operation_audits as collect
    return collect._audit_import_refresh_state(conn, audit_id, finding_ids)


def preview_audit_import_refresh(db_url: str | None, audit_id: str, finding_ids: list[int]) -> dict:
    """Compatibility facade for :func:`common.admin_operation_audits.preview_audit_import_refresh`."""
    from common import admin_operation_audits as collect
    return collect.preview_audit_import_refresh(db_url, audit_id, finding_ids)


def _safe_orphan_files(raw, media_root: Path) -> list[dict] | None:
    """Compatibility facade for :func:`common.admin_operation_audits._safe_orphan_files`."""
    from common import admin_operation_audits as collect
    return collect._safe_orphan_files(raw, media_root)


def _orphan_finding_state(conn, audit_id: str, finding_ids: list[int], media_root: Path) -> dict | None:
    """Compatibility facade for :func:`common.admin_operation_audits._orphan_finding_state`."""
    from common import admin_operation_audits as collect
    return collect._orphan_finding_state(conn, audit_id, finding_ids, media_root)


def preview_orphan_file_quarantine(db_url: str | None, audit_id: str, finding_ids: list[int]) -> dict:
    """Compatibility facade for :func:`common.admin_operation_audits.preview_orphan_file_quarantine`."""
    from common import admin_operation_audits as collect
    return collect.preview_orphan_file_quarantine(db_url, audit_id, finding_ids)


def preview_orphan_file_restore(db_url: str | None, operation_id: str) -> dict:
    """Compatibility facade for :func:`common.admin_operation_audits.preview_orphan_file_restore`."""
    from common import admin_operation_audits as collect
    return collect.preview_orphan_file_restore(db_url, operation_id)


def _record_operation_queue_audit(conn, plan_id, operation_type, scope, jobs, reused):
    if isinstance(scope, dict):
        if isinstance(scope.get("video_ids"), list):
            requested_count = len(scope["video_ids"])
        elif isinstance(scope.get("loser_ids"), list):
            requested_count = len(scope["loser_ids"])
        elif isinstance(scope.get("groups"), list):
            requested_count = sum(len(group.get("loser_ids", [])) for group in scope["groups"]
                                  if isinstance(group, dict) and isinstance(group.get("loser_ids"), list))
        elif isinstance(scope.get("finding_ids"), list):
            requested_count = len(scope["finding_ids"])
        else:
            requested_count = 1
    else:
        requested_count = 1
    from common.admin_audit_events import record_operation_queued
    recorded = record_operation_queued(conn, plan_id=plan_id, operation_type=str(operation_type),
                                       requested_count=requested_count, jobs=jobs, reused=bool(reused))
    if not recorded:
        raise RuntimeError("Could not record the operation audit event.")


def execute_operation(db_url: str | None, plan_id: str, config_path: str | None = None) -> dict:
    """Execute a previously previewed operation (compatibility facade)."""
    from common.admin_operation_execution import execute_operation as execute

    return execute(db_url, plan_id, config_path)


def operation_status(db_url: str | None, plan_id: str) -> dict | None:
    """Compatibility facade for :func:`common.admin_operation_history.operation_status`."""
    from common.admin_operation_history import operation_status as read_projection
    return read_projection(db_url, plan_id)


def list_operations(db_url: str | None, limit: int = 20, offset: int = 0) -> dict:
    """Compatibility facade for :func:`common.admin_operation_history.list_operations`."""
    from common.admin_operation_history import list_operations as read_projection
    return read_projection(db_url, limit, offset)
