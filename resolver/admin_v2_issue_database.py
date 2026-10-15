"""Database-backed job, queue, library, and quality issues for Admin v2."""

from __future__ import annotations

from urllib.parse import quote

from common.admin_job_health import PROCESSING_STALE_WARNING_SECONDS, is_processing_stale
from common.job_queue import ALL_ACTIONS
from admin_v2_issue_builder import (
    _issue,
    _quality_issues,
    _timestamp,
)


def collect_database_issues(admin, now, health, issues):
    db_available = False
    try:
        from admin_services import sync_backlog, job_listing, unresolved_failed_task_listing

        backlog = sync_backlog(admin.HUB_DB_URL)
        health["available"]["database"] = bool(backlog.get("available"))
        db_available = bool(backlog.get("available"))
        count = int(backlog.get("queued") or 0) if db_available else 0
        age = backlog.get("oldest_queued_age_seconds") if db_available else None
        if count and age is not None:
            oldest = max(0, now - int(age))
            threshold_minutes = health["thresholds"]["queue_warning_seconds"] // 60
            issues.append(_issue(
                "sync_backlog", "medium" if age < health["thresholds"]["queue_warning_seconds"] else "high",
                "Synchronization queue is backed up",
                f"{count} synchronization tasks are waiting. The oldest has been queued for {int(age) // 60} minutes. High severity starts after {threshold_minutes} minutes.",
                "task", "sync-backlog", oldest, now, "Open tasks",
                "/admin/v2/?job_state=queued&job_action=sync#jobs",
            ))

        if db_available:
            # The failure feed reconciles the full failed/done history in SQL;
            # it must not infer that a failure is open from a capped success page.
            full_failures = unresolved_failed_task_listing(admin.HUB_DB_URL, limit=1000)
            processing_page = job_listing(admin.HUB_DB_URL, limit=1000, state="processing")
            failed_jobs = full_failures.get("jobs") or []
            if full_failures.get("available"):
                health["truncated"]["failed_jobs"] = bool(full_failures.get("truncated"))
                health["truncated"]["done_jobs"] = False
            else:
                health["available"]["database"] = False
                db_available = False
                health["truncated"]["failed_jobs"] = True
            health["truncated"]["done_jobs"] = False
            health["truncated"]["processing_jobs"] = bool(processing_page.get("truncated"))
            for job in processing_page.get("jobs") or []:
                updated_at = _timestamp(job.get("updated_at"))
                if not is_processing_stale(job.get("state"), updated_at, now=now):
                    continue
                job_id = str(job.get("id") or "")[:120]
                raw_action = str(job.get("action") or "")
                action = raw_action if raw_action in ALL_ACTIONS else "task"
                issues.append(_issue(
                    "job_stalled", "high", "Task may be stalled",
                    f"The “{action}” task has reported no worker activity for more than {PROCESSING_STALE_WARNING_SECONDS} seconds. Review its latest stage before taking action.",
                    "task", job_id,
                    updated_at + PROCESSING_STALE_WARNING_SECONDS, now,
                    "Open task details",
                    f"/admin/v2/?job_id={quote(job_id, safe='')}#jobs" if job_id else "/admin/v2/#jobs",
                ))
            for job in failed_jobs:
                failed_at = _timestamp(job.get("updated_at") or job.get("created_at"))
                attempts = int(job.get("attempts") or 1)
                raw_action = str(job.get("action") or "")
                action = raw_action if raw_action in ALL_ACTIONS else "task"
                job_id = str(job.get("id") or "")[:120]
                message = (f"The “{action}” operation failed on {attempts} matching attempts. "
                           "Review the latest task and retry it if appropriate."
                           if attempts > 1 else
                           f"The “{action}” operation failed. Review the task and retry it if appropriate.")
                issues.append(_issue(
                    "job_failure", "high", "Task failed",
                    message,
                    "task", job_id, failed_at, failed_at, "Open task details",
                    f"/admin/v2/?job_id={quote(job_id, safe='')}#jobs" if job_id else "/admin/v2/#jobs",
                ))
                if attempts > 1:
                    issues[-1]["count"] = attempts

        library = admin.admin_data.unavailable_videos(admin.HUB_DB_URL, limit=1, offset=0)
        health["available"]["database"] = bool(library.get("available")) and db_available
        db_available = health["available"]["database"]
        if db_available:
            total = int(library.get("total") or 0)
            if total:
                issues.append(_issue(
                    "unavailable_video", "medium", "Unavailable videos",
                    f"{total} videos in the library are marked unavailable.",
                    "library", "unavailable", None, now, "Show unavailable",
                    "/admin/v2/#library?availability=unavailable",
                ))
    except Exception:
        db_available = False
        health["available"]["database"] = False
    return db_available


def collect_quality_issues(admin, now, health, issues, db_available):
    if db_available:
        try:
            issues.extend(_quality_issues(admin, now))
            health["available"]["quality_audit"] = True
        except Exception:
            issues.append(_issue(
                "quality_audit", "high", "Quality audit summary unavailable",
                "The latest catalog-quality findings could not be loaded. Review Quality and try again.",
                "quality", "latest-audit", now, now, "Review Quality", "/admin/v2/#quality",
            ))
