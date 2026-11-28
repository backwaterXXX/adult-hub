"""Operator alerts for background jobs and catalog metadata backlog."""

from __future__ import annotations

import os
import time

from common import db

FAILED_JOB_ALERT_THRESHOLD = 25
METADATA_BACKLOG_ALERT_THRESHOLD = 5000


def _threshold(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


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
        "items": items,
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
