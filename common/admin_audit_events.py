"""Field-level audit records for supported Admin-managed changes."""

from __future__ import annotations

import json
import re
from common import db
from psycopg.types.json import Jsonb


VIDEO_FIELDS = {"title", "plot", "studio", "performers", "tags", "date_released"}
SOURCE_FIELDS = {
    "enabled", "studio", "max_pages", "min_duration_seconds", "stream_mode",
    "history_batch_size", "tags_whitelist", "tags_blacklist", "sections",
}
GENERIC_SOURCE_FIELDS = {"enabled", "stream_mode", "max_pages", "sections"}
OPERATION_FIELDS = {"operation_type", "requested_count", "job_count", "reused", "state"}
SCHEDULE_FIELDS = {"action", "schedule_type", "interval_minutes", "daily_at", "timezone",
                   "enabled", "revision", "video_count"}
CHANGE_EVENT_TYPES = {"metadata_update", "source_settings_update", "admin_operation_queued",
                      "schedule_created", "schedule_enabled", "performer_profile_update"}
CHANGE_OBJECT_TYPES = {"video", "source_configuration", "operation_plan", "schedule", "performer"}


def record_metadata_change(conn, *, job_id: str, video_id: str,
                           before: dict, after: dict, before_overrides: dict,
                           after_overrides: dict, fields) -> bool:
    """Insert one idempotent, transaction-scoped before/after record."""
    changed = {}
    for field in sorted(set(fields)):
        if field not in VIDEO_FIELDS:
            continue
        old, new = before.get(field), after.get(field)
        was_overridden = field in before_overrides
        is_overridden = field in after_overrides
        if old != new or was_overridden != is_overridden:
            changed[field] = {"before": old, "after": new,
                              "before_overridden": was_overridden,
                              "after_overridden": is_overridden}
    if not changed:
        return False
    encoded = json.dumps(changed, ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) > 32 * 1024:
        raise ValueError("Audit changes exceed the permitted size.")
    changed = json.loads(encoded)
    event_key = f"metadata:{job_id}:{video_id}"
    cursor = conn.execute(
        "INSERT INTO admin_audit_events "
        "(event_key,event_type,object_type,object_id,job_id,changes) "
        "VALUES (%s,'metadata_update','video',%s,%s,%s) ON CONFLICT(event_key) DO NOTHING",
        (event_key, video_id, job_id, Jsonb(changed)),
    )
    return cursor.rowcount == 1


def list_change_events(conn, *, limit: int = 50, offset: int = 0,
                       event_type: str | None = None, object_type: str | None = None,
                       query: str | None = None) -> dict:
    """Return bounded newest-first change history with allowlisted fields only."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if type(offset) is not int or not 0 <= offset <= 2_147_483_647:
        raise ValueError("offset must be a non-negative integer")
    event_type = str(event_type or "")
    object_type = str(object_type or "")
    query = str(query or "").strip()
    if event_type and event_type not in CHANGE_EVENT_TYPES:
        raise ValueError("event_type is not supported")
    if object_type and object_type not in CHANGE_OBJECT_TYPES:
        raise ValueError("object_type is not supported")
    if len(query) > 100 or any(ord(char) < 32 for char in query):
        raise ValueError("query is not supported")
    clauses, params = [], []
    if event_type:
        clauses.append("event_type=%s")
        params.append(event_type)
    if object_type:
        clauses.append("object_type=%s")
        params.append(object_type)
    if query:
        clauses.append("(object_id ILIKE %s OR COALESCE(job_id,'') ILIKE %s)")
        params.extend((f"%{query}%", f"%{query}%"))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT id,created_at,event_type,object_type,object_id,job_id,changes,truncated "
        f"FROM admin_audit_events{where} ORDER BY created_at DESC,id DESC LIMIT %s OFFSET %s",
        (*params, limit, offset),
    ).fetchall()
    total = int(conn.execute(f"SELECT count(*) FROM admin_audit_events{where}", params).fetchone()[0])
    items = []
    for row in rows:
        raw = row[6] if isinstance(row[6], dict) else {}
        if isinstance(row[6], str):
            try:
                decoded = json.loads(row[6])
                raw = decoded if isinstance(decoded, dict) else {}
            except (TypeError, ValueError):
                raw = {}
        changes = {}
        for field, values in raw.items():
            if not isinstance(values, dict):
                continue
            if field in VIDEO_FIELDS:
                changes[field] = {key: values.get(key) for key in (
                    "before", "after", "before_overridden", "after_overridden")}
            elif row[2] == "source_settings_update" and _valid_source_audit_path(field.split(".")):
                changes[field] = {key: values.get(key) for key in ("before", "after")}
            elif row[2] == "admin_operation_queued" and field in OPERATION_FIELDS:
                changes[field] = {key: values.get(key) for key in ("before", "after")}
            elif row[2] in {"schedule_created", "schedule_enabled"} and field in SCHEDULE_FIELDS:
                changes[field] = {key: values.get(key) for key in ("before", "after")}
            elif row[2] == "performer_profile_update" and field in {"bio", "photo", "aliases"}:
                changes[field] = {key: values.get(key) for key in ("before", "after")}
        created_at = row[1].isoformat() if hasattr(row[1], "isoformat") else row[1]
        items.append({"id": int(row[0]), "created_at": created_at, "event_type": row[2],
                      "object_type": row[3], "object_id": row[4], "job_id": row[5],
                      "changes": changes, "truncated": bool(row[7]), "actor_type": "shared_admin"})
    return {"items": items, "total": total, "offset": offset, "limit": limit}


def record_performer_profile_change(conn, *, name: str, revision: int,
                                    before: dict, after: dict, fields) -> bool:
    """Record an idempotent, bounded diff without retaining artwork URLs."""
    if (not isinstance(name, str) or not name.strip() or len(name) > 200
            or type(revision) is not int or revision < 1):
        return False
    changed = {}
    for field in sorted(set(fields)):
        if field == "bio":
            old, new = str(before.get(field) or ""), str(after.get(field) or "")
            changed[field] = {"before": old[:3000], "after": new[:3000]}
        elif field == "photo":
            # Provider URLs can include credentials; record presence only.
            changed[field] = {"before": bool(before.get(field)), "after": bool(after.get(field))}
        elif field == "aliases":
            old = before.get(field) if isinstance(before.get(field), list) else []
            new = after.get(field) if isinstance(after.get(field), list) else []
            changed[field] = {
                "before": [str(item)[:200] for item in old[:100] if isinstance(item, str)],
                "after": [str(item)[:200] for item in new[:100] if isinstance(item, str)],
            }
    if not changed or len(json.dumps(changed, ensure_ascii=False).encode("utf-8")) > 32 * 1024:
        return False
    return _insert_change(
        conn, event_key=f"performer-profile:{name.casefold()}:{revision}",
        event_type="performer_profile_update", object_type="performer", object_id=name,
        changes=changed,
    )


def _insert_change(conn, *, event_key: str, event_type: str, object_type: str,
                   object_id: str, changes: dict) -> bool:
    if not changes or len(json.dumps(changes, ensure_ascii=False).encode("utf-8")) > 32 * 1024:
        return False
    return conn.execute(
        "INSERT INTO admin_audit_events (event_key,event_type,object_type,object_id,changes) "
        "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(event_key) DO NOTHING",
        (event_key, event_type, object_type, object_id, Jsonb(changes)),
    ).rowcount == 1


def record_operation_queued(conn, *, plan_id: str, operation_type: str,
                            requested_count: int, jobs, reused: bool) -> bool:
    """Audit a successfully consumed explicit operation plan, without object IDs/payload."""
    allowed = {"hide", "hide_batch", "quarantine", "restore", "metadata_update", "metadata_update_batch",
               "performer_merge", "audit_repair_nfo", "audit_import_refresh",
               "orphan_file_quarantine", "orphan_file_restore"}
    if (not isinstance(plan_id, str) or not 1 <= len(plan_id) <= 100
            or operation_type not in allowed or type(requested_count) is not int
            or not 1 <= requested_count <= 10000 or not isinstance(jobs, list)
            or type(reused) is not bool):
        return False
    job_count = len(jobs)
    if not 1 <= job_count <= 100:
        return False
    changes = {
        "operation_type": {"before": None, "after": operation_type},
        "requested_count": {"before": None, "after": requested_count},
        "job_count": {"before": None, "after": job_count},
        "reused": {"before": None, "after": reused},
        "state": {"before": "previewed", "after": "queued"},
    }
    return _insert_change(conn, event_key=f"operation:{plan_id}:queued",
                          event_type="admin_operation_queued", object_type="operation_plan",
                          object_id=plan_id, changes=changes)


def record_schedule_created(conn, *, schedule_id: str, action: str, schedule_type: str,
                            interval_minutes: int | None, daily_at: str | None,
                            timezone: str, enabled: bool, video_count: int) -> bool:
    """Record allowlisted effective schedule fields; never store its arbitrary name or IDs."""
    if (not isinstance(schedule_id, str) or not schedule_id or action not in {
            "availability_check", "consistency_audit", "backup_create"}
            or schedule_type not in {"interval", "daily"} or timezone != "Europe/Warsaw"
            or type(enabled) is not bool or type(video_count) is not int or not 0 <= video_count <= 20
            or (interval_minutes is not None and type(interval_minutes) is not int)
            or (daily_at is not None and not isinstance(daily_at, str))):
        return False
    after = {"action": action, "schedule_type": schedule_type,
             "interval_minutes": interval_minutes, "daily_at": daily_at,
             "timezone": timezone, "enabled": enabled, "video_count": video_count}
    return _insert_change(conn, event_key=f"schedule:{schedule_id}:created",
                          event_type="schedule_created", object_type="schedule",
                          object_id=schedule_id,
                          changes={field: {"before": None, "after": value}
                                   for field, value in after.items()})


def record_schedule_enabled(conn, *, schedule_id: str, before: bool,
                            after: bool, revision_before: int,
                            revision_after: int) -> bool:
    if (not isinstance(schedule_id, str) or not schedule_id or type(before) is not bool
            or type(after) is not bool or type(revision_before) is not int
            or type(revision_after) is not int or revision_after != revision_before + 1):
        return False
    return _insert_change(
        conn, event_key=f"schedule:{schedule_id}:enabled:{revision_after}",
        event_type="schedule_enabled", object_type="schedule", object_id=schedule_id,
        changes={"enabled": {"before": before, "after": after},
                 "revision": {"before": revision_before, "after": revision_after}},
    )


def _safe_source_path(path: list[str], channel_aliases: dict[str, str]) -> list[str] | None:
    if not path or any(not isinstance(part, str) or not part or len(part) > 256
                       or any(ord(char) < 32 for char in part) for part in path):
        return None
    provider = path[0]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", provider):
        return None
    if provider != "telegram":
        if len(path) == 2 and path[1] in GENERIC_SOURCE_FIELDS:
            return path
        return None
    if len(path) == 2 and path[1] == "enabled":
        return path
    if len(path) == 3 and path[1] == "defaults" and path[2] in SOURCE_FIELDS:
        return path
    if len(path) == 4 and path[1] == "channels" and path[3] in SOURCE_FIELDS:
        alias = channel_aliases.setdefault(path[2], f"channel_{len(channel_aliases) + 1}")
        return ["telegram", "channels", alias, path[3]]
    return None


def _valid_source_audit_path(path: list[str]) -> bool:
    """Validate the privacy-safe dotted path stored in a source change event."""
    if not path or any(not isinstance(part, str) or not part for part in path):
        return False
    provider = path[0]
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", provider):
        return False
    if provider != "telegram":
        return len(path) == 2 and path[1] in GENERIC_SOURCE_FIELDS
    if len(path) == 2:
        return path[1] == "enabled"
    if len(path) == 3:
        return path[1] == "defaults" and path[2] in SOURCE_FIELDS
    if len(path) == 4:
        return path[1] == "channels" and re.fullmatch(r"channel_[1-9][0-9]*", path[2]) is not None \
            and path[3] in SOURCE_FIELDS
    return False


def record_source_settings_change(db_url: str | None, *, revision: int, changes,
                                  truncated: bool = False) -> bool:
    """Store a safe effective source-config diff; identical replays count as success."""
    if type(revision) is not int or revision < 1 or not isinstance(changes, list):
        return False
    safe = {}
    channel_aliases = {}
    truncated = bool(truncated or len(changes) > 1000)
    def safe_value(value):
        nonlocal truncated
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, str):
            if len(value) > 300:
                truncated = True
            return value[:300]
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            if len(value) > 40 or any(len(v) > 100 for v in value):
                truncated = True
            return [v[:100] for v in value[:40]]
        return None

    for item in changes[:1000]:
        if not isinstance(item, dict) or set(item) != {"path", "before", "after"}:
            continue
        path = item["path"]
        if not isinstance(path, list):
            continue
        field = path[-1]
        if field not in SOURCE_FIELDS:
            continue
        safe_path = _safe_source_path(path, channel_aliases)
        if safe_path is None:
            continue
        key = ".".join(safe_path)
        safe[key] = {"before": safe_value(item["before"]), "after": safe_value(item["after"])}
    if not safe:
        return False
    if len(json.dumps(safe, ensure_ascii=False).encode("utf-8")) > 32 * 1024:
        while safe and len(json.dumps(safe, ensure_ascii=False).encode("utf-8")) > 32 * 1024:
            safe.pop(next(reversed(safe)))
            truncated = True
    conn = db.connect(db_url)
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO admin_audit_events "
                "(event_key,event_type,object_type,object_id,changes,truncated) "
                "VALUES (%s,'source_settings_update','source_configuration','managed_sources',%s,%s) "
                "ON CONFLICT(event_key) DO NOTHING RETURNING event_key",
                (f"source-settings:{revision}", Jsonb(safe), truncated),
            )
            if cursor.fetchone() is not None:
                return True
            existing = conn.execute(
                "SELECT event_type,object_type,object_id,changes,truncated FROM admin_audit_events "
                "WHERE event_key=%s", (f"source-settings:{revision}",),
            ).fetchone()
            existing_changes = existing[3] if existing else None
            if isinstance(existing_changes, str):
                try:
                    existing_changes = json.loads(existing_changes)
                except (TypeError, ValueError):
                    existing_changes = None
            return bool(existing and existing[0] == "source_settings_update"
                        and existing[1] == "source_configuration" and existing[2] == "managed_sources"
                        and existing_changes == safe and bool(existing[4]) == truncated)
    finally:
        conn.close()
