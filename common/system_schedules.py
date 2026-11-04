"""Shared, database-backed cron settings for the independent system workers."""

from __future__ import annotations

from common import admin_schedules, db

_RUN_LOCKS = {
    "library-sync": 0x41485501,
    "database-backup": 0x41485502,
    "artwork-generation": 0x41485503,
}
_ACTIVE_RUNS = {}

_NAMES = {
    "library-sync": "Library sync",
    "database-backup": "System database backup",
    "artwork-generation": "Daily artwork generation",
}


def _cron_for_interval(minutes: int) -> str | None:
    if type(minutes) is not int or minutes < 15:
        return None
    if minutes <= 60 and 60 % minutes == 0:
        return "0 * * * *" if minutes == 60 else f"*/{minutes} * * * *"
    if minutes == 1440:
        return "0 0 * * *"
    if minutes % 60 == 0 and 24 % (minutes // 60) == 0:
        hours = minutes // 60
        return "0 * * * *" if hours == 1 else f"0 */{hours} * * *"
    return None


def _defaults(daily_at, interval_minutes):
    if daily_at:
        try:
            hour, minute = map(int, str(daily_at).split(":"))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return f"{minute} {hour} * * *", None, None
        except (TypeError, ValueError):
            raise ValueError("Daily schedule must use HH:MM in Europe/Warsaw.") from None
        raise ValueError("Daily schedule must use HH:MM in Europe/Warsaw.")
    try:
        minutes = int(interval_minutes)
    except (TypeError, ValueError):
        minutes = 60
    expression = _cron_for_interval(minutes)
    if expression:
        return expression, None, None
    return None, minutes if 15 <= minutes <= 43200 else 60, None


def ensure_system_schedules(db_url, *, sync_daily_at=None, sync_interval_minutes=60,
                            backup_daily_at="04:30", artwork_daily_at="06:00",
                            include_sync=True, include_artwork=True) -> None:
    """Seed effective defaults once; later config changes never overwrite Admin edits."""
    sync_cron, legacy_interval, legacy_daily = _defaults(sync_daily_at, sync_interval_minutes)
    if backup_daily_at:
        backup_cron, backup_interval, backup_legacy_daily = _defaults(backup_daily_at, None)
    else:
        backup_cron, backup_interval, backup_legacy_daily = _defaults(None, 60)
    artwork_cron, artwork_interval, artwork_legacy_daily = _defaults(artwork_daily_at, None)
    values = []
    if include_sync:
        values.append(("library-sync", _NAMES["library-sync"], sync_cron, legacy_interval, legacy_daily))
    values.append(("database-backup", _NAMES["database-backup"], backup_cron, backup_interval, backup_legacy_daily))
    if include_artwork:
        values.append(("artwork-generation", _NAMES["artwork-generation"], artwork_cron,
                       artwork_interval, artwork_legacy_daily))
    conn = db.connect(db_url)
    try:
        with conn:
            for schedule_id, name, expression, old_interval, old_daily in values:
                needs_conversion = expression is None
                if expression:
                    next_at = admin_schedules.next_cron(expression)
                elif old_daily:
                    next_at = admin_schedules.next_run("daily", None, old_daily)
                else:
                    next_at = admin_schedules.next_run("interval", old_interval, None)
                conn.execute(
                    "INSERT INTO system_schedules (id,name,cron_expression,legacy_interval_minutes,"
                    "legacy_daily_at,needs_conversion,enabled,revision,next_run_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,TRUE,1,%s) ON CONFLICT(id) DO NOTHING",
                    (schedule_id, name, expression, old_interval, old_daily, needs_conversion, next_at),
                )
    finally:
        conn.close()


def list_system_schedules(db_url) -> list[dict]:
    conn = db.connect(db_url)
    try:
        rows = conn.execute(
            "SELECT id,name,cron_expression,legacy_interval_minutes,legacy_daily_at,needs_conversion,"
            "enabled,revision,next_run_at,last_run_at,last_result,last_finished_at "
            "FROM system_schedules ORDER BY id"
        ).fetchall()
        return [
            {
                "id": row[0], "name": row[1], "cron_expression": row[2],
                "legacy_interval_minutes": row[3], "legacy_daily_at": row[4],
                "needs_conversion": bool(row[5]), "enabled": bool(row[6]),
                "revision": int(row[7]),
                "next_run_at": admin_schedules._utc(row[8]).isoformat() if row[8] else None,
                "last_run_at": admin_schedules._utc(row[9]).isoformat() if row[9] else None,
                "last_result": row[10], "timezone": "Europe/Warsaw",
                "last_finished_at": admin_schedules._utc(row[11]).isoformat() if row[11] else None,
            }
            for row in rows
        ]
    finally:
        conn.close()


def _update_system_schedule_row(conn, schedule_id: str, expression: str, enabled: bool,
                                upcoming, instant) -> None:
    if expression:
        conn.execute(
            "UPDATE system_schedules SET cron_expression=%s,legacy_interval_minutes=NULL,"
            "legacy_daily_at=NULL,needs_conversion=FALSE,enabled=%s,revision=revision+1,"
            "next_run_at=%s,updated_at=%s WHERE id=%s",
            (expression, enabled, upcoming, instant.timestamp(), schedule_id),
        )
    else:
        conn.execute(
            "UPDATE system_schedules SET enabled=%s,revision=revision+1,next_run_at=%s,updated_at=%s WHERE id=%s",
            (enabled, upcoming, instant.timestamp(), schedule_id),
        )


def _record_system_schedule_changes(conn, schedule_id: str, row, expression: str, enabled: bool,
                                    revision: int) -> None:
    from common.admin_audit_events import record_schedule_cron, record_schedule_enabled
    if expression and row[0] != expression and not record_schedule_cron(
            conn, schedule_id=f"system-{schedule_id}", before=row[0], after=expression,
            revision_before=revision):
        raise RuntimeError("Could not record the system schedule cron audit event.")
    if bool(row[1]) != enabled and not record_schedule_enabled(
            conn, schedule_id=f"system-{schedule_id}", before=bool(row[1]), after=enabled,
            revision_before=revision, revision_after=revision + 1):
        raise RuntimeError("Could not record the system schedule enabled audit event.")


def set_system_schedule(db_url, schedule_id: str, *, expression: str, enabled: bool,
                        revision: int, now=None) -> dict:
    if schedule_id not in _NAMES:
        return {"outcome": "not_found"}
    if type(enabled) is not bool or type(revision) is not int or revision < 1:
        raise ValueError("Provide a cron expression, enabled state and valid revision.")
    if not isinstance(expression, str):
        raise ValueError("Provide a cron expression.")
    expression = " ".join(expression.split())
    instant = admin_schedules._utc(now)
    upcoming = admin_schedules.next_cron(expression, after=instant) if expression and enabled else None
    conn = db.connect(db_url)
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT cron_expression,enabled,revision,legacy_interval_minutes,legacy_daily_at,needs_conversion "
            "FROM system_schedules WHERE id=%s FOR UPDATE",
            (schedule_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return {"outcome": "not_found"}
        if int(row[2]) != revision:
            conn.rollback()
            return {"outcome": "conflict", "revision": int(row[2])}
        if not expression and not row[5]:
            raise ValueError("Provide a cron expression.")
        if not expression and enabled:
            old_type = "daily" if row[4] else "interval"
            upcoming = admin_schedules.next_run(old_type, row[3], row[4], after=instant)
        _update_system_schedule_row(conn, schedule_id, expression, enabled, upcoming, instant)
        _record_system_schedule_changes(conn, schedule_id, row, expression, enabled, revision)
        conn.commit()
        return {"outcome": "updated", "revision": revision + 1,
                "cron_expression": expression or None, "enabled": enabled}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def request_system_run(db_url, schedule_id: str, *, now=None) -> dict:
    """Request a run for the system worker, serialized with scheduled claims."""
    if schedule_id not in _NAMES:
        return {"outcome": "not_found"}
    instant = admin_schedules._utc(now)
    conn = db.connect(db_url)
    try:
        conn.execute("BEGIN")
        locked = conn.execute("SELECT pg_try_advisory_xact_lock(%s)", (_RUN_LOCKS[schedule_id],)).fetchone()[0]
        if not locked:
            conn.rollback()
            return {"outcome": "conflict"}
        row = conn.execute("SELECT last_result FROM system_schedules WHERE id=%s FOR UPDATE", (schedule_id,)).fetchone()
        if not row:
            conn.rollback()
            return {"outcome": "not_found"}
        if row[0] == "queued":
            conn.rollback()
            return {"outcome": "conflict"}
        conn.execute("UPDATE system_schedules SET last_result='queued',updated_at=%s WHERE id=%s", (instant.timestamp(), schedule_id))
        conn.commit()
        return {"outcome": "queued"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_due_system_schedule(db_url, schedule_id: str, *, now=None) -> bool:
    """Claim a manual or due run; keep a scheduled slot owed until success."""
    if schedule_id not in _RUN_LOCKS:
        return False
    instant = admin_schedules._utc(now)
    conn = db.connect(db_url)
    retained = False
    try:
        conn.execute("BEGIN")
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (_RUN_LOCKS[schedule_id],)).fetchone()[0]:
            conn.rollback()
            return False
        row = conn.execute(
            "SELECT enabled,next_run_at,last_result,revision FROM system_schedules WHERE id=%s FOR UPDATE",
            (schedule_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        manual = row[2] in {"queued", "processing_manual"}
        abandoned = row[2] == "processing"
        due = bool(row[0] and (abandoned or (row[1] is not None and admin_schedules._utc(row[1]) <= instant)))
        if not manual and not due:
            conn.rollback()
            return False
        result = "processing_manual" if manual else "processing"
        conn.execute("UPDATE system_schedules SET last_run_at=%s,last_result=%s,updated_at=%s WHERE id=%s",
                     (instant.timestamp(), result, instant.timestamp(), schedule_id))
        conn.commit()
        _ACTIVE_RUNS[schedule_id] = (conn, manual, int(row[3]), due)
        retained = True
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        if not retained:
            conn.close()


def finish_system_schedule(db_url, schedule_id: str, *, success: bool, now=None,
                           skipped_overlap: bool = False, interrupted: bool = False) -> None:
    if any(type(value) is not bool for value in (success, skipped_overlap, interrupted)):
        raise ValueError("success, skipped_overlap and interrupted must be boolean")
    instant = admin_schedules._utc(now)
    active = _ACTIVE_RUNS.pop(schedule_id, None)
    if active is None:
        return
    conn, manual, revision, slot_due = active
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT cron_expression,legacy_interval_minutes,legacy_daily_at,needs_conversion,enabled,revision "
            "FROM system_schedules WHERE id=%s FOR UPDATE", (schedule_id,),
        ).fetchone()
        if row:
            result = ("queued" if manual and interrupted else "skipped_overlap" if skipped_overlap
                      else "done" if success else "failed")
            if success and row[4] and int(row[5]) == revision and (not manual or slot_due):
                upcoming = (admin_schedules.next_run("daily" if row[2] else "interval", row[1], row[2], after=instant)
                            if row[3] else admin_schedules.next_cron(row[0], after=instant))
                conn.execute("UPDATE system_schedules SET last_result=%s,last_finished_at=%s,"
                             "next_run_at=%s,updated_at=%s WHERE id=%s",
                             (result, instant, upcoming, instant.timestamp(), schedule_id))
            else:
                conn.execute("UPDATE system_schedules SET last_result=%s,last_finished_at=%s,"
                             "updated_at=%s WHERE id=%s",
                             (result, instant, instant.timestamp(), schedule_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
