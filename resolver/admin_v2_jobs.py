"""Read-only job queue and history API for Admin v2."""

from __future__ import annotations

import re

from fastapi import APIRouter, Query, Request
from admin_v2_errors import V2ErrorRoute
from common.admin_job_health import is_processing_stale
from common.job_queue import ALL_ACTIONS
from common.job_stages import ALL_STAGES
from admin_v2_duplicate_routes import (
    MAX_DUPLICATE_ACTION_BYTES as MAX_DUPLICATE_ACTION_BYTES,
    register_duplicate_routes,
)


JOB_STATES = {"all", "active", "queued", "processing", "done", "failed", "cancelled"}
JOB_ACTIONS = frozenset(ALL_ACTIONS)
JOB_STAGE_LABELS = ALL_STAGES
JOB_FAILURE_MESSAGES = {
    "operation_timeout": "A required operation timed out.",
    "connection_failed": "Could not connect to a required service.",
    "permission_denied": "Access to a required resource was denied.",
    "file_missing": "A required file was not found.",
    "stale_operation": "The operation no longer matches current data. Refresh its preview before retrying.",
    "remote_service_failed": "A required service returned an unsuccessful response.",
    "sync_failed": "Synchronization reported a failure. Open Sources for per-source results.",
    "telegram_history_failed": "A Telegram history request failed. Open Sources for per-source results.",
}


def _result_summary(job: dict) -> str | None:
    """Return fixed, safe result labels; never forward worker-provided text."""
    result = job.get("result")
    if isinstance(result, dict):
        if (job.get("action") == "availability_check"
                and result.get("local_state") == "availability_checked"):
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
        if job.get("action") == "sync":
            if result.get("local_state") == "sync failed":
                failures = result.get("source_failures")
                if isinstance(failures, list):
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
                    if summaries:
                        return "Sync failed: " + "; ".join(summaries) + ". Open Sources for per-source results."
                return "Sync failed. Open Sources to inspect per-source results."
            if result.get("local_state") == "sync completed":
                return "Sync completed. Open Sources to review per-source counts."
        if job.get("action") == "metadata_backfill" and result.get("local_state") == "metadata_scan_completed":
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
        if (job.get("action") == "telegram_fingerprint_scan"
                and result.get("local_state") == "telegram_fingerprint_scan_completed"):
            checked = result.get("videos_checked")
            enriched = result.get("metadata_enriched")
            if (type(checked) is int and 0 <= checked <= 100
                    and type(enriched) is int and 0 <= enriched <= checked):
                return f"Telegram fingerprint scan checked {checked} videos and enriched {enriched}."
            return "Telegram fingerprint scan completed."
        if job.get("action") == "artwork_generate" and result.get("local_state") == "artwork_generated":
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
        if job.get("action") == "artwork_download":
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
        if result.get("restore_test") is True:
            return "Backup restored and validated in a disposable database; that database was removed."
        if result.get("backup_created") is True:
            return "Database backup created and verified."
        if result.get("jellyfin_refresh") == "failed":
            return "Local result saved; Jellyfin refresh failed."
        if result.get("refresh_requested") is True:
            return "Jellyfin accepted the refresh request; run a new audit to confirm the import."
        if result.get("jellyfin_import") == "not_confirmed":
            return "Local metadata saved; Jellyfin accepted the refresh request, but import is not confirmed."
        if result.get("jellyfin_import") == "confirmed":
            return "Jellyfin imported the current local metadata file."
        if result.get("recovery") == "in_progress":
            return "Recovery in progress."
    state = str(job.get("state") or "").lower()
    if state == "done":
        return "Completed."
    if state == "failed":
        return "Job failed."
    if state == "cancelled":
        return "Cancelled before the worker started."
    return None


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
        failures = []
        for source in run.get("sources", []):
            if not isinstance(source, dict) or source.get("success") is not False:
                continue
            item = {}
            label = source.get("label")
            error = source.get("error")
            if isinstance(label, str) and len(label) <= 256:
                item["label"] = label
            if isinstance(error, str) and re.fullmatch(r"Sync failed \([A-Za-z0-9_]{1,80}\)\.", error):
                item["error"] = error
            if item:
                failures.append(item)
        if failures:
            result["source_failures"] = failures[:100]
        return


def _safe_job(job: dict, *, now=None) -> dict:
    """Project persisted data onto fields safe for the v2 UI."""
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    state = str(job.get("state") or "unknown").lower()
    local_state = result.get("local_state")
    refresh_state = result.get("jellyfin_refresh")
    import_state = result.get("jellyfin_import")
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    audit_mode = None
    if job.get("action") == "catalog_full_audit":
        audit_mode = "full"
    elif job.get("action") == "metadata_quality_audit":
        audit_mode = "metadata_quality"
    elif job.get("action") == "consistency_audit":
        candidate = payload.get("mode")
        audit_mode = candidate if candidate in {"file_consistency", "full"} else "full"
    # These are fixed worker states. Never expose arbitrary result or payload
    # fields: they can contain source URLs, paths, or credentials.
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    recorded_stage = progress.get("stage")
    if state == "processing" and recorded_stage in JOB_STAGE_LABELS:
        stage = recorded_stage
    elif state == "queued":
        stage = "queued"
    elif state == "processing" and refresh_state == "pending":
        stage = "jellyfin_refresh"
    elif state == "processing" and result.get("recovery") == "in_progress":
        stage = "recovery"
    elif state == "processing":
        stage = "processing"
    else:
        stage = state
    safe_progress = {"stage": stage}
    for key in ("updated_at", "completed", "total"):
        value = progress.get(key)
        if isinstance(value, int) and value >= 0:
            safe_progress[key] = value
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
    safe_job = {
        "id": str(job.get("id") or ""),
        "action": str(job.get("action") or "task"),
        "audit_mode": audit_mode,
        "state": state,
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "activity_stale": is_processing_stale(state, job.get("updated_at"), now=now),
        "stage": stage,
        "progress": safe_progress,
        "events": safe_events[-20:],
        "local_state": local_state if isinstance(local_state, str) and local_state in {"hidden", "dismissed", "restored", "quarantined", "metadata_updated", "metadata_scan_completed", "telegram_fingerprint_scan_completed", "artwork_downloaded", "artwork_generated", "artwork_not_found", "artwork_download_failed", "artwork_lookup_failed", "availability_checked", "audit_queued", "audit_completed", "metadata_quality_audit_completed", "nfo_repaired", "files_repaired"} else None,
        "jellyfin_refresh": refresh_state if isinstance(refresh_state, str) and refresh_state in {"pending", "updated", "failed", "skipped", "unavailable", "not_required"} else None,
        "jellyfin_library_refresh": result.get("jellyfin_library_refresh") if isinstance(result.get("jellyfin_library_refresh"), str) and result.get("jellyfin_library_refresh") in {"pending", "updated", "failed", "skipped", "unavailable", "not_required"} else None,
        "jellyfin_import": import_state if isinstance(import_state, str) and import_state in {"not_requested", "not_confirmed", "confirmed"} else None,
        "error": (
            JOB_FAILURE_MESSAGES.get(job.get("failure_code"))
            or ("Job failed. Inspect local worker logs for details." if job.get("error") else None)
        ),
        "result_summary": _result_summary(job),
        "undo_available": bool(job.get("undo_available")) if job.get("undo_available") is not None else None,
    }
    source_id = result.get("source")
    if (job.get("action") == "sync" and isinstance(source_id, str)
            and re.fullmatch(r"telegram:[A-Za-z0-9@+_.-]{1,250}", source_id)):
        # A configured source identifier is safe to link to the Sources view;
        # arbitrary worker result fields (including URLs and credentials) stay private.
        safe_job["source_id"] = source_id
    return safe_job


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    register_duplicate_routes(router, admin)

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
            return admin.JSONResponse({"error": "Could not retry task."}, status_code=500)

    @router.post("/api/v2/jobs/{job_id}/cancel")
    async def cancel_job(request: Request, job_id: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            from common.job_queue import cancel_queued_job
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
            return admin.JSONResponse({"error": "Could not cancel task."}, status_code=500)

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
        except Exception:
            return admin.JSONResponse({"error": "Could not load job history."}, status_code=503)

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

    @router.get("/api/v2/jobs/{job_id}")
    async def admin_v2_job(request: Request, job_id: str):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            from admin_services import get_job_v2

            result = await admin.asyncio.to_thread(get_job_v2, admin.HUB_DB_URL, job_id)
        except Exception:
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

    return router
