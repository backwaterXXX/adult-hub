"""Local management router, separate from public playback routes.

Reads are blocked for the exact PUBLIC_DOMAIN unless ADMIN_PUBLIC is enabled.
Every mutation requires the same Origin. A configured admin token additionally
requires Bearer authorization; an empty token enables writes without a token.
ADMIN_PUBLIC controls access through the public domain.
"""

import asyncio  # noqa: F401 - shared by the split Admin v2 route modules
import html
import json
import logging
import os
import sys
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from resolver import admin_data
from resolver import admin_security
from common.app_version import __version__ as APP_VERSION
from common.config import (
    DEFAULT_ADMIN_WORKER_CAPABILITIES_PATH,
    DEFAULT_CONFIG_PATH,
    DEFAULT_ENABLE_ADMIN,
    DEFAULT_PUBLIC_DOMAIN,
    get_admin_public,
    get_admin_write_token,
    get_backup_dir,
    get_cache_dir,
    get_media_dir,
    parse_bool,
)

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger("admin")

ENABLE_ADMIN = parse_bool(os.getenv("ENABLE_ADMIN"), DEFAULT_ENABLE_ADMIN, name="ENABLE_ADMIN")
ADMIN_PUBLIC = get_admin_public()
ADMIN_WRITE_TOKEN = get_admin_write_token()
ADMIN_DUPLICATE_TOKEN = os.getenv("ADMIN_DUPLICATE_TOKEN", "")
PUBLIC_DOMAIN = (os.getenv("PUBLIC_DOMAIN") or DEFAULT_PUBLIC_DOMAIN).strip().lower()
HUB_DB_URL = (os.getenv("DATABASE_URL") or os.getenv("STANDALONE_DATABASE_URL") or "").strip() or None
CACHE_DIR = get_cache_dir()
SYNC_STATUS_FILE = CACHE_DIR / "sync_status.json"
WORKER_CAPABILITIES_FILE = Path(os.getenv(
    "ADMIN_WORKER_CAPABILITIES_PATH", str(DEFAULT_ADMIN_WORKER_CAPABILITIES_PATH)))
SYNC_TRIGGER_FILE = CACHE_DIR / "sync.trigger"
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", DEFAULT_CONFIG_PATH))
SYNC_HISTORY_FILE = CACHE_DIR / "sync_history.json"
BACKUP_DIR = get_backup_dir()
BACKUP_RESTORE_DATABASE_URL = os.getenv("BACKUP_RESTORE_DATABASE_URL", "").strip()
MEDIA_DIR = get_media_dir()


def _via_public_domain(request: Request) -> bool:
    return admin_security.via_public_domain(request, PUBLIC_DOMAIN)


def _admin_blocked(request: Request) -> bool:
    if not ENABLE_ADMIN:
        return True
    if ADMIN_PUBLIC:
        return False
    return _via_public_domain(request)


def _db_stats() -> dict:
    return admin_data.db_stats(HUB_DB_URL, CONFIG_PATH, MEDIA_DIR)


def _sync_status() -> dict:
    return admin_data.sync_status(SYNC_STATUS_FILE)


def _sync_history(limit: int = 30, offset: int = 0) -> dict:
    return admin_data.sync_history(SYNC_HISTORY_FILE, CONFIG_PATH, limit, offset)


def _backup_status() -> dict:
    return admin_data.backup_status(BACKUP_DIR)


def _attention_summary() -> dict:
    """Counts answering "what needs action?" for the Overview."""
    try:
        from resolver.admin_services import job_listing, sync_backlog, unresolved_failed_task_summary
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
        logger.debug("Ignored exception in _attention_summary", exc_info=True)
        return {}


def _write_token() -> str | None:
    for val in (ADMIN_WRITE_TOKEN, ADMIN_DUPLICATE_TOKEN):
        if val is not None and str(val) != "" and not str(val).strip():
            # Whitespace-only token must fail closed, never fail open.
            return None
    return (ADMIN_WRITE_TOKEN or "").strip() or (ADMIN_DUPLICATE_TOKEN or "").strip()


def _capabilities() -> dict:
    token = _write_token()
    result = admin_security.capabilities(
        enabled=ENABLE_ADMIN,
        public=ADMIN_PUBLIC,
        token="" if token is None else token,
    )
    if token is None:
        result.update(actions_enabled=False, read_only=True, read_only_reason="invalid_admin_token", token_required=True)
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


def _write_denied(request: Request):
    token = _write_token()
    capabilities = _capabilities()
    if token is None:
        return JSONResponse({"error": "Admin write token configuration is invalid.", **capabilities}, status_code=403)
    if not admin_security.write_authorized(
            request, blocked=_admin_blocked(request), public=ADMIN_PUBLIC, token=token):
        if capabilities["read_only"]:
            message = "This panel is read-only."
        elif token:
            message = "Authorization and matching Origin required."
        else:
            message = "Matching Origin required."
        return JSONResponse({"error": message, **capabilities}, status_code=403)
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
        source = template_path.read_text(encoding="utf-8").replace("__APP_VERSION__", html.escape(APP_VERSION))
    except OSError:
        return HTMLResponse("<h1>Admin v2 is unavailable.</h1>", status_code=503)
    return HTMLResponse(
        source,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )

from resolver import admin_library_v2  # noqa: E402
from resolver import admin_v2_issues  # noqa: E402
from resolver import admin_v2_jobs  # noqa: E402
from resolver import admin_v2_operations  # noqa: E402
from resolver import admin_v2_metadata  # noqa: E402
from resolver import admin_v2_organize  # noqa: E402
from resolver import admin_v2_availability  # noqa: E402
from resolver import admin_v2_backups  # noqa: E402
from resolver import admin_v2_diagnostics  # noqa: E402
from resolver import admin_v2_schedules  # noqa: E402
from resolver import admin_v2_sources  # noqa: E402
from resolver import admin_v2_performers  # noqa: E402
from resolver import admin_v2_maintenance  # noqa: E402
from resolver import admin_v2_status  # noqa: E402
from resolver import admin_v2_quality  # noqa: E402
from resolver import admin_v2_phash  # noqa: E402
from resolver import admin_v2_phash_jobs  # noqa: E402
from resolver import admin_v2_tasks  # noqa: E402
from resolver import admin_v2_telegram_auth  # noqa: E402

_self = sys.modules[__name__]
router.include_router(admin_library_v2.build_router(_self))
router.include_router(admin_v2_issues.build_router(_self))
router.include_router(admin_v2_jobs.build_router(_self))
router.include_router(admin_v2_operations.build_router(_self))
router.include_router(admin_v2_metadata.build_router(_self))
router.include_router(admin_v2_organize.build_router(_self))
router.include_router(admin_v2_availability.build_router(_self))
router.include_router(admin_v2_backups.build_router(_self))
router.include_router(admin_v2_diagnostics.build_router(_self))
router.include_router(admin_v2_schedules.build_router(_self))
router.include_router(admin_v2_sources.build_router(_self))
router.include_router(admin_v2_performers.build_router(_self))
router.include_router(admin_v2_maintenance.build_router(_self))
router.include_router(admin_v2_status.build_router(_self))
router.include_router(admin_v2_quality.build_router(_self))
router.include_router(admin_v2_phash.build_router(_self))
router.include_router(admin_v2_phash_jobs.build_router(_self))
router.include_router(admin_v2_tasks.build_router(_self))
router.include_router(admin_v2_telegram_auth.build_router(_self))
