"""Basic service probes for Admin v2."""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
import logging
from fastapi import APIRouter, Request
from resolver.admin_v2_errors import V2ErrorRoute

logger = logging.getLogger(__name__)


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
        logger.debug("Ignored exception in _http_probe", exc_info=True)
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
        logger.debug("Ignored exception in _configured_jellyfin_probe", exc_info=True)
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
            logger.debug("Ignored exception in service_probes", exc_info=True)
            return {"ok": True, "checked_at": datetime.now(timezone.utc).isoformat(),
                    "cache_age_seconds": 0,
                    "services": [
                        _safe_probe_row("postgresql", "PostgreSQL", "unknown"),
                        _safe_probe_row("resolver", "Resolver", "unknown"),
                        _safe_probe_row("sync_worker", "Sync worker", "unknown"),
                        _safe_probe_row("phash_worker", "pHash worker", "unknown"),
                        _safe_probe_row("jellyfin", "Jellyfin", "unknown"),
                    ]}

    return router
