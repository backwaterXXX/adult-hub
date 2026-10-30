"""Restart-safe dispatcher for opt-in Admin maintenance schedules."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, time as day_time, timedelta, timezone
from zoneinfo import ZoneInfo

from common import db

LOCAL_ZONE = ZoneInfo("Europe/Warsaw")
ALLOWED_ACTIONS = {"availability_check", "consistency_audit", "metadata_quality_audit", "catalog_full_audit", "backup_create", "duplicate_reconcile"}
_TIME = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


def _cron_fields(expression: str):
    """Parse numeric five-field cron expressions, including lists, ranges and steps."""
    if not isinstance(expression, str) or len(expression) > 100:
        raise ValueError("Cron must have five numeric fields.")
    parts = expression.split()
    if len(parts) != 5:
        raise ValueError("Cron must have five fields: minute hour day month weekday.")
    fields = []
    for part, (lower, upper) in zip(parts, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))):
        values = set()
        for entry in part.split(','):
            match = re.fullmatch(r'(\*|\d+|\d+-\d+)(?:/(\d+))?', entry)
            if not match:
                raise ValueError("Invalid cron field; use numbers, *, ranges, lists and steps.")
            base, step_text = match.groups()
            step = int(step_text or 1)
            if step < 1 or step > upper - lower + 1:
                raise ValueError("Invalid cron step.")
            if base == '*':
                start, end = lower, upper
            elif '-' in base:
                start, end = map(int, base.split('-'))
            else:
                start = int(base)
                end = upper if step_text else start
            if start < lower or end > upper or start > end:
                raise ValueError("Cron value is outside the allowed range.")
            values.update(range(start, end + 1, step))
        if upper == 7 and 7 in values:
            values.add(0)
        fields.append(values)
    return parts, fields


def next_cron(expression: str, *, after=None) -> datetime:
    """Find the next Warsaw wall time; skip DST gaps and the second fold."""
    parts, (minutes, hours, days, months, weekdays) = _cron_fields(expression)
    instant = _utc(after)
    first_day = instant.astimezone(LOCAL_ZONE).date()
    for offset in range(366 * 8):
        date = first_day + timedelta(days=offset)
        day_match = date.day in days
        weekday_match = (date.weekday() + 1) % 7 in weekdays
        if parts[2].startswith('*') or parts[4].startswith('*'):
            date_matches = day_match and weekday_match
        else:
            date_matches = day_match or weekday_match
        if date.month not in months or not date_matches:
            continue
        for hour in sorted(hours):
            for minute in sorted(minutes):
                wall = datetime.combine(date, day_time(hour, minute))
                candidate = wall.replace(tzinfo=LOCAL_ZONE, fold=0).astimezone(timezone.utc)
                if candidate > instant and candidate.astimezone(LOCAL_ZONE).replace(tzinfo=None) == wall:
                    return candidate
    raise ValueError("Cron expression has no matching date within eight years.")


def _schedule_next(schedule_type, interval_minutes, daily_at, payload, *, after):
    expression = payload.get('_cron') if isinstance(payload, dict) else None
    return (next_cron(expression, after=after) if expression else
            next_run(schedule_type, interval_minutes, daily_at, after=after))


def _utc(value: datetime | int | float | None = None) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def next_run(schedule_type: str, interval_minutes: int | None, daily_at: str | None,
             *, after: datetime | None = None) -> datetime:
    """Return the next UTC run, skipping nonexistent DST wall times and using the first fold."""
    instant = _utc(after)
    if schedule_type == "interval":
        if isinstance(interval_minutes, bool) or not isinstance(interval_minutes, int) or not 15 <= interval_minutes <= 43200:
            raise ValueError("Interval must be between 15 minutes and 30 days.")
        return instant + timedelta(minutes=interval_minutes)
    if schedule_type != "daily" or not isinstance(daily_at, str) or not _TIME.fullmatch(daily_at):
        raise ValueError("Choose an interval or a daily time in Europe/Warsaw.")
    hour, minute = map(int, daily_at.split(":"))
    local_now = instant.astimezone(LOCAL_ZONE)
    for offset in range(0, 370):
        date = local_now.date() + timedelta(days=offset)
        wall = datetime.combine(date, day_time(hour, minute))
        candidate = wall.replace(tzinfo=LOCAL_ZONE, fold=0)
        candidate_utc = candidate.astimezone(timezone.utc)
        # A spring-forward wall time has no exact round trip. Skip it to the next date.
        if candidate_utc.astimezone(LOCAL_ZONE).replace(tzinfo=None) != wall:
            continue
        if candidate_utc > instant:
            return candidate_utc
    raise ValueError("Could not calculate the next local schedule time.")


def _validate(name, action, schedule_type, interval_minutes, daily_at, payload):
    if not isinstance(name, str) or not 3 <= len(name.strip()) <= 100:
        raise ValueError("Schedule name must be between 3 and 100 characters.")
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        raise ValueError("Unsupported scheduled action.")
    if schedule_type == "interval":
        next_run(schedule_type, interval_minutes, None)
        if daily_at is not None:
            raise ValueError("Daily time must be empty for an interval schedule.")
    elif schedule_type == "daily":
        next_run(schedule_type, None, daily_at)
        if interval_minutes is not None:
            raise ValueError("Interval must be empty for a daily schedule.")
    else:
        raise ValueError("Unsupported schedule type.")
    if action == "availability_check":
        ids = payload.get("video_ids") if isinstance(payload, dict) else None
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 20
                or any(not isinstance(item, str) or not item or len(item) > 1000 for item in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("Availability schedules need 1 to 20 distinct video IDs.")
        return {"video_ids": ids}
    if payload not in ({}, None):
        raise ValueError("This scheduled action does not accept a payload.")
    return {}


def create_schedule(db_url, *, name, action, schedule_type, interval_minutes=None,
                    daily_at=None, payload=None, now=None) -> dict:
    payload = _validate(name, action, schedule_type, interval_minutes, daily_at, payload or {})
    instant = _utc(now)
    schedule_id = uuid.uuid4().hex
    conn = db.connect(db_url)
    try:
        with conn:
            conn.execute(
                "INSERT INTO admin_schedules (id,name,action,payload,schedule_type,interval_minutes,daily_at,"
                "timezone,enabled,revision,next_run_at,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'Europe/Warsaw',TRUE,1,%s,%s,%s)",
                (schedule_id, name.strip(), action, json.dumps(payload, ensure_ascii=False), schedule_type,
                 interval_minutes, daily_at, next_run(schedule_type, interval_minutes, daily_at, after=instant),
                 instant.timestamp(), instant.timestamp()),
            )
            from common.admin_audit_events import record_schedule_created
            recorded = record_schedule_created(
                conn, schedule_id=schedule_id, action=action, schedule_type=schedule_type,
                interval_minutes=interval_minutes, daily_at=daily_at, timezone="Europe/Warsaw",
                enabled=True, video_count=len(payload.get("video_ids", [])),
            )
            if not recorded:
                raise RuntimeError("Could not record the schedule audit event.")
        return {"id": schedule_id, "revision": 1}
    finally:
        conn.close()


def set_enabled(db_url, schedule_id: str, *, enabled: bool, revision: int, now=None) -> dict:
    if not isinstance(enabled, bool) or isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("Invalid schedule revision or enabled state.")
    conn = db.connect(db_url)
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT schedule_type,interval_minutes,daily_at,revision,enabled,payload FROM admin_schedules WHERE id=%s FOR UPDATE",
            (schedule_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return {"outcome": "not_found"}
        if int(row[3]) != revision:
            conn.rollback()
            return {"outcome": "conflict", "revision": int(row[3])}
        instant = _utc(now)
        payload = json.loads(row[5]) if isinstance(row[5], str) else (row[5] or {})
        upcoming = _schedule_next(row[0], row[1], row[2], payload, after=instant) if enabled else None
        changed = conn.execute(
            "UPDATE admin_schedules SET enabled=%s,revision=revision+1,next_run_at=%s,updated_at=%s WHERE id=%s",
            (enabled, upcoming, instant.timestamp(), schedule_id),
        ).rowcount
        from common.admin_audit_events import record_schedule_enabled
        recorded = record_schedule_enabled(conn, schedule_id=schedule_id, before=bool(row[4]), after=enabled,
                                           revision_before=int(row[3]), revision_after=revision + 1)
        if not recorded:
            raise RuntimeError("Could not record the schedule audit event.")
        conn.commit()
        return {"outcome": "updated", "revision": revision + 1, "enabled": enabled, "changed": changed == 1}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_reconcile_cron(db_url, schedule_id: str, *, expression: str, revision: int, now=None) -> dict:
    if not isinstance(expression, str) or type(revision) is not int or revision < 1:
        raise ValueError("Provide a cron expression and a valid revision.")
    expression = ' '.join(expression.split())
    instant = _utc(now)
    upcoming = next_cron(expression, after=instant)
    conn = db.connect(db_url)
    try:
        conn.execute('BEGIN')
        row = conn.execute("SELECT action,payload,revision,enabled FROM admin_schedules WHERE id=%s FOR UPDATE", (schedule_id,)).fetchone()
        if not row:
            conn.rollback()
            return {'outcome': 'not_found'}
        if row[0] not in {'duplicate_reconcile', 'catalog_full_audit'}:
            raise ValueError("Cron editing is not available for this schedule.")
        if int(row[2]) != revision:
            conn.rollback()
            return {'outcome': 'conflict', 'revision': int(row[2])}
        payload = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
        before = payload.get('_cron')
        payload['_cron'] = expression
        conn.execute("UPDATE admin_schedules SET payload=%s,revision=revision+1,next_run_at=%s,updated_at=%s WHERE id=%s",
                     (json.dumps(payload), upcoming if row[3] else None, instant.timestamp(), schedule_id))
        from common.admin_audit_events import record_schedule_cron
        if not record_schedule_cron(conn, schedule_id=schedule_id, before=before, after=expression, revision_before=revision):
            raise RuntimeError('Could not record the schedule audit event.')
        conn.commit()
        return {'outcome': 'updated', 'revision': revision + 1, 'cron_expression': expression}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_schedules(db_url) -> list[dict]:
    conn = db.connect(db_url)
    try:
        rows = conn.execute(
            "SELECT s.id,s.name,s.action,s.payload,s.schedule_type,s.interval_minutes,s.daily_at,s.timezone,"
            "s.enabled,s.revision,s.next_run_at,s.last_run_at,s.last_job_id,s.last_result,j.state "
            "FROM admin_schedules s LEFT JOIN video_duplicate_jobs j ON j.id=s.last_job_id "
            "ORDER BY s.created_at DESC,s.id"
        ).fetchall()
        result = []
        for row in rows:
            try:
                payload = json.loads(row[3]) if row[3] else {}
            except (ValueError, TypeError):
                payload = {}
            job_state = row[14] if row[14] in {"queued", "processing", "done", "failed", "cancelled"} else None
            stored_result = row[13] if row[13] in {"queued", "processing", "done", "failed", "cancelled", "skipped_overlap"} else None
            result.append({"id": row[0], "name": row[1], "action": row[2],
                           "video_count": len(payload.get("video_ids", [])) if row[2] == "availability_check" else 0,
                           "schedule_type": row[4], "interval_minutes": row[5], "daily_at": row[6],
                           "cron_expression": payload.get('_cron') if row[2] in {'duplicate_reconcile', 'catalog_full_audit'} else None,
                           "timezone": row[7], "enabled": bool(row[8]), "revision": int(row[9]),
                           "next_run_at": _utc(row[10]).isoformat() if row[10] else None,
                           "last_run_at": _utc(row[11]).isoformat() if row[11] else None,
                           "last_job_id": row[12],
                           "last_result": stored_result if stored_result == "skipped_overlap" else (job_state or stored_result),
                           "last_job_state": job_state})
        return result
    finally:
        conn.close()


def dispatch_due_schedules(db_url, *, now=None, limit=20) -> int:
    """Atomically persist one queued job per due schedule and advance its clock."""
    instant = _utc(now)
    conn = db.connect(db_url)
    dispatched = 0
    try:
        conn.execute("BEGIN")
        # Provision the built-in daily audit once. Operator changes to this row
        # are respected on subsequent starts, including disabling the schedule.
        for schedule_id, name, action, payload, daily_at in (
            ("catalog-health-full", "Catalog health: daily full audit", "catalog_full_audit", {}, "03:00"),
        ):
            conn.execute(
                "INSERT INTO admin_schedules (id,name,action,payload,schedule_type,daily_at,timezone,enabled,revision,next_run_at,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,'daily',%s,'Europe/Warsaw',TRUE,1,%s,%s,%s) ON CONFLICT(id) DO NOTHING",
                (schedule_id, name, action, json.dumps(payload), daily_at,
                 next_run("daily", None, daily_at, after=instant), instant.timestamp(), instant.timestamp()),
            )
        due = conn.execute(
            "SELECT id,action,payload,schedule_type,interval_minutes,daily_at,last_job_id "
            "FROM admin_schedules WHERE enabled=TRUE AND next_run_at<=%s "
            "ORDER BY next_run_at,id LIMIT %s FOR UPDATE SKIP LOCKED",
            (instant, max(1, min(int(limit), 100))),
        ).fetchall()
        for schedule_id, action, payload_raw, schedule_type, interval_minutes, daily_at, last_job_id in due:
            try:
                payload = json.loads(payload_raw) if payload_raw else {}
            except (ValueError, TypeError):
                payload = {}
            active = False
            if last_job_id:
                job = conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (last_job_id,)).fetchone()
                active = bool(job and job[0] in {"queued", "processing"})
            upcoming = _schedule_next(schedule_type, interval_minutes, daily_at, payload, after=instant)
            if active:
                conn.execute(
                    "UPDATE admin_schedules SET next_run_at=%s,last_result='skipped_overlap',updated_at=%s WHERE id=%s",
                    (upcoming, instant.timestamp(), schedule_id),
                )
                continue
            payload.pop('_cron', None)
            payload["_schedule_id"] = schedule_id
            job_id = uuid.uuid4().hex
            epoch = int(instant.timestamp())
            conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                "VALUES (%s,%s,'queued',%s,NULL,%s,%s)",
                (job_id, action, json.dumps(payload, ensure_ascii=False), epoch, epoch),
            )
            conn.execute(
                "UPDATE admin_schedules SET next_run_at=%s,last_run_at=%s,last_job_id=%s,last_result='queued',updated_at=%s "
                "WHERE id=%s",
                (upcoming, instant.timestamp(), job_id, instant.timestamp(), schedule_id),
            )
            dispatched += 1
        conn.commit()
        return dispatched
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
