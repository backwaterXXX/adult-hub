"""Build sanitized issue-center projections from application status sources."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from common.admin_job_health import (
    PROCESSING_STALE_WARNING_SECONDS,
    timestamp_seconds,
)

QUALITY_ISSUE_LABELS = {
    "missing_metadata": "Catalog metadata is incomplete",
    "missing_nfo": "NFO files are missing",
    "invalid_nfo": "NFO files are invalid",
    "nfo_metadata_mismatch": "NFO metadata differs",
    "missing_artwork": "Artwork files are missing",
}


def _timestamp(value):
    """Normalize supported persisted timestamps to an integer epoch value."""
    return timestamp_seconds(value)


def _iso_time(value):
    """Return an ISO-8601 UTC timestamp for public issue responses."""
    stamp = _timestamp(value)
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _issue(kind, severity, title, message, object_type, object_id,
           first_seen, last_seen, label, route, *,
           operation_id=None, operation_label=None, list_route=None):
    """Build one issue row.

    ``operation_id`` names the operation that reported the problem (a task
    action, a source, an audit category) and is what the Overview groups by;
    it is always passed in, never recovered from the message text.
    ``list_route`` is where a group that covers many objects should link.
    """
    issue = {
        "kind": kind,
        "severity": severity,
        "status": "open",
        "title": title,
        "message": message,
        "object_type": object_type,
        "object_id": str(object_id) if object_id is not None else None,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "action": {"label": label, "route": route},
        "operation_id": str(operation_id if operation_id is not None else object_id or "")[:120],
    }
    if operation_label is not None:
        issue["operation_label"] = str(operation_label)[:120]
    if list_route is not None:
        issue["list_route"] = str(list_route)[:240]
    return issue


def _is_failed_source(source: dict) -> bool:
    if source.get("success") is True:
        return False
    warnings = source.get("warnings")
    if isinstance(warnings, list) and any(
            isinstance(item, str) and item.startswith("history batch: ") for item in warnings):
        return False
    # Old history formats may not carry a success flag. Treat it as failed
    # only when a failure marker is present, never by assuming falsy means bad.
    return source.get("success") is False or bool(source.get("error"))


def _latest_source_failures(history):
    """Return failures from the newest recorded run for each source."""
    runs = history.get("runs") if isinstance(history, dict) else None
    if not isinstance(runs, list):
        return []

    latest = {}
    # History is newest-first. Keep the first appearance per stable source key.
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_time = _timestamp(run.get("ended_at") or run.get("started_at"))
        sources = run.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            source_id = str(source.get("source") or "").strip()
            if source_id and source_id not in latest:
                latest[source_id] = (source, run_time)

    failures = []
    for source_id, (source, run_time) in latest.items():
        if _is_failed_source(source):
            failures.append((source_id, source, run_time))
    return failures


def _quality_issues(admin, now):
    reader = getattr(admin, "_quality_summary", None)
    if not callable(reader):
        from common.admin_quality import read_quality_summary
        reader = read_quality_summary
    summary = reader(admin.HUB_DB_URL)
    if not isinstance(summary, dict) or summary.get("available") is not True:
        raise RuntimeError("Quality summary is unavailable")
    status = summary.get("status")
    if status == "no_complete_audit":
        return [_issue(
            "quality_audit", "low", "No complete quality audit",
            "The retained audit history has no completed run to summarize yet.",
            "quality", "latest-audit", now, now, "Open System status", "/admin/v2/#system",
        )]
    if status != "ready" or not isinstance(summary.get("counts"), dict):
        raise RuntimeError("Quality summary has an unexpected shape")
    latest = summary.get("latest_audit")
    latest_at = _timestamp(latest.get("created_at")) if isinstance(latest, dict) else None
    issues = []
    for category, label in QUALITY_ISSUE_LABELS.items():
        count = summary["counts"].get(category)
        if type(count) is not int or count < 0:
            raise RuntimeError("Quality summary has invalid finding counts")
        if count == 0:
            continue
        issues.append(_issue(
            "quality_finding", "medium", label,
            f"The latest complete audit found {count} open item(s) in this category.",
            "quality", category, latest_at, latest_at, "Open System status", "/admin/v2/#system",
            list_route="/admin/v2/#system",
        ))
        issues[-1]["count"] = count
    return issues


def _latest_source_ids(history):
    """Return source IDs with a recorded latest run, whether it passed or failed."""
    runs = history.get("runs") if isinstance(history, dict) else None
    if not isinstance(runs, list):
        return set()
    latest = set()
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("sources"), list):
            continue
        for source in run["sources"]:
            if isinstance(source, dict):
                source_id = str(source.get("source") or "").strip()
                if source_id:
                    latest.add(source_id)
        # Runs are newest first; source identities from older runs are still
        # present, so a set is enough to distinguish never-run from recorded.
    return latest


def _queue_warning_seconds():
    """Read the documented queue-age threshold without failing on bad env input."""
    try:
        return max(60, min(int(os.environ.get("ADMIN_V2_QUEUE_WARNING_SECONDS", "3600")), 30 * 86400))
    except (TypeError, ValueError):
        return 3600


def _backup_warning_seconds():
    """Default to a 36-hour warning window for normally daily DB backups."""
    try:
        return max(3600, min(int(os.environ.get("ADMIN_V2_BACKUP_WARNING_SECONDS", str(36 * 3600))), 90 * 86400))
    except (TypeError, ValueError):
        return 36 * 3600


ISSUE_SUMMARY_GROUP_LIMIT = 5
_ISSUE_PRIORITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _issue_priority(issue):
    return _ISSUE_PRIORITY.get(issue.get("severity"), len(_ISSUE_PRIORITY))


def _issue_occurrences(issue):
    """Occurrences for one issue row: its recorded count, else a single event."""
    count = issue.get("count")
    return count if type(count) is int and count >= 0 else 1


def _issue_operation_id(issue):
    """The operation a problem belongs to, always taken from structured data."""
    for key in ("operation_id", "object_id"):
        value = issue.get(key)
        if value is not None:
            return str(value)[:120]
    return ""


def _issue_route(issue):
    action = issue.get("action")
    return action.get("route") if isinstance(action, dict) else None


def _issue_summary(issues, health):
    """Summarize the complete open issue set as problem groups for Overview.

    Grouping happens before filtering and pagination so the tiles cannot move
    when the operator narrows the list below.
    """
    open_issues = [issue for issue in issues if isinstance(issue, dict)]
    open_issues = [issue for issue in open_issues if issue.get("status", "open") == "open"]
    grouped = {}
    for issue in open_issues:
        key = (str(issue.get("kind") or "issue"), _issue_operation_id(issue))
        grouped.setdefault(key, []).append(issue)

    summaries = []
    for (kind, operation_id), members in grouped.items():
        newest = max(members, key=lambda item: _timestamp(item.get("last_seen")) or 0)
        severity = min(members, key=_issue_priority).get("severity")
        severity = severity if severity in _ISSUE_PRIORITY else "low"
        routes = {route for route in (_issue_route(item) for item in members) if route}
        if len(routes) > 1:
            # A group covering many objects must not link to an arbitrary one.
            route = next((item.get("list_route") for item in members
                          if item.get("list_route")), None) or _issue_route(newest)
        else:
            route = next(iter(routes), None) or newest.get("list_route")
        label = str(newest.get("title") or kind)[:160]
        operation_label = newest.get("operation_label")
        if operation_label and str(operation_label) not in label:
            label = f"{label} — {operation_label}"[:160]
        action = newest.get("action")
        action_label = str((action.get("label") if isinstance(action, dict) else "") or "Review")[:80]
        summaries.append((
            _ISSUE_PRIORITY[severity],
            -(_timestamp(newest.get("last_seen")) or 0),
            kind,
            operation_id,
            {
                "kind": kind,
                "operation_id": operation_id,
                "label": label,
                "severity": severity,
                "occurrences": sum(_issue_occurrences(item) for item in members),
                "objects": len(members),
                "last_seen": _iso_time(newest.get("last_seen")),
                "action": {"label": action_label, "route": route},
            },
        ))

    summaries.sort(key=lambda row: row[:4])
    incomplete = bool(health.get("incomplete", True)) if isinstance(health, dict) else True
    return {
        "total_open": len(open_issues),
        "groups_total": len(summaries),
        "groups": [row[4] for row in summaries[:ISSUE_SUMMARY_GROUP_LIMIT]],
        "incomplete": incomplete,
    }


def _build_issues(admin, now):
    issues = []
    health = {
        "available": {"sync_history": False, "config": False, "database": False,
                       "backup_inventory": False, "quality_audit": False},
        "freshness": {"sync_history_at": None, "database_checked_at": now, "backup_at": None},
        "truncated": {"failed_jobs": False, "done_jobs": False,
                      "processing_jobs": False, "sync_history": False},
        "thresholds": {"queue_warning_seconds": _queue_warning_seconds(),
                       "backup_warning_seconds": _backup_warning_seconds(),
                       "processing_stale_warning_seconds": PROCESSING_STALE_WARNING_SECONDS},
        "incomplete": True,
    }

    from admin_v2_issue_sources import collect_source_issues
    from admin_v2_issue_database import collect_database_issues, collect_quality_issues
    from admin_v2_issue_backup import collect_backup_issues

    collect_source_issues(admin, now, health, issues)
    db_available = collect_database_issues(admin, now, health, issues)
    collect_quality_issues(admin, now, health, issues, db_available)
    monitor_backups = os.environ.get("ADMIN_V2_MONITOR_BACKUPS", "false").strip().lower() in {"1", "true", "yes", "on"}
    if monitor_backups:
        collect_backup_issues(admin, now, health, issues)

    # Missing configuration or database state is one explicit warning. Avoid
    # presenting failed reads as an empty issue list.
    missing = []
    if not health["available"]["config"]:
        missing.append("source configuration")
    if not health["available"]["database"]:
        missing.append("database or required tables")
    if missing:
        issues.append(_issue(
            "system", "critical", "Could not load complete admin status",
            "Unavailable data: " + " and ".join(missing) + ". The issue list may be incomplete.",
            "system", "admin-data", now, now, "Check system settings",
            "/admin/v2/#system",
        ))

    health["incomplete"] = bool(
        missing
        or health["truncated"]["failed_jobs"]
        or health["truncated"]["done_jobs"]
        or health["truncated"].get("processing_jobs", False)
        or not health["available"]["sync_history"]
        or (monitor_backups and not health["available"]["backup_inventory"])
        or (health["available"]["database"] and not health["available"]["quality_audit"])
        or health["truncated"].get("sync_history", False)
    )
    # Public timestamps use a stable wire format; sorting above uses epochs.
    health["freshness"]["sync_history_at"] = _iso_time(health["freshness"]["sync_history_at"])
    health["freshness"]["backup_at"] = _iso_time(health["freshness"]["backup_at"])
    for issue in issues:
        issue["first_seen"] = _iso_time(issue["first_seen"])
        issue["last_seen"] = _iso_time(issue["last_seen"])
    return issues, health
