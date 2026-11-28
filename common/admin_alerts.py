"""Operator alerts for background jobs and catalog metadata backlog."""

from __future__ import annotations

import os
import time

from common import db

FAILED_JOB_ALERT_THRESHOLD = 25
METADATA_BACKLOG_ALERT_THRESHOLD = 5000
SNOOZE_META_PREFIX = "admin_alert_snooze:"
SNOOZEABLE_ALERTS = frozenset({"failed_jobs", "metadata_backlog"})


def _threshold(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _snooze_until(conn, kind: str, now: int) -> int | None:
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key=%s", (SNOOZE_META_PREFIX + kind,)
        ).fetchone()
        until = int(str(row[0])) if row and row[0] is not None else 0
    except Exception:
        return None
    return until if until > now else None


def snooze_alert(db_url: str | None, kind: str, hours: int = 24, *, now: int | None = None) -> dict:
    """Silence one threshold alert for a bounded number of hours."""
    if kind not in SNOOZEABLE_ALERTS:
        raise ValueError("unsupported alert kind")
    if not 1 <= int(hours) <= 168:
        raise ValueError("snooze hours must be between 1 and 168")
    if not db_url:
        raise ValueError("database is unavailable")
    timestamp = int(time.time()) if now is None else int(now)
    until = timestamp + int(hours) * 3600
    conn = db.connect(db_url)
    try:
        with conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES (%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                (SNOOZE_META_PREFIX + kind, str(until)),
            )
    finally:
        conn.close()
    return {"ok": True, "kind": kind, "snoozed_until": until, "hours": int(hours)}


def build_alerts(conn, *, now: int | None = None) -> dict:
    """Return threshold-based alerts from the durable job and audit tables."""
    timestamp = int(time.time()) if now is None else int(now)
    failed_threshold = _threshold("ADMIN_FAILED_JOB_ALERT_THRESHOLD", FAILED_JOB_ALERT_THRESHOLD)
    backlog_threshold = _threshold("ADMIN_METADATA_BACKLOG_ALERT_THRESHOLD", METADATA_BACKLOG_ALERT_THRESHOLD)
    try:
        failed_rows = conn.execute(
            """SELECT action, count(*) FROM video_duplicate_jobs
               WHERE state='failed' AND updated_at >= to_timestamp(%s)
               GROUP BY action ORDER BY count(*) DESC, action""",
            (timestamp - 86400,),
        ).fetchall()
    except Exception:
        failed_rows = []
    by_action = {str(row[0]): int(row[1]) for row in failed_rows}
    failed_total = sum(by_action.values())
    try:
        metadata_open = int(conn.execute(
            "SELECT count(*) FROM admin_audit_items WHERE kind='missing_metadata' AND status='open'"
        ).fetchone()[0] or 0)
    except Exception:
        metadata_open = 0

    items = []
    if failed_total > failed_threshold:
        items.append({
            "kind": "failed_jobs",
            "severity": "warning",
            "count": failed_total,
            "threshold": failed_threshold,
            "message": f"{failed_total} background tasks failed in the last 24 hours (threshold {failed_threshold}).",
        })
    if metadata_open > backlog_threshold:
        items.append({
            "kind": "metadata_backlog",
            "severity": "warning",
            "count": metadata_open,
            "threshold": backlog_threshold,
            "message": f"{metadata_open} open missing-metadata findings exceed the threshold of {backlog_threshold}.",
        })
    active_items = []
    snoozed = {}
    for item in items:
        until = _snooze_until(conn, str(item["kind"]), timestamp)
        if until is None:
            active_items.append(item)
        else:
            snoozed[str(item["kind"])] = until
    return {
        "available": True,
        "failed_jobs_24h": {
            "count": failed_total,
            "threshold": failed_threshold,
            "exceeded": failed_total > failed_threshold,
            "by_action": by_action,
        },
        "metadata_backlog": {
            "count": metadata_open,
            "threshold": backlog_threshold,
            "exceeded": metadata_open > backlog_threshold,
        },
        "items": active_items,
        "snoozed": snoozed,
    }


def read_alerts(db_url: str | None, *, now: int | None = None) -> dict:
    """Open a short-lived connection and return alerts; unavailable is non-fatal."""
    if not db_url:
        return {"available": False, "failed_jobs_24h": {}, "metadata_backlog": {}, "items": []}
    try:
        conn = db.connect(db_url)
    except Exception:
        return {"available": False, "failed_jobs_24h": {}, "metadata_backlog": {}, "items": []}
    try:
        return build_alerts(conn, now=now)
    except Exception:
        return {"available": False, "failed_jobs_24h": {}, "metadata_backlog": {}, "items": []}
    finally:
        conn.close()
