"""Local management router, separate from public playback routes.

Reads are blocked for the exact PUBLIC_DOMAIN unless ADMIN_PUBLIC is enabled.
Every mutation requires the same Origin. A configured admin token additionally
requires Bearer authorization; an empty token enables writes without a token.
ADMIN_PUBLIC controls access through the public domain.
"""

import asyncio  # noqa: F401 - shared by the split Admin v2 route modules
import json
import logging
import os
import sys
from pathlib import Path

from fastapi import APIRouter, Request
import admin_data
import admin_security
from common.app_version import __version__ as APP_VERSION
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger("admin")

ENABLE_ADMIN = os.getenv("ENABLE_ADMIN", "true").lower() == "true"
ADMIN_PUBLIC = os.getenv("ADMIN_PUBLIC", "false").lower() == "true"
ADMIN_WRITE_TOKEN = os.getenv("ADMIN_WRITE_TOKEN", "")
ADMIN_DUPLICATE_TOKEN = os.getenv("ADMIN_DUPLICATE_TOKEN", "")
PUBLIC_DOMAIN = os.getenv("PUBLIC_DOMAIN", "media.example.org").strip().lower()
HUB_DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None
LINKCHECK_LOG_PATH = Path(os.getenv("LINKCHECK_LOG_PATH", "/data/linkcheck.log"))
SYNC_ACTIVITY_LOG_PATH = Path(os.getenv("SYNC_ACTIVITY_LOG_PATH", "/data/cache/sync_activity.log"))
CACHE_DIR = Path(os.getenv("CACHE_DIR", "/cache"))
SYNC_STATUS_FILE = CACHE_DIR / "sync_status.json"
WORKER_CAPABILITIES_FILE = Path(os.getenv(
    "ADMIN_WORKER_CAPABILITIES_PATH", "/data/cache/worker-capabilities.json"))
SYNC_TRIGGER_FILE = CACHE_DIR / "sync.trigger"
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/config.yaml"))
SYNC_HISTORY_FILE = CACHE_DIR / "sync_history.json"
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/data/backups"))
BACKUP_RESTORE_DATABASE_URL = os.getenv("BACKUP_RESTORE_DATABASE_URL", "").strip()
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/media/adult"))


def _playback_diagnostics():
    """Import lazily so an optional diagnostics backend cannot break the panel."""
    from playback_diagnostics import diagnostics
    return diagnostics


_DIAGNOSTIC_REPORT_FIELDS = {
    "report_id", "started_at", "started_epoch", "ended_at", "provider", "video_key",
    "method", "range", "status_code", "outcome", "error_type", "elapsed_ms",
    "file_size_ms", "upstream_first_byte_ms", "downstream_first_byte_ms",
    "upstream_bytes", "downstream_bytes", "upstream_max_gap_ms",
    "downstream_max_gap_ms", "upstream_idle_ms", "downstream_idle_ms",
    "phase", "phase_idle_ms",
    "upstream_samples", "downstream_samples", "samples_truncated", "active",
    "enabled", "enabled_until", "active_count", "saved_count", "retention_days",
    "days", "analysis_limit", "analyzed", "available", "truncated", "by_provider", "reports",
    "requests", "failures", "median_elapsed_ms", "median_start_ms", "median_downstream_gap_ms",
}
_DIAGNOSTIC_EVENT_FIELDS = {"t_s", "bytes"}


def _safe_diagnostic_value(value, *, event=False):
    """Allow only known report fields; never expose exception strings or credentials."""
    allowed = _DIAGNOSTIC_EVENT_FIELDS if event else _DIAGNOSTIC_REPORT_FIELDS
    if isinstance(value, dict):
        return {
            str(key): _safe_diagnostic_value(
                item, event=(key in {"events", "requests", "upstream_samples", "downstream_samples"})
            )
            for key, item in value.items()
            if key in allowed
        }
    if isinstance(value, list):
        return [_safe_diagnostic_value(item, event=event) for item in value[:500]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            return value[:500]
        return value
    return None


def _diagnostic_failure():
    # Backend errors can contain URLs, Telegram references, or tokens.
    return JSONResponse({"error": "Playback diagnostics operation failed."}, status_code=500)


def _via_public_domain(request: Request) -> bool:
    return admin_security.via_public_domain(request, PUBLIC_DOMAIN)


def _admin_blocked(request: Request) -> bool:
    if not ENABLE_ADMIN:
        return True
    if ADMIN_PUBLIC:
        return False
    return _via_public_domain(request)


def _db_stats() -> dict:
    return admin_data.db_stats(HUB_DB_URL, CONFIG_PATH)


def _sync_status() -> dict:
    return admin_data.sync_status(SYNC_STATUS_FILE)


def _sync_history(limit: int = 30) -> dict:
    return admin_data.sync_history(SYNC_HISTORY_FILE, CONFIG_PATH, limit)


def _backup_status() -> dict:
    return admin_data.backup_status(BACKUP_DIR)


def _playback_summary() -> dict:
    diagnostics = _playback_diagnostics()
    reports = diagnostics.list_reports(limit=admin_data.PLAYBACK_ANALYSIS_LIMIT)
    try:
        available = int(diagnostics.status().get("saved_count") or 0)
    except Exception:
        available = len(reports)
    return admin_data.playback_summary(reports, available)


def _linkcheck_tail(n: int = 20) -> dict:
    return admin_data.linkcheck_tail(LINKCHECK_LOG_PATH, n)


def _sync_activity_tail(n: int = 80) -> dict:
    return admin_data.linkcheck_tail(SYNC_ACTIVITY_LOG_PATH, n)


def _attention_summary() -> dict:
    """Counts answering "what needs action?" for the Overview."""
    try:
        from admin_services import job_listing, sync_backlog, unresolved_failed_task_summary
        active = job_listing(HUB_DB_URL, limit=1, state="active")
        failed = unresolved_failed_task_summary(HUB_DB_URL)
        backlog = sync_backlog(HUB_DB_URL)
        library = admin_data.unavailable_videos(HUB_DB_URL, limit=1)
        runs = _sync_history(1).get("runs", [])
        latest = runs[0] if runs else {}
        latest_http = latest.get("http") or {}
        failed_sources = sum(1 for source in latest.get("sources", []) if not source.get("success"))
        failures_available = failed.get("available") is True
        return {
            "pending_tasks": int(active.get("active_total") or 0),
            "failed_tasks": failed.get("count") if failures_available else None,
            "failed_tasks_available": failures_available,
            "unavailable_videos": int(library.get("total") or 0),
            "unavailable_available": bool(library.get("available", True)),
            "sync_backlog_available": bool(backlog.get("available")),
            "sync_backlog_queued": int(backlog.get("queued") or 0),
            "sync_backlog_processing": int(backlog.get("processing") or 0),
            "sync_backlog_oldest_age_seconds": backlog.get("oldest_queued_age_seconds"),
            "last_sync_failed_sources": failed_sources if runs else None,
            "last_sync_rate_limited": int(latest_http.get("rate_limited") or 0) if runs else None,
        }
    except Exception:
        return {}


def _write_token() -> str:
    return ADMIN_WRITE_TOKEN or ADMIN_DUPLICATE_TOKEN


def _capabilities() -> dict:
    result = admin_security.capabilities(enabled=ENABLE_ADMIN, public=ADMIN_PUBLIC, token=_write_token())
    try:
        from common.worker_capabilities import read_worker_capabilities
        worker = read_worker_capabilities(WORKER_CAPABILITIES_FILE)
    except Exception:
        worker = {"available": False, "worker_version": None, "updated_at": None,
                  "age_seconds": None, "actions": []}
    try:
        from common.job_queue import ALL_ACTIONS
        missing_actions = sorted(set(ALL_ACTIONS) - set(worker.get("actions", [])))
    except Exception:
        missing_actions = ["unknown"]
    worker["missing_actions"] = missing_actions
    worker_ready = worker["available"] and not missing_actions
    result["worker"] = worker
    result["worker_ready"] = worker_ready
    if result["actions_enabled"] and not worker_ready:
        reason = "worker_unavailable" if not worker["available"] else "worker_incompatible"
        result.update(actions_enabled=False, read_only=True, read_only_reason=reason)
    return result


def _duplicate_write_blocked(request: Request) -> bool:
    """Compatibility name for the shared mutation policy."""
    return not admin_security.write_authorized(
        request, blocked=_admin_blocked(request), public=ADMIN_PUBLIC, token=_write_token())


def _write_denied(request: Request):
    if _duplicate_write_blocked(request):
        message = "This panel is read-only." if _capabilities()["read_only"] else "Matching Origin required."
        if not _capabilities()["read_only"] and _write_token():
            message = "Authorization and matching Origin required."
        return JSONResponse({"error": message, **_capabilities()}, status_code=403)
    capabilities = _capabilities()
    if capabilities.get("read_only_reason") in {"worker_unavailable", "worker_incompatible"}:
        reason = capabilities["read_only_reason"]
        return JSONResponse({"ok": False, "code": reason,
                             "error": "The sync worker is unavailable or does not report supported actions.",
                             "worker": capabilities["worker"]}, status_code=503)
    return None


def _wake_worker(action: str, **payload) -> None:
    """Write the runner wake signal. The durable job is the source of truth."""
    try:
        SYNC_TRIGGER_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = SYNC_TRIGGER_FILE.with_suffix(".trigger.tmp")
        tmp_file.write_text(json.dumps({"action": action, **payload}))
        os.replace(tmp_file, SYNC_TRIGGER_FILE)
    except OSError as e:
        logger.warning("Could not wake sync runner for %s: %s", action, e)


@router.api_route("", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def admin_page(request: Request):
    return await admin_v2_page(request)


@router.api_route("/v2", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/v2/", methods=["GET", "HEAD"], include_in_schema=False)
async def admin_v2_page(request: Request):
    """Serve the Admin UI."""
    if _admin_blocked(request):
        return HTMLResponse(
            "<h1>403</h1><p>Admin is only available locally (blocked on the public domain).</p>",
            status_code=403,
        )
    template_path = Path(__file__).parent / "templates" / "admin_v2.html"
    try:
        source = template_path.read_text(encoding="utf-8")
    except OSError:
        return HTMLResponse("<h1>Admin v2 is unavailable.</h1>", status_code=503)
    return HTMLResponse(
        source.replace("{{APP_VERSION}}", APP_VERSION),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )

import admin_library_v2  # noqa: E402
import admin_v2_issues  # noqa: E402
import admin_v2_jobs  # noqa: E402
import admin_v2_operations  # noqa: E402
import admin_v2_metadata  # noqa: E402
import admin_v2_organize  # noqa: E402
import admin_v2_availability  # noqa: E402
import admin_v2_audits  # noqa: E402
import admin_v2_backups  # noqa: E402
import admin_v2_diagnostics  # noqa: E402
import admin_v2_schedules  # noqa: E402
import admin_v2_sources  # noqa: E402
import admin_v2_activity  # noqa: E402
import admin_v2_performers  # noqa: E402
import admin_v2_maintenance  # noqa: E402
import admin_v2_status  # noqa: E402
import admin_v2_quality  # noqa: E402
import admin_v2_enrichment  # noqa: E402
import admin_v2_phash  # noqa: E402
import admin_v2_phash_jobs  # noqa: E402
import admin_v2_tasks  # noqa: E402

_self = sys.modules[__name__]
router.include_router(admin_library_v2.build_router(_self))
router.include_router(admin_v2_issues.build_router(_self))
router.include_router(admin_v2_jobs.build_router(_self))
router.include_router(admin_v2_operations.build_router(_self))
router.include_router(admin_v2_metadata.build_router(_self))
router.include_router(admin_v2_organize.build_router(_self))
router.include_router(admin_v2_availability.build_router(_self))
router.include_router(admin_v2_audits.build_router(_self))
router.include_router(admin_v2_backups.build_router(_self))
router.include_router(admin_v2_diagnostics.build_router(_self))
router.include_router(admin_v2_schedules.build_router(_self))
router.include_router(admin_v2_sources.build_router(_self))
router.include_router(admin_v2_activity.build_router(_self))
router.include_router(admin_v2_performers.build_router(_self))
router.include_router(admin_v2_maintenance.build_router(_self))
router.include_router(admin_v2_status.build_router(_self))
router.include_router(admin_v2_quality.build_router(_self))
router.include_router(admin_v2_enrichment.build_router(_self))
router.include_router(admin_v2_phash.build_router(_self))
router.include_router(admin_v2_phash_jobs.build_router(_self))
router.include_router(admin_v2_tasks.build_router(_self))
