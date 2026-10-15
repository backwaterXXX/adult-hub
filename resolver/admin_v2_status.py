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


def _sync_stale_after_seconds(config_path):
    """Allow two configured sync intervals before calling the status stale."""
    try:
        from common.config_loader import load_config

        config = load_config(str(config_path))
        schedule = config.sync
        interval = 24 * 60 * 60 if schedule.daily_at else int(schedule.interval_minutes) * 60
        return max(300, interval * 2) if interval > 0 else None
    except Exception:
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
    except Exception:
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
    router = APIRouter()

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
            asyncio.to_thread(_sync_stale_after_seconds, admin.CONFIG_PATH),
        )
        return {"ok": True, "sync": _sanitized_sync_status(sync, stale_after)}

    @router.get("/api/v2/status/jellyfin")
    async def admin_v2_jellyfin_status(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        return {"ok": True, **(await _jellyfin_probe())}

    @router.get("/api/v2/linkcheck")
    async def linkcheck_report(request: Request):
        """Expose outcome totals only; linkcheck lines may contain URLs or IDs."""
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            report = await admin.asyncio.to_thread(admin._linkcheck_tail, 1000)
        except Exception:
            return {
                "ok": True, "available": False, "exists": False, "checked_at": None,
                "providers": [], "empty": False,
                "instruction": "docker compose exec sync python linkcheck.py",
                "scoped_instruction": "docker compose exec sync python linkcheck.py --sample 5 --provider telegram",
            }
        report_readable = isinstance(report, dict) and not report.get("error")
        if not isinstance(report, dict):
            report = {}
        rows = {}
        samples = []
        latest_at = None
        prefix = r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[([A-Za-z0-9_.-]{1,40})\] "
        summary_pattern = re.compile(
            prefix + r"(?:(\d{1,6})/(\d{1,6}) ok -> (HEALTHY|BROKEN)|no videos in DB, skipping)$"
        )
        sample_pattern = re.compile(prefix + r"(OK|FAIL)\s+[^\s]+: (.*)$")

        def failure_label(detail):
            if detail.startswith("resolver error:"):
                return "Resolver request failed"
            if detail.startswith("playlist fetch error:"):
                return "Playlist request failed"
            status = re.match(r"(?:resolve|playlist) HTTP (\d{3})\b", detail)
            if status:
                return f"HTTP {status.group(1)}"
            return "Stream check failed"

        for line in report.get("tail", []) if isinstance(report.get("tail"), list) else []:
            if not isinstance(line, str):
                continue
            line = line.strip()
            sample = sample_pattern.fullmatch(line)
            if sample:
                timestamp, provider, outcome, detail = sample.groups()
                latest_at = max(filter(None, (latest_at, timestamp)))
                row = rows.setdefault(provider, {"provider": provider, "ok": 0, "total": 0,
                                                  "status": "unknown", "failure_reasons": {}})
                row["total"] += 1
                if outcome == "OK":
                    row["ok"] += 1
                    samples.append({
                        "timestamp": timestamp, "provider": provider, "result": "passed",
                    })
                else:
                    label = failure_label(detail)
                    row["failure_reasons"][label] = row["failure_reasons"].get(label, 0) + 1
                    samples.append({
                        "timestamp": timestamp, "provider": provider,
                        "result": "failed", "failure_reason": label,
                    })
                continue
            summary = summary_pattern.fullmatch(line)
            if not summary:
                continue
            timestamp, provider, ok_count, total_count, result = summary.groups()
            latest_at = max(filter(None, (latest_at, timestamp)))
            row = rows.setdefault(provider, {"provider": provider, "ok": 0, "total": 0,
                                              "status": "unknown", "failure_reasons": {}})
            if result:
                row.update(ok=int(ok_count), total=int(total_count), status=result.lower())
            else:
                row.update(ok=0, total=0, status="no_samples")
        providers = sorted(rows.values(), key=lambda row: row["provider"])
        exists = report_readable and report.get("exists") is True
        sample_total = len(samples)
        return {
            "ok": True,
            # `available` means the report source could be read; `exists` says
            # whether a report has ever been recorded. Keep absence distinct
            # from a read failure so the UI never claims "no report" on error.
            "available": report_readable,
            "exists": exists,
            "checked_at": latest_at,
            "providers": providers,
            # Keep useful per-check diagnostics while excluding the media
            # reference and raw detail, which may contain IDs, URLs or tokens.
            "samples": samples[-200:],
            "samples_total": sample_total,
            "samples_truncated": sample_total > 200,
            "empty": exists and not providers,
            "instruction": "docker compose exec sync python linkcheck.py",
            "scoped_instruction": "docker compose exec sync python linkcheck.py --sample 5 --provider telegram",
        }

    @router.get("/api/v2/sync/activity")
    async def sync_activity(request: Request):
        """Return a redacted view of sync progress without raw log messages."""
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        try:
            report = await admin.asyncio.to_thread(admin._sync_activity_tail, 80)
        except Exception:
            return {"ok": True, "available": False, "exists": False, "updated_at": None,
                    "entries": [], "omitted_entries": 0}
        if not isinstance(report, dict):
            report = {}
        report_available = not report.get("error")
        line_pattern = re.compile(
            r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,\d{3})? "
            r"\[(INFO|WARNING|ERROR|CRITICAL)\] (.*)$"
        )

        def summarize(message):
            if message.startswith("Starting Adult-Hub sync"):
                return "Sync started."
            if message.startswith("Sync completed successfully"):
                return "Sync completed successfully."
            if message.startswith("Sync completed with source failures"):
                return "Sync completed with source failures."
            if message.startswith("Syncing Telegram ("):
                match = re.search(r"\((\d+) (?:enabled )?channel(?:s)?", message)
                return f"Syncing Telegram ({match.group(1)} channels)." if match else "Syncing Telegram sources."
            if message.startswith("Safe checkpoint processed ") or message.startswith("Jobs worker completed "):
                match = re.search(r"(\d+) queued decision", message)
                return f"Processed {match.group(1)} queued decision(s)." if match else "Queue checkpoint completed."
            if message.startswith("Full sync is active; queue will be processed"):
                return "Queued work deferred to the next safe sync checkpoint."
            if message.startswith("Telegram sync failed for "):
                return "A Telegram source failed to sync."
            if message.startswith("Sync completed with source failures"):
                return "Sync completed with source failures."
            if message.startswith("Performer catalogue refresh failed; using last local copy:"):
                return "Performer catalogue refresh failed; the last local copy remains in use."
            catalogue = re.match(r"Performer catalogue refreshed: (\d+) people, (\d+) aliases$", message)
            if catalogue:
                return f"Performer catalogue refreshed: {catalogue.group(1)} people and {catalogue.group(2)} aliases."
            saved = re.match(r"Saved (\d+)/(\d+) videos.*; there were (\d+) write/commit errors\.$", message)
            if saved:
                return (f"Saved {saved.group(1)} of {saved.group(2)} catalog items with "
                        f"{saved.group(3)} write errors.")
            saved = re.match(r"Successfully saved (\d+)/(\d+) videos", message)
            if saved:
                return f"Saved {saved.group(1)} of {saved.group(2)} catalog items successfully."
            availability = re.match(r"Dead-stream probe: (\d+) videos checked, (\d+) to purge$", message)
            if availability:
                return (f"Availability probe checked {availability.group(1)} items; "
                        f"{availability.group(2)} are eligible for cleanup.")
            purge_limit = re.match(r"Dead-stream purge capped: (\d+) candidates, purging (\d+) this run$", message)
            if purge_limit:
                return (f"Cleanup was capped at {purge_limit.group(2)} of "
                        f"{purge_limit.group(1)} eligible items for this run.")
            retention = re.match(r"Retention policy: purging (\d+) videos older than (\d+) days", message)
            if retention:
                return (f"Retention cleanup is removing {retention.group(1)} catalog items "
                        f"older than {retention.group(2)} days.")
            retention = re.match(r"Retention check: all active videos seen within (\d+) days", message)
            if retention:
                return f"Retention check passed for the {retention.group(1)}-day window."
            purge = re.match(r"Purged (\d+) videos \(.*\)$", message)
            if purge:
                return f"Cleanup removed {purge.group(1)} catalog items."
            rewrite = re.match(r"Stream rewrite: (\d+)/(\d+) files updated to current mode$", message)
            if rewrite:
                return f"Stream format updated for {rewrite.group(1)} of {rewrite.group(2)} files."
            jellyfin = re.match(r"Jellyfin library refresh triggered: HTTP (\d{3})\b", message)
            if jellyfin:
                return f"Jellyfin refresh request returned HTTP {jellyfin.group(1)}."
            return None

        entries = []
        omitted = 0
        for line in report.get("tail", []) if isinstance(report.get("tail"), list) else []:
            if not isinstance(line, str):
                continue
            match = line_pattern.fullmatch(line.strip())
            if not match:
                omitted += 1
                continue
            timestamp, level, message = match.groups()
            summary = summarize(message)
            if summary is None:
                omitted += 1
                continue
            entries.append({"timestamp": timestamp, "level": level.lower(), "message": summary})
        updated_at = report.get("mtime_human")
        if not isinstance(updated_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", updated_at):
            updated_at = None
        return {
            "ok": True,
            "available": report_available,
            "exists": report_available and report.get("exists") is True,
            "updated_at": updated_at,
            "entries": entries,
            "omitted_entries": omitted,
        }

    @router.get("/api/v2/status")
    async def admin_v2_status(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"error": "Admin is only available locally."}, status_code=403)
        db, sync, sync_stale_after = await asyncio.gather(
            asyncio.to_thread(admin._db_stats),
            asyncio.to_thread(admin._sync_status),
            asyncio.to_thread(_sync_stale_after_seconds, admin.CONFIG_PATH),
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
            except Exception:
                pass
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
        except Exception:
            pass
        db_result = {}
        if isinstance(db, dict):
            if db.get("database_backend") == "PostgreSQL":
                db_result["database_backend"] = "PostgreSQL"
            for key in ("videos_total", "files_total", "telegram_total", "fresh_24h", "photos_total",
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
