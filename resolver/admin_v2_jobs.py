"""Read-only job queue and history API for Admin v2."""

from __future__ import annotations

import re
import logging

from fastapi import APIRouter, Query, Request
from admin_v2_errors import V2ErrorRoute
from common.admin_job_health import is_processing_stale
from common.job_queue import ALL_ACTIONS
from common.job_stages import ALL_STAGES
from common.job_failures import FAILURE_MESSAGES
from admin_v2_duplicate_routes import (
    MAX_DUPLICATE_ACTION_BYTES as MAX_DUPLICATE_ACTION_BYTES,
    register_duplicate_routes,
)

logger = logging.getLogger(__name__)

_WARNED_JOB_HISTORY_ERROR: bool = False


JOB_STATES = {"all", "active", "queued", "processing", "done", "failed", "cancelled"}
JOB_ACTIONS = frozenset(ALL_ACTIONS)
JOB_STAGE_LABELS = ALL_STAGES
JOB_FAILURE_MESSAGES = FAILURE_MESSAGES


def _availability_summary(result: dict) -> str | None:
    if result.get("local_state") != "availability_checked":
        return None
    counts = result.get("counts")
    if isinstance(counts, dict):
        labels = (
            ("available", "available"),
            ("unavailable", "unavailable"),
            ("transient_error", "temporary error"),
            ("unknown", "inconclusive"),
        )
        values = [counts.get(key, 0) for key, _label in labels]
        skipped = counts.get("skipped", 0)
        if (all(type(value) is int and 0 <= value <= 20 for value in values)
                and type(skipped) is int and 0 <= skipped <= 20
                and sum(values) + skipped <= 20):
            summary = ", ".join(f"{value} {label}" for value, (_key, label) in zip(values, labels))
            if skipped:
                summary += f", {skipped} skipped"
            return f"Playback check: {summary}."
    return "Playback check recorded. Review item details for the latest results."


def _sync_failure_labels(failures: list) -> list[str]:
    summaries = []
    for failure in failures[:20]:
        if not isinstance(failure, dict):
            continue
        label = failure.get("label")
        if not isinstance(label, str) or len(label) > 256:
            continue
        detail = failure.get("detail")
        if detail == "Telegram fetch/history operation failed.":
            summaries.append(f"{label}: {detail}")
        else:
            error = failure.get("error")
            if isinstance(error, str) and re.fullmatch(
                    r"Sync failed \([A-Za-z0-9_]{1,80}\)\.", error):
                summaries.append(f"{label}: {error}")
            else:
                summaries.append(label)
    return summaries


def _sync_summary(result: dict) -> str | None:
    state = result.get("local_state")
    if state == "sync failed":
        failures = result.get("source_failures")
        if isinstance(failures, list):
            summaries = _sync_failure_labels(failures)
            if summaries:
                return "Sync failed: " + "; ".join(summaries) + ". Open Sources for per-source results."
        return "Sync failed. Open Sources to inspect per-source results."
    if state == "sync completed":
        return "Sync completed. Open Sources to review per-source counts."
    return None


def _source_remove_summary(result: dict) -> str | None:
    if result.get("local_state") != "source_removed":
        return None
    videos = result.get("videos_deleted")
    runs = result.get("history_runs_removed")
    if type(videos) is int and videos >= 0 and type(runs) is int and runs >= 0:
        return f"Channel removed; {videos:,} videos and {runs:,} channel-only sync runs deleted."
    return "Channel videos and sync history removed."


def _source_readd_summary(result: dict) -> str | None:
    if result.get("local_state") != "source_readded":
        return None
    ch = result.get("channel_id") or "channel"
    return f"Channel {ch} configuration updated."


def _metadata_summary(result: dict) -> str | None:
    if result.get("local_state") != "metadata_scan_completed":
        return None
    summary = []
    for key, label in (("entities_checked", "checked"), ("entities_enriched", "enriched")):
        count = result.get(key)
        if type(count) is int and 0 <= count <= 1_000_000_000:
            summary.append(f"{label} {count:,} entities")
    completed = result.get("providers_completed")
    if type(completed) is int and 0 <= completed <= 5:
        summary.append(f"{completed} provider steps completed")
    failed = result.get("providers_failed")
    if type(failed) is int and 1 <= failed <= 5:
        noun = "step" if failed == 1 else "steps"
        verb = "an error" if failed == 1 else "errors"
        summary.append(f"{failed} provider {noun} reported {verb}")
    detail = f" {', '.join(summary)}." if summary else " completed."
    return f"External metadata scan{detail} Open System to review cumulative progress."


def _fingerprint_summary(result: dict) -> str | None:
    if result.get("local_state") != "telegram_fingerprint_scan_completed":
        return None
    checked = result.get("videos_checked")
    enriched = result.get("metadata_enriched")
    if (type(checked) is int and 0 <= checked <= 100
            and type(enriched) is int and 0 <= enriched <= checked):
        return f"Telegram fingerprint scan checked {checked} videos and enriched {enriched}."
    return "Telegram fingerprint scan completed."


def _artwork_generate_summary(result: dict) -> str | None:
    local_state = result.get("local_state")
    if local_state == "artwork_generated":
        summary = "Artwork generated from a video frame."
        if result.get("jellyfin_refresh") == "failed":
            summary += " Local result saved; Jellyfin refresh failed."
        elif result.get("jellyfin_refresh") == "skipped":
            summary += " Jellyfin refresh was skipped."
        elif result.get("jellyfin_refresh") == "unavailable":
            summary += " Jellyfin item refresh was unavailable."
        if result.get("jellyfin_library_refresh") == "failed":
            summary += " The global Jellyfin library refresh failed."
        return summary
    if local_state == "artwork_already_valid":
        return "Artwork was already valid when the task started; no changes were made."
    return None


def _artwork_download_summary(result: dict) -> str | None:
    local_state = result.get("local_state")
    if local_state == "artwork_downloaded":
        provider = result.get("provider")
        summary = (f"Artwork downloaded from {provider}."
                   if provider in {"StashDB", "ThePornDB"} else "Artwork downloaded.")
        if result.get("jellyfin_refresh") == "failed":
            summary += " Local result saved; Jellyfin refresh failed."
        return summary
    if local_state == "artwork_not_found":
        return "No matching artwork found on StashDB or ThePornDB."
    if local_state == "artwork_download_failed":
        return "A matching poster was found, but it could not be downloaded."
    if local_state == "artwork_lookup_failed":
        return "The artwork search did not complete; metadata providers reported errors."
    return None


_RESULT_SUMMARIES = {
    "availability_check": _availability_summary,
    "sync": _sync_summary,
    "source_remove": _source_remove_summary,
    "source_readd": _source_readd_summary,
    "metadata_backfill": _metadata_summary,
    "telegram_fingerprint_scan": _fingerprint_summary,
    "artwork_generate": _artwork_generate_summary,
    "artwork_download": _artwork_download_summary,
}


def _generic_result_summary(result: dict) -> str | None:
    if result.get("restore_test") is True:
        return "Backup restored and validated in a disposable database; that database was removed."
    if result.get("backup_created") is True:
        return "Database backup created and verified."
    if result.get("jellyfin_refresh") == "failed":
        return "Local result saved; Jellyfin refresh failed."
    if result.get("refresh_requested") is True:
        return "Jellyfin accepted the refresh request; check the library after its next refresh."
    if result.get("jellyfin_import") == "not_confirmed":
        return "Local metadata saved; Jellyfin accepted the refresh request, but import is not confirmed."
    if result.get("jellyfin_import") == "confirmed":
        return "Jellyfin imported the current local metadata file."
    if result.get("recovery") == "in_progress":
        return "Recovery in progress."
    return None


def _result_summary(job: dict) -> str | None:
    """Return fixed, safe result labels; never forward worker-provided text."""
    result = job.get("result")
    if isinstance(result, dict):
        action = job.get("action")
        summarize = _RESULT_SUMMARIES.get(action) if isinstance(action, str) else None
        summary = summarize(result) if summarize is not None else None
        if summary is None:
            summary = _generic_result_summary(result)
        if summary is not None:
            return summary
    state = str(job.get("state") or "").lower()
    if state == "done":
        return "Completed."
    if state == "failed":
        return "Job failed."
    if state == "cancelled":
        return "Cancelled before the worker started."
    return None


def _source_failure_item(source: dict) -> dict:
    item = {}
    label = source.get("label")
    error = source.get("error")
    if isinstance(label, str) and len(label) <= 256:
        item["label"] = label
    if isinstance(error, str) and re.fullmatch(r"Sync failed \([A-Za-z0-9_]{1,80}\)\.", error):
        item["error"] = error
    return item


def _run_source_failures(run: dict) -> list[dict]:
    failures = []
    for source in run.get("sources", []):
        if not isinstance(source, dict) or source.get("success") is not False:
            continue
        item = _source_failure_item(source)
        if item:
            failures.append(item)
    return failures


def _attach_sync_failures(job: dict, history: dict | None) -> None:
    """Attach bounded per-source context to a legacy sync job result."""
    if job.get("action") != "sync" or not isinstance(job.get("result"), dict):
        return
    result = job["result"]
    if result.get("local_state") != "sync failed" or isinstance(result.get("source_failures"), list):
        return
    if not isinstance(history, dict) or not isinstance(history.get("runs"), list):
        return
    updated_at = job.get("updated_at")
    source_filter = result.get("source")
    for run in history["runs"]:
        if (not isinstance(run, dict) or run.get("ended_at") != updated_at
                or run.get("source_filter") != source_filter):
            continue
        failures = _run_source_failures(run)
        if failures:
            result["source_failures"] = failures[:100]
        return


_LOCAL_STATES = frozenset({"hidden", "dismissed", "restored", "quarantined", "source_removed", "source_readded", "metadata_updated", "metadata_scan_completed", "telegram_fingerprint_scan_completed", "artwork_downloaded", "artwork_generated", "artwork_already_valid", "artwork_not_found", "artwork_download_failed", "artwork_lookup_failed", "availability_checked", "audit_queued", "audit_completed", "metadata_quality_audit_completed", "nfo_repaired", "files_repaired"})
_JELLYFIN_REFRESH_STATES = frozenset({"pending", "updated", "failed", "skipped", "unavailable", "not_required"})
_JELLYFIN_IMPORT_STATES = frozenset({"not_requested", "not_confirmed", "confirmed"})


def _choice(value, allowed) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _audit_mode(job: dict, payload: dict) -> str | None:
    if job.get("action") == "catalog_full_audit":
        return "full"
    if job.get("action") == "metadata_quality_audit":
        return "metadata_quality"
    if job.get("action") == "consistency_audit":
        candidate = payload.get("mode")
        return candidate if candidate in {"file_consistency", "full"} else "full"
    return None


def _job_stage(state: str, progress: dict, result: dict) -> str:
    # These are fixed worker states. Never expose arbitrary result or payload
    # fields: they can contain source URLs, paths, or credentials.
    recorded_stage = progress.get("stage")
    if state == "processing" and recorded_stage in JOB_STAGE_LABELS:
        return recorded_stage
    if state == "queued":
        return "queued"
    if state == "processing" and result.get("jellyfin_refresh") == "pending":
        return "jellyfin_refresh"
    if state == "processing" and result.get("recovery") == "in_progress":
        return "recovery"
    if state == "processing":
        return "processing"
    return state


def _safe_events(job: dict) -> list[dict]:
    safe_events = []
    for event in job.get("events", []) if isinstance(job.get("events"), list) else []:
        if not isinstance(event, dict) or event.get("stage") not in JOB_STAGE_LABELS:
            continue
        safe_event = {"stage": event["stage"]}
        for key in ("updated_at", "completed", "total"):
            value = event.get(key)
            if isinstance(value, int) and value >= 0:
                safe_event[key] = value
        safe_events.append(safe_event)
    return safe_events


def _safe_parameters(job: dict, payload: dict) -> dict:
    parameters = {}
    for key in ("video_id", "source", "channel_id"):
        value = payload.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9:@+_.-]{1,250}", value):
            parameters[key] = value
    if isinstance(payload.get("sync_mode"), str) and payload["sync_mode"] in {"scheduled", "new", "backfill"}:
        parameters["sync_mode"] = payload["sync_mode"]
    if type(job.get("attempts")) is int:
        parameters["attempts"] = job["attempts"]
    return parameters


def _sync_source_id(job: dict, result: dict) -> str | None:
    # A configured source identifier is safe to link to the Sources view;
    # arbitrary worker result fields (including URLs and credentials) stay private.
    source_id = result.get("source")
    if (job.get("action") == "sync" and isinstance(source_id, str)
            and re.fullmatch(r"telegram:[A-Za-z0-9@+_.-]{1,250}", source_id)):
        return source_id
    return None


def _safe_job(job: dict, *, now=None) -> dict:
    """Project persisted data onto fields safe for the v2 UI."""
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    state = str(job.get("state") or "unknown").lower()
    local_state = result.get("local_state")
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    stage = _job_stage(state, progress, result)
    safe_progress = {"stage": stage}
    for key in ("updated_at", "completed", "total"):
        value = progress.get(key)
        if isinstance(value, int) and value >= 0:
            safe_progress[key] = value
    safe_events = _safe_events(job)
    failure_code = job.get("failure_code")
    if not isinstance(failure_code, str) or failure_code not in JOB_FAILURE_MESSAGES:
        failure_code = None
    safe_job = {
        "id": str(job.get("id") or ""),
        "action": str(job.get("action") or "task"),
        "audit_mode": _audit_mode(job, payload),
        "state": state,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "activity_stale": is_processing_stale(state, job.get("updated_at"), now=now),
        "stage": stage,
        "progress": safe_progress,
        "events": safe_events[-20:],
        "failure_code": failure_code,
        "failure_stage": next((event["stage"] for event in reversed(safe_events)
                                if event["stage"] not in {"starting", "completed", "cancelled"}), None)
        if state == "failed" else None,
        "local_state": _choice(local_state, _LOCAL_STATES),
        "jellyfin_refresh": _choice(result.get("jellyfin_refresh"), _JELLYFIN_REFRESH_STATES),
        "jellyfin_library_refresh": _choice(result.get("jellyfin_library_refresh"), _JELLYFIN_REFRESH_STATES),
        "jellyfin_import": _choice(result.get("jellyfin_import"), _JELLYFIN_IMPORT_STATES),
        "error": (
            JOB_FAILURE_MESSAGES.get(job.get("failure_code"))
            or ("Job failed. Inspect local worker logs for details." if job.get("error") else None)
        ),
        "result_summary": _result_summary(job),
        "undo_available": bool(job.get("undo_available")) if job.get("undo_available") is not None else None,
    }
    safe_job["parameters"] = _safe_parameters(job, payload)
    source_id = _sync_source_id(job, result)
    if source_id is not None:
        safe_job["source_id"] = source_id
    return safe_job


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    register_duplicate_routes(router, admin)
    _route_retry_job(router, admin)
    _route_cancel_job(router, admin)
    _route_admin_v2_jobs(router, admin)
    _route_admin_v2_job(router, admin)
    return router


def _route_retry_job(router, admin):
    @router.post("/api/v2/jobs/{job_id}/retry")
    async def retry_job(request: Request, job_id: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            from common.job_queue import read_job, retry_job as enqueue_retry
            existing = await admin.asyncio.to_thread(read_job, admin.HUB_DB_URL, job_id)
            if existing is None:
                return admin.JSONResponse({"error": "Task not found."}, status_code=404)
            new_id = await admin.asyncio.to_thread(enqueue_retry, admin.HUB_DB_URL, job_id)
            if new_id is None:
                return admin.JSONResponse({"error": "This task cannot be retried."}, status_code=400)
            admin._wake_worker("sync" if existing.get("action") == "sync" else "jobs")
            return {"ok": True, "job_id": new_id, "state": "queued", "message": "Task re-queued."}
        except Exception:
            logger.warning("Request failed in retry_job", exc_info=True)
            return admin.JSONResponse({"error": "Could not retry task."}, status_code=500)


def _route_cancel_job(router, admin):
    @router.post("/api/v2/jobs/{job_id}/cancel")
    async def cancel_job(request: Request, job_id: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            from common.job_queue import cancel_queued_job, read_job
            existing = await admin.asyncio.to_thread(read_job, admin.HUB_DB_URL, job_id)
            if existing is not None and existing.get("action") == "source_remove":
                return admin.JSONResponse(
                    {"error": "Channel deletion cannot be cancelled after it is accepted."},
                    status_code=409,
                )
            result = await admin.asyncio.to_thread(cancel_queued_job, admin.HUB_DB_URL, job_id)
            if result["outcome"] == "not_found":
                return admin.JSONResponse({"error": "Task not found."}, status_code=404)
            if result["outcome"] == "conflict":
                return admin.JSONResponse(
                    {"error": "Only queued tasks can be cancelled; refresh task status and try again."},
                    status_code=409,
                )
            return {"ok": True, "state": "cancelled", "message": "Task cancelled."}
        except Exception:
            logger.warning("Request failed in cancel_job", exc_info=True)
            return admin.JSONResponse({"error": "Could not cancel task."}, status_code=500)


def _route_admin_v2_jobs(router, admin):
    @router.get("/api/v2/jobs")
    async def admin_v2_jobs(
        request: Request,
        state: str = Query("all", max_length=20),
        action: str = Query("", max_length=40),
        offset: int = Query(0, ge=0, le=2_147_483_647),
        limit: int = Query(50, ge=1, le=100),
    ):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        global _WARNED_JOB_HISTORY_ERROR
        state = state.strip().lower()
        action = action.strip().lower()
        if state not in JOB_STATES:
            return admin.JSONResponse({"error": "Invalid job state filter."}, status_code=400)
        if action and action not in JOB_ACTIONS:
            return admin.JSONResponse({"error": "Invalid job action filter."}, status_code=400)

        try:
            from admin_services import job_listing

            page = await admin.asyncio.to_thread(
                job_listing,
                admin.HUB_DB_URL,
                limit,
                "" if state == "all" else state,
                action,
                offset,
                include_failure_code=True,
            )
        except Exception as exc:
            if not _WARNED_JOB_HISTORY_ERROR:
                _WARNED_JOB_HISTORY_ERROR = True
                logger.warning("Could not load job history in admin_v2_jobs: %s", exc)
            else:
                logger.debug("Could not load job history in admin_v2_jobs: %s", exc)
            return admin.JSONResponse({"error": "Could not load job history."}, status_code=503)

        if _WARNED_JOB_HISTORY_ERROR:
            _WARNED_JOB_HISTORY_ERROR = False
            logger.info("Job history loading recovered in admin_v2_jobs")

        available = bool(page.get("available"))
        total = int(page.get("total") or 0) if available else 0
        active_total = int(page.get("active_total") or 0) if available else None
        jobs = [_safe_job(job) for job in page.get("jobs", [])] if available else []
        truncated = bool(available and offset + len(jobs) < total)
        return {
            "ok": True,
            "jobs": jobs,
            "total": total,
            "active_total": active_total,
            "offset": offset,
            "limit": limit,
            "truncated": truncated,
            "data": {"available": available, "incomplete": not available},
        }


def _route_admin_v2_job(router, admin):
    @router.get("/api/v2/jobs/{job_id}")
    async def admin_v2_job(request: Request, job_id: str):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            from admin_services import get_job_v2

            result = await admin.asyncio.to_thread(get_job_v2, admin.HUB_DB_URL, job_id)
        except Exception:
            logger.warning("Request failed in admin_v2_job", exc_info=True)
            return admin.JSONResponse({"error": "Could not load job details."}, status_code=503)
        if not result.get("available"):
            return admin.JSONResponse({"error": "Job data is temporarily unavailable."}, status_code=503)
        if result.get("job") is None:
            return admin.JSONResponse({"error": "Job not found."}, status_code=404)
        try:
            history = await admin.asyncio.to_thread(admin._sync_history, 100)
        except Exception:
            history = None
        _attach_sync_failures(result["job"], history)
        return {"ok": True, "job": _safe_job(result["job"])}
