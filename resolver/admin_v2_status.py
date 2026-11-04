"""Sanitized overview status endpoint for the Admin V2 interface."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import math
import re
import time
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Request
from admin_v2_errors import V2ErrorRoute
import logging

logger = logging.getLogger(__name__)



def _sync_stale_after_seconds(config_path, db_url=None):
    """Allow two configured cron periods before calling the sync status stale."""
    if db_url:
        try:
            from common import admin_schedules, system_schedules
            item = next((row for row in system_schedules.list_system_schedules(db_url)
                         if row["id"] == "library-sync"), None)
            if item and item["enabled"]:
                if item["cron_expression"]:
                    first = admin_schedules.next_cron(item["cron_expression"])
                    second = admin_schedules.next_cron(item["cron_expression"], after=first)
                    return max(300, int((second - first).total_seconds()) * 2)
                if item["needs_conversion"]:
                    if item["legacy_daily_at"]:
                        return 48 * 60 * 60
                    if item["legacy_interval_minutes"]:
                        return max(300, int(item["legacy_interval_minutes"]) * 120)
            elif item:
                return None
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "_sync_stale_after_seconds", exc_info=True)
    try:
        from common.config_loader import load_config

        config = load_config(str(config_path))
        schedule = config.sync
        interval = 24 * 60 * 60 if schedule.daily_at else int(schedule.interval_minutes) * 60
        return max(300, interval * 2) if interval > 0 else None
    except Exception as _exc:
        logger.debug("Ignored exception in %s", "_sync_stale_after_seconds", exc_info=True)
        return None


def _sanitized_sync_status(sync, sync_stale_after):
    """Return the small, safe sync-freshness contract shared by status views."""
    sync_result = {}
    if isinstance(sync, dict):
        sync_available = (not sync.get("error") and sync.get("available", True) is not False
                          and sync.get("exists", True) is not False)
        sync_result["available"] = sync_available
        sync_result["running"] = sync_available and sync.get("running") is True
        sync_result["freshness"] = "unknown"
        if sync_available:
            if sync_result["running"]:
                sync_result["freshness"] = "fresh"
            else:
                updated_at = sync.get("updated_at")
                if (sync_stale_after is not None and type(updated_at) in {int, float}
                        and math.isfinite(updated_at)):
                    age = time.time() - updated_at
                    if age >= 0:
                        sync_result["freshness"] = (
                            "stale" if age > sync_stale_after else "fresh"
                        )
        last_success = sync.get("last_success") if sync_available else None
        if type(last_success) is bool:
            sync_result["last_success"] = last_success
        if not sync_available:
            sync_result["status_text"] = "Status unavailable"
        elif sync_result["running"]:
            sync_result["status_text"] = "Sync in progress"
        elif sync_result["freshness"] == "stale":
            sync_result["status_text"] = "Status stale"
        elif last_success is True:
            sync_result["status_text"] = "Last run succeeded"
        elif last_success is False:
            sync_result["status_text"] = "Last run failed"
        else:
            sync_result["status_text"] = "No completed run"
    else:
        sync_result = {"available": False, "running": False, "freshness": "unknown",
                       "status_text": "Status unavailable"}
    return sync_result


def _sync_history_for_status(admin):
    """Read the newest synchronization run, tolerating an absent history file."""
    reader = getattr(admin, "_sync_history", None)
    if not callable(reader):
        return {"runs": [], "error": "History reader unavailable"}
    try:
        return reader(1)
    except Exception as _exc:
        logger.debug("Ignored exception in %s", "_sync_history_for_status", exc_info=True)
        return {"runs": [], "error": "Sync history is unavailable."}


def _iso_run_timestamp(value):
    """Normalize a history run timestamp without echoing unexpected shapes."""
    from common.admin_job_health import timestamp_seconds

    stamp = timestamp_seconds(value)
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def _last_sync_run(sync_result, history):
    """Attach the newest recorded synchronization run to the sync contract.

    The status file says whether a run is in flight; only the history file says
    when the last completed run happened and how it ended. A missing history
    file is reported as no history, an unreadable one as unavailable.
    """
    available = isinstance(history, dict) and not history.get("error")
    sync_result["history_available"] = available
    sync_result["last_run"] = None
    if not available:
        return
    runs = history.get("runs")
    run = runs[0] if isinstance(runs, list) and runs and isinstance(runs[0], dict) else None
    if run is None:
        return

    last_run = {
        "started_at": _iso_run_timestamp(run.get("started_at")),
        "ended_at": _iso_run_timestamp(run.get("ended_at") or run.get("started_at")),
    }
    success = run.get("success")
    if type(success) is bool:
        last_run["success"] = success
    interrupted = run.get("interrupted")
    if type(interrupted) is bool:
        last_run["interrupted"] = interrupted
    sync_result["last_run"] = last_run


async def _jellyfin_probe():
    """Make a bounded, unauthenticated reachability probe without returning server details."""
    import os

    base_url = os.getenv("JELLYFIN_URL", "").strip().rstrip("/")
    if not base_url:
        return {"status": "not_configured", "checked_at": None}
    try:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            return {"status": "unknown", "checked_at": None}
    except Exception as _exc:
        logger.debug("Ignored exception in %s", "_jellyfin_probe", exc_info=True)
        return {"status": "unknown", "checked_at": None}
    try:
        # Jellyfin's public system-info route responds without an API key. Ignore
        # the body entirely: it includes public instance details not needed here.
        async with httpx.AsyncClient(timeout=3.0, follow_redirects=False) as client:
            response = await client.get(f"{base_url}/System/Info/Public")
        status = "reachable" if response.status_code == 200 else "unavailable"
    except Exception:
        status = "unavailable"
    return {"status": status, "checked_at": datetime.now(timezone.utc).isoformat()}


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/capabilities")
    async def admin_v2_capabilities(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        return {"ok": True, **admin._capabilities()}

    @router.get("/api/v2/status/sync")
    async def admin_v2_sync_status(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        sync, stale_after = await asyncio.gather(
            asyncio.to_thread(admin._sync_status),
            asyncio.to_thread(_sync_stale_after_seconds, admin.CONFIG_PATH, admin.HUB_DB_URL),
        )
        return {"ok": True, "sync": _sanitized_sync_status(sync, stale_after)}

    @router.get("/api/v2/status/jellyfin")
    async def admin_v2_jellyfin_status(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        return {"ok": True, **(await _jellyfin_probe())}

    @router.get("/api/v2/status")
    async def admin_v2_status(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        db, sync, sync_stale_after, sync_history = await asyncio.gather(
            asyncio.to_thread(admin._db_stats),
            asyncio.to_thread(admin._sync_status),
            asyncio.to_thread(_sync_stale_after_seconds, admin.CONFIG_PATH, admin.HUB_DB_URL),
            asyncio.to_thread(_sync_history_for_status, admin),
        )
        runtime = {}
        app = request.scope.get("app")
        extra_fn = getattr(getattr(app, "state", None), "admin_extra", None)
        if callable(extra_fn):
            try:
                result = await admin.asyncio.to_thread(extra_fn)
                if isinstance(result, dict):
                    status = result.get("status")
                    if status in {"ok", "degraded", "error"}:
                        runtime["status"] = status
                    version = result.get("version")
                    if isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9._+-]{1,80}", version):
                        runtime["version"] = version
                    cache_entries = result.get("cache_entries")
                    if type(cache_entries) is int and cache_entries >= 0:
                        runtime["cache_entries"] = cache_entries
                    providers = result.get("providers")
                    if isinstance(providers, list):
                        runtime["providers"] = sorted({
                            provider for provider in providers
                            if isinstance(provider, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", provider)
                        })[:20]
            except Exception as _exc:
                logger.debug("Ignored exception in %s", "admin_v2_status", exc_info=True)
        # Keep the dashboard contract explicit: raw DB paths, exception text,
        # and arbitrary status-file keys are never returned to the browser.
        attention = {}
        try:
            raw_attention = admin._attention_summary()
            if isinstance(raw_attention, dict):
                allowed = (
                    "pending_tasks", "failed_tasks", "failed_tasks_available", "unavailable_videos",
                    "unavailable_available", "sync_backlog_available", "sync_backlog_queued",
                    "sync_backlog_processing", "sync_backlog_oldest_age_seconds",
                    "last_sync_failed_sources", "last_sync_rate_limited",
                )
                attention = {key: raw_attention[key] for key in allowed if key in raw_attention}
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "admin_v2_status", exc_info=True)
        db_result = {}
        if isinstance(db, dict):
            if db.get("database_backend") == "PostgreSQL":
                db_result["database_backend"] = "PostgreSQL"
            for key in ("videos_total", "artwork_total", "oshash_total", "phash_total", "files_total", "telegram_total", "fresh_24h", "photos_total",
                        "bios_total", "aliases_total", "dead_streams_total", "unplayable_total"):
                value = db.get(key)
                if type(value) is int and value >= 0:
                    db_result[key] = value
            size = db.get("db_size_mb")
            if type(size) in {int, float}:
                if math.isfinite(size) and size >= 0:
                    db_result["db_size_mb"] = size
            elif isinstance(size, Decimal) and size.is_finite() and size >= 0:
                # PostgreSQL NUMERIC values are decoded by psycopg as Decimal;
                # convert the allowlisted metric before JSON serialization.
                db_result["db_size_mb"] = float(size)
            db_result["db_ok"] = db.get("db_ok") is True
            by_provider = db.get("by_provider")
            if isinstance(by_provider, dict):
                grouped = {}
                for key, value in by_provider.items():
                    if not isinstance(key, str) or type(value) is not int or value < 0:
                        continue
                    provider = "telegram" if key.startswith("tg:[") else key[:40]
                    if not provider.replace("_", "").replace("-", "").isalnum():
                        continue
                    grouped[provider] = grouped.get(provider, 0) + value
                db_result["by_provider"] = grouped
            channel_map = db.get("telegram_channels")
            if isinstance(channel_map, dict):
                # Channel IDs are not needed on Home; preserve only the count.
                db_result["telegram_channels"] = {str(index): True for index, _ in enumerate(channel_map)}
            for key in ("last_seen", "last_seen_human"):
                value = db.get(key)
                if isinstance(value, (str, int, float)) and len(str(value)) <= 64:
                    db_result[key] = value
        sync_result = _sanitized_sync_status(sync, sync_stale_after)
        _last_sync_run(sync_result, sync_history)
        return {
            "ok": True,
            "time_human": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "db": db_result,
            "sync": sync_result,
            "attention": attention,
            "runtime": runtime,
            "capabilities": admin._capabilities(),
        }

    return router
