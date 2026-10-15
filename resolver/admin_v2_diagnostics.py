"""Playback diagnostics views and opt-in controls for Admin v2."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from fastapi import APIRouter, Query, Request
from admin_v2_errors import V2ErrorRoute


_PROBE_CACHE_SECONDS = 10.0
_PROBE_CACHE = {"checked_monotonic": 0.0, "result": None}
_PROBE_LOCK = asyncio.Lock()
_PROBE_TIMEOUT_SECONDS = 2.0


def _safe_probe_row(service: str, label: str, status: str, *, elapsed_ms=None,
                    http_status=None) -> dict:
    """Build the fixed public shape used by the service probe diagnostics."""
    row = {"service": service, "label": label, "status": status}
    if type(elapsed_ms) is int and 0 <= elapsed_ms <= 60_000:
        row["elapsed_ms"] = elapsed_ms
    if type(http_status) is int and 100 <= http_status <= 599:
        row["http_status"] = http_status
    return row


async def _http_probe(url: str, *, service: str, label: str) -> dict:
    """Probe one preselected endpoint; never return its URL, headers, or body."""
    import httpx

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
                timeout=_PROBE_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", url) as response:
                code = response.status_code
        status = "healthy" if 200 <= code < 300 else "unavailable"
        return _safe_probe_row(
            service, label, status,
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
            http_status=code,
        )
    except Exception:
        return _safe_probe_row(
            service, label, "unavailable",
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        )


async def _database_probe(admin) -> dict:
    started = time.monotonic()

    def check():
        from common import db

        conn = db.connect(admin.HUB_DB_URL, connect_timeout=2)
        try:
            # Bound a stalled server-side query as well as connection establishment.
            conn.execute("SET statement_timeout = '1500ms'")
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()

    try:
        await asyncio.to_thread(check)
        status = "healthy"
    except Exception:
        status = "unavailable"
    return _safe_probe_row(
        "postgresql", "PostgreSQL", status,
        elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
    )


def _worker_probe(service: str, label: str, path: str) -> dict:
    from common.worker_capabilities import read_worker_capabilities

    snapshot = read_worker_capabilities(path)
    return _safe_probe_row(service, label,
                           "healthy" if snapshot.get("available") is True else "unavailable")


async def _run_service_probes(admin) -> dict:
    """Probe only fixed local/configured services with secret-free results."""
    resolver, jellyfin = await asyncio.gather(
        _http_probe("http://127.0.0.1:8090/admin/health/live",
                    service="resolver", label="Resolver"),
        _configured_jellyfin_probe(),
    )
    database, workers = await asyncio.gather(
        _database_probe(admin), asyncio.to_thread(_worker_probe_set),
    )
    return {"services": [database, resolver, workers[0], workers[1], jellyfin]}


async def _configured_jellyfin_probe() -> dict:
    base_url = os.getenv("JELLYFIN_URL", "").strip().rstrip("/")
    if not base_url:
        return _safe_probe_row("jellyfin", "Jellyfin", "not_configured")
    try:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            return _safe_probe_row("jellyfin", "Jellyfin", "unknown")
    except Exception:
        return _safe_probe_row("jellyfin", "Jellyfin", "unknown")
    return await _http_probe(
        f"{base_url}/System/Info/Public", service="jellyfin", label="Jellyfin",
    )


def _worker_probe_set() -> list[dict]:
    return [
        _worker_probe("sync_worker", "Sync worker", str(os.getenv(
            "ADMIN_WORKER_CAPABILITIES_PATH", "/data/cache/worker-capabilities.json"))),
        _worker_probe("phash_worker", "pHash worker", str(os.getenv(
            "PHASH_WORKER_CAPABILITIES_PATH", "/data/cache/phash-worker-capabilities.json"))),
    ]


async def _cached_service_probes(admin) -> dict:
    """Coalesce concurrent callers and cap external probes to one set per 10s."""
    async with _PROBE_LOCK:
        now = time.monotonic()
        checked_at = _PROBE_CACHE["checked_monotonic"]
        if (_PROBE_CACHE["result"] is not None and now >= checked_at
                and now - checked_at < _PROBE_CACHE_SECONDS):
            age = max(0, int(now - checked_at))
            return {**_PROBE_CACHE["result"], "cache_age_seconds": age}
        try:
            result = await _run_service_probes(admin)
        except Exception:
            # Cache failures too, so a broken dependency cannot turn this
            # endpoint into an unbounded probe loop.
            result = {"services": [
                _safe_probe_row("postgresql", "PostgreSQL", "unknown"),
                _safe_probe_row("resolver", "Resolver", "unknown"),
                _safe_probe_row("sync_worker", "Sync worker", "unknown"),
                _safe_probe_row("phash_worker", "pHash worker", "unknown"),
                _safe_probe_row("jellyfin", "Jellyfin", "unknown"),
            ]}
        result["checked_at"] = datetime.now(timezone.utc).isoformat()
        _PROBE_CACHE.update(checked_monotonic=time.monotonic(), result=result)
        return {**result, "cache_age_seconds": 0}


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/diagnostics/probes")
    async def service_probes(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            return {"ok": True, **(await _cached_service_probes(admin))}
        except Exception:
            return {"ok": True, "checked_at": datetime.now(timezone.utc).isoformat(),
                    "cache_age_seconds": 0,
                    "services": [
                        _safe_probe_row("postgresql", "PostgreSQL", "unknown"),
                        _safe_probe_row("resolver", "Resolver", "unknown"),
                        _safe_probe_row("sync_worker", "Sync worker", "unknown"),
                        _safe_probe_row("phash_worker", "pHash worker", "unknown"),
                        _safe_probe_row("jellyfin", "Jellyfin", "unknown"),
                    ]}

    @router.get("/api/v2/diagnostics/playback/export")
    async def export_reports(request: Request, days: int = Query(7, ge=1, le=30),
                             limit: int = Query(100, ge=1, le=100)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            values = await admin.asyncio.to_thread(
                admin._playback_diagnostics().list_reports, limit=limit
            )
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
            fields = {
                "report_id", "started_at", "started_epoch", "ended_at", "provider", "method",
                "range", "status_code", "outcome", "error_type", "elapsed_ms", "file_size_ms",
                "upstream_first_byte_ms", "downstream_first_byte_ms", "upstream_bytes",
                "downstream_bytes", "upstream_max_gap_ms", "downstream_max_gap_ms",
                "upstream_idle_ms", "downstream_idle_ms", "phase", "phase_idle_ms",
            }
            reports = []
            for value in values:
                if not isinstance(value, dict):
                    continue
                try:
                    started = float(value.get("started_epoch"))
                except (TypeError, ValueError, OverflowError):
                    continue
                if started < cutoff:
                    continue
                reports.append({key: admin._safe_diagnostic_value({key: value[key]}).get(key)
                                for key in fields if key in value})
            payload = {
                "ok": True,
                "scope": {"days": days, "limit": limit, "generated_at": datetime.now(timezone.utc).isoformat()},
                "truncated": len(values) >= limit,
                "reports": reports,
            }
            filename = f"adult-hub-playback-diagnostics-{datetime.now(timezone.utc):%Y%m%d}.json"
            return admin.JSONResponse(
                payload,
                headers={"Cache-Control": "no-store", "Content-Disposition": f'attachment; filename="{filename}"'},
            )
        except Exception:
            return admin._diagnostic_failure()

    @router.get("/api/v2/diagnostics/playback/summary")
    async def summary(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            return {"ok": True, **admin._safe_diagnostic_value(await admin.asyncio.to_thread(admin._playback_summary))}
        except Exception:
            return admin._diagnostic_failure()

    @router.get("/api/v2/diagnostics/playback/status")
    async def status(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            value = await admin.asyncio.to_thread(admin._playback_diagnostics().status)
            return {"ok": True, **admin._safe_diagnostic_value(value)}
        except Exception:
            return admin._diagnostic_failure()

    @router.get("/api/v2/diagnostics/playback/reports")
    async def reports(request: Request, limit: int = Query(50, ge=1, le=100)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            values = await admin.asyncio.to_thread(admin._playback_diagnostics().list_reports, limit=limit)
            return {"ok": True, "items": [admin._safe_diagnostic_value(item) for item in values], "limit": limit}
        except Exception:
            return admin._diagnostic_failure()

    @router.get("/api/v2/diagnostics/playback/reports/{report_id}")
    async def report(request: Request, report_id: str):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            value = await admin.asyncio.to_thread(admin._playback_diagnostics().get_report, report_id)
            if value is None:
                return admin.JSONResponse({"ok": False, "error": "Playback report was not found."}, status_code=404)
            return {"ok": True, "report": admin._safe_diagnostic_value(value)}
        except Exception:
            return admin._diagnostic_failure()

    @router.post("/api/v2/diagnostics/playback/enable")
    async def enable(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            result = await admin.asyncio.to_thread(admin._playback_diagnostics().set_enabled, hours=24)
            return {"ok": True, **admin._safe_diagnostic_value(result)}
        except Exception:
            return admin._diagnostic_failure()

    @router.post("/api/v2/diagnostics/playback/disable")
    async def disable(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            result = await admin.asyncio.to_thread(admin._playback_diagnostics().disable)
            return {"ok": True, **admin._safe_diagnostic_value(result)}
        except Exception:
            return admin._diagnostic_failure()

    @router.delete("/api/v2/diagnostics/playback/reports")
    async def clear_reports(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            cleared = await admin.asyncio.to_thread(
                admin._playback_diagnostics().clear_reports
            )
            return {"ok": True, "cleared": max(0, int(cleared or 0))}
        except Exception:
            return admin._diagnostic_failure()

    return router
