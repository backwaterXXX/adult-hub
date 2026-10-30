"""Shared, database-backed cron settings for the independent system workers."""

from __future__ import annotations

from common import admin_schedules, db

_NAMES = {
    "library-sync": "Library sync",
    "database-backup": "System database backup",
}


def _cron_for_interval(minutes: int) -> str | None:
    if type(minutes) is not int or minutes < 15:
        return None
    if minutes <= 60 and 60 % minutes == 0:
        return f"*/{minutes} * * * *"
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
                            backup_daily_at="04:30", include_sync=True) -> None:
    """Seed effective defaults once; later config changes never overwrite Admin edits."""
    sync_cron, legacy_interval, legacy_daily = _defaults(sync_daily_at, sync_interval_minutes)
    if backup_daily_at:
        backup_cron, backup_interval, backup_legacy_daily = _defaults(backup_daily_at, None)
    else:
        backup_cron, backup_interval, backup_legacy_daily = _defaults(None, 60)
    values = []
    if include_sync:
        values.append(("library-sync", _NAMES["library-sync"], sync_cron, legacy_interval, legacy_daily))
    values.append(("database-backup", _NAMES["database-backup"], backup_cron, backup_interval, backup_legacy_daily))
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
            "enabled,revision,next_run_at,last_run_at,last_result FROM system_schedules ORDER BY id"
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
            }
            for row in rows
        ]
    finally:
        conn.close()


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
        if expression:
            conn.execute(
                "UPDATE system_schedules SET cron_expression=%s,legacy_interval_minutes=NULL,"
                "legacy_daily_at=NULL,needs_conversion=FALSE,enabled=%s,revision=revision+1,"
                "next_run_at=%s,updated_at=%s WHERE id=%s",
                (expression, enabled, upcoming, instant, schedule_id),
            )
        else:
            conn.execute(
                "UPDATE system_schedules SET enabled=%s,revision=revision+1,next_run_at=%s,updated_at=%s WHERE id=%s",
                (enabled, upcoming, instant, schedule_id),
            )
        from common.admin_audit_events import record_schedule_cron, record_schedule_enabled
        if expression and row[0] != expression and not record_schedule_cron(
                conn, schedule_id=f"system-{schedule_id}", before=row[0], after=expression,
                revision_before=revision):
            raise RuntimeError("Could not record the system schedule cron audit event.")
        if bool(row[1]) != enabled and not record_schedule_enabled(
                conn, schedule_id=f"system-{schedule_id}", before=bool(row[1]), after=enabled,
                revision_before=revision, revision_after=revision + 1):
            raise RuntimeError("Could not record the system schedule enabled audit event.")
        conn.commit()
        return {"outcome": "updated", "revision": revision + 1,
                "cron_expression": expression or None, "enabled": enabled}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_due_system_schedule(db_url, schedule_id: str, *, now=None) -> bool:
    """Claim one due run and advance its next occurrence in the same transaction."""
    instant = admin_schedules._utc(now)
    conn = db.connect(db_url)
    try:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT cron_expression,legacy_interval_minutes,legacy_daily_at,needs_conversion,enabled,next_run_at "
            "FROM system_schedules WHERE id=%s FOR UPDATE", (schedule_id,),
        ).fetchone()
        if not row or not row[4] or row[5] is None or admin_schedules._utc(row[5]) > instant:
            conn.rollback()
            return False
        if row[3]:
            schedule_type = "daily" if row[2] else "interval"
            upcoming = admin_schedules.next_run(schedule_type, row[1], row[2], after=instant)
        else:
            upcoming = admin_schedules.next_cron(row[0], after=instant)
        conn.execute(
            "UPDATE system_schedules SET next_run_at=%s,last_run_at=%s,last_result='processing',updated_at=%s WHERE id=%s",
            (upcoming, instant, instant, schedule_id),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_system_schedule(db_url, schedule_id: str, *, success: bool, now=None) -> None:
    if type(success) is not bool:
        raise ValueError("success must be boolean")
    instant = admin_schedules._utc(now)
    conn = db.connect(db_url)
    try:
        row = conn.execute(
            "SELECT cron_expression,legacy_interval_minutes,legacy_daily_at,needs_conversion,enabled "
            "FROM system_schedules WHERE id=%s", (schedule_id,),
        ).fetchone()
        if not row:
            return
        if not row[4]:
            upcoming = None
        elif row[3]:
            upcoming = admin_schedules.next_run("daily" if row[2] else "interval", row[1], row[2], after=instant)
        else:
            upcoming = admin_schedules.next_cron(row[0], after=instant)
        conn.execute("UPDATE system_schedules SET last_result=%s,next_run_at=%s,updated_at=%s WHERE id=%s",
                     ("done" if success else "failed", upcoming, instant, schedule_id))
        conn.commit()
    finally:
        conn.close()
