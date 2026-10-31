import os
import time
import asyncio
import inspect
from common import db
from common.job_queue import ALL_ACTIONS
from common.logging_setup import configure_logging
import ipaddress
import socket
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, urljoin
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import RedirectResponse, StreamingResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx

from providers import (
    get_provider,
    list_providers,
    ensure_config_providers_registered,
)
from providers._http import get_shared_client, aclose_shared_client
import admin as admin_module
from admin import router as admin_router
from admin_v2_errors import configure_activity_database, v2_error_response
from playback_diagnostics import diagnostics
from common.app_version import __version__ as APP_VERSION
from metrics import resolver_metrics
from catalog_api import (
    router as catalog_api_router,
)
from proxy_utils import looks_like_playlist, _parse_range_request, rewrite_m3u8_playlist  # noqa: F401
from telegram_proxy import TelegramProxyServices, proxy_telegram_response as _proxy_telegram_response_impl
from stream_cache import (
    PersistentLRUCache,
    cache_ttl_for_provider,
)

logger = configure_logging("resolver")
class AdminStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store"
        return response


app = FastAPI(title="Adult-Hub Resolver", version=APP_VERSION)
WORKER_METRIC_ACTIONS = frozenset(ALL_ACTIONS)
WORKER_METRIC_STATES = frozenset({"queued", "processing", "done", "failed", "cancelled"})
configure_activity_database(lambda: admin_module.HUB_DB_URL)


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(request: Request, exc: RequestValidationError):
    # FastAPI raises parameter validation errors outside APIRoute's handler wrapper.
    # Keep v1's standard response while presenting the documented v2 contract.
    if request.url.path.startswith("/admin/api/v2/"):
        return v2_error_response(400, "Invalid request parameters.")
    return await request_validation_exception_handler(request, exc)


@app.middleware("http")
async def prevent_admin_v2_api_caching(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/admin/api/v2/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def observe_http_requests(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        route = request.scope.get("route")
        route_name = getattr(route, "path", "unmatched")
        resolver_metrics.record_request(request.method, route_name, 500,
                                        time.perf_counter() - started)
        raise

    route = request.scope.get("route")
    route_name = getattr(route, "path", "unmatched")
    stream = "/play/" in route_name and route_name.endswith("/stream")
    body_iterator = response.body_iterator

    async def observed_body():
        try:
            async for chunk in body_iterator:
                yield chunk
        finally:
            resolver_metrics.record_request(
                request.method, route_name, response.status_code,
                time.perf_counter() - started, stream=stream,
            )

    response.body_iterator = observed_body()
    return response


app.include_router(admin_router)
app.mount(
    "/admin/assets-v2",
    AdminStaticFiles(directory=Path(__file__).parent / "static" / "admin-v2", check_dir=False),
    name="admin-assets-v2",
)

# Load configuration and initialize provider addresses (Telegram-only).
# Telegram is always registered by the providers package. A config that
# enables a provider which is not registered fails fast here.
app_config = None
try:
    from common.config_loader import load_config, ConfigError
    app_config = load_config()
    for prov_name, src in app_config.sources.items():
        if src.address:
            try:
                p = get_provider(prov_name)
                p.set_address(src.address)
                logger.info(f"Initialized provider '{prov_name}' with address {src.address}")
            except KeyError:
                pass
    ensure_config_providers_registered(app_config)
except ConfigError as ce:
    logger.critical(f"FATAL CONFIG ERROR: {ce}")
    raise SystemExit(f"FATAL CONFIG ERROR: {ce}")
except (RuntimeError, ValueError) as ext_e:
    logger.critical(f"FATAL PROVIDER ERROR: {ext_e}")
    raise
except SystemExit:
    raise
except Exception as e:
    logger.critical(f"FATAL CONFIG ERROR: configuration loader failed unexpectedly: {e}")
    raise SystemExit(1)

# Wszystkie endpointy resolvera mieszkaja pod /admin (panel, api, play, px).
# Caddy kieruje /admin* wprost tutaj (bez stripowania), a bezposredni
# dostep :8090 dziala tak samo. Stare sciezki (/health, /api/*, /play/*, /px)
# odpowiadaja 307 -> /admin/* (kompatybilnosc starych .strm i klientow).
api = APIRouter(prefix="/admin")
api.include_router(catalog_api_router)


def _process_health() -> dict:
    """Liveness state built from process memory only; performs no I/O."""
    return {
        "status": "ok",
        "version": APP_VERSION,
        "providers": list(list_providers().keys()),
    }


def _provider_health() -> dict:
    """Provider health including the persisted stream-cache size.

    ``len(cache)`` opens a PostgreSQL connection when the persistent cache is
    enabled, so event-loop callers run this through ``asyncio.to_thread``.
    """
    return {**_process_health(), "cache_entries": len(cache)}


def _admin_extra():
    return _provider_health()


def _admin_cache_clear() -> int:
    """Clear the resolver cache and return its unique live entry count."""
    return cache.clear()


app.state.admin_extra = _admin_extra
app.state.admin_cache_clear = _admin_cache_clear


@app.on_event("shutdown")
async def _on_shutdown():
    try:
        await aclose_shared_client()
    except Exception:
        pass
    # Bounded parallel Telegram streaming (v1, opt-in): let the provider
    # close its own resources (including the shared stream manager, if one
    # was created). provider.close() creates nothing; never fail shutdown.
    try:
        tg_prov = get_provider("telegram")
        _close = getattr(tg_prov, "close", None)
        if callable(_close):
            try:
                _res = _close()
                if inspect.isawaitable(_res):
                    await asyncio.wait_for(_res, timeout=5.0)
            except Exception:
                pass
    except Exception:
        pass
    try:
        cache.close()
    except Exception:
        pass

# Concurrency limit for upstream stream resolutions (#28)
RESOLVER_CONCURRENCY_LIMIT = int(os.getenv("RESOLVER_CONCURRENCY_LIMIT", "10"))
_stream_semaphore = asyncio.Semaphore(RESOLVER_CONCURRENCY_LIMIT)

cache = PersistentLRUCache()

def parse_composite_id(video_id: str) -> Tuple[str, str]:
    """Split a ``provider:id`` composite on the FIRST colon.

    - Prefix registered -> ``(provider, rest)`` (rest keeps any further
      colons, e.g. ``telegram:ch:42:uniq`` -> ``("telegram", "ch:42:uniq")``).
    - No prefix -> Telegram-only ``("telegram", video_id)``.
    - Unknown prefix -> ``ValueError`` (play endpoint maps it to HTTP 400).

    Empty input maps to ``("telegram", "")`` and fails downstream.
    """
    if not video_id:
        return "telegram", ""
    text = video_id.strip()
    if ":" in text:
        prov, raw_id = text.split(":", 1)
        if prov in list_providers():
            return prov, raw_id
        raise ValueError(
            f"Unknown provider prefix '{prov}' in id {video_id!r}. "
            f"Available: {sorted(list_providers().keys())}"
        )
    return "telegram", text

def validate_proxy_url(url: str):
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Invalid stream URL")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise HTTPException(status_code=400, detail="Local stream URLs are not allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(addr.split("%", 1)[0]).is_global for addr in addresses):
            raise HTTPException(status_code=400, detail="Non-public stream addresses are not allowed")
    except HTTPException:
        raise
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="Could not validate stream host")


async def _validate_proxy_url_async(url: str):
    """Run blocking DNS validation outside the ASGI event loop."""
    await asyncio.to_thread(validate_proxy_url, url)


_REDIRECT_CODES = {301, 302, 303, 307, 308}


async def _safe_proxy_stream(client, url: str, *, headers: dict, timeout: "float | httpx.Timeout | None", method: str = "GET"):
    """Open a streaming request, checking every redirect before following it."""
    current = url
    for _ in range(6):
        await _validate_proxy_url_async(current)
        context = client.stream(method, current, headers=headers, timeout=timeout, follow_redirects=False)
        response = await context.__aenter__()
        if response.status_code not in _REDIRECT_CODES:
            return context, response
        location = response.headers.get("location")
        await context.__aexit__(None, None, None)
        if not location:
            raise HTTPException(status_code=502, detail="Invalid upstream redirect")
        current = urljoin(current, location)
    raise HTTPException(status_code=502, detail="Too many upstream redirects")

@api.get("/health")
async def health_check():
    return await asyncio.to_thread(_provider_health)


@api.get("/health/live")
async def health_liveness():
    """Process liveness; deliberately independent of the database."""
    return _process_health()


def _check_database_ready() -> None:
    conn = db.connect(admin_module.HUB_DB_URL)
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()


@api.get("/health/ready")
async def health_readiness():
    """Readiness for requests that depend on the shared catalog database."""
    try:
        await asyncio.to_thread(_check_database_ready)
    except Exception:
        return JSONResponse({"status": "not_ready", "database": "unavailable"}, status_code=503)
    return {**(await asyncio.to_thread(_provider_health)), "database": "ok"}


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    """Expose aggregate operational metrics without identifiers or credentials."""
    if admin_module._admin_blocked(request):
        return Response(status_code=404)
    lines = [resolver_metrics.render().rstrip()]
    sync_status = admin_module._sync_status()
    lines.extend([
        "# HELP adult_hub_sync_running Whether the sync runner is currently active.",
        "# TYPE adult_hub_sync_running gauge",
        f'adult_hub_sync_running {1 if sync_status.get("running") is True else 0}',
    ])
    if isinstance(sync_status.get("last_success"), bool):
        lines.extend([
            "# HELP adult_hub_sync_last_success Whether the most recent sync succeeded.",
            "# TYPE adult_hub_sync_last_success gauge",
            f'adult_hub_sync_last_success {1 if sync_status["last_success"] else 0}',
        ])
    if isinstance(sync_status.get("updated_at"), (int, float)):
        lines.extend([
            "# HELP adult_hub_sync_status_updated_timestamp_seconds Last sync status update time.",
            "# TYPE adult_hub_sync_status_updated_timestamp_seconds gauge",
            f'adult_hub_sync_status_updated_timestamp_seconds {float(sync_status["updated_at"]):.3f}',
        ])
    try:
        history = admin_module._sync_history(1).get("runs", [])
        if history and isinstance(history[0].get("duration_seconds"), (int, float)):
            lines.extend([
                "# HELP adult_hub_sync_last_duration_seconds Duration of the latest completed sync.",
                "# TYPE adult_hub_sync_last_duration_seconds gauge",
                f'adult_hub_sync_last_duration_seconds {float(history[0]["duration_seconds"]):.3f}',
            ])
    except Exception:
        pass
    try:
        def read_job_counts():
            conn = db.connect(admin_module.HUB_DB_URL)
            try:
                return conn.execute(
                    "SELECT action,state,count(*) FROM video_duplicate_jobs GROUP BY action,state"
                ).fetchall()
            finally:
                conn.close()
        job_rows = await asyncio.to_thread(read_job_counts)
        lines.extend([
            "# HELP adult_hub_jobs Number of durable jobs grouped by action and state.",
            "# TYPE adult_hub_jobs gauge",
        ])
        for action, state, count in job_rows:
            if str(action) in WORKER_METRIC_ACTIONS and str(state) in WORKER_METRIC_STATES:
                lines.append(
                    f'adult_hub_jobs{{action="{action}",state="{state}"}} {int(count)}'
                )
        lines.extend([
            "# HELP adult_hub_jobs_database_available Whether durable job counts were read successfully.",
            "# TYPE adult_hub_jobs_database_available gauge",
            "adult_hub_jobs_database_available 1",
        ])
    except Exception:
        lines.extend([
            "# HELP adult_hub_jobs_database_available Whether durable job counts were read successfully.",
            "# TYPE adult_hub_jobs_database_available gauge",
            "adult_hub_jobs_database_available 0",
        ])
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")










def _performer_bio_payload(wanted: str) -> dict:
    """Blocking performer lookup; the endpoint runs it off the event loop."""
    import json as _json

    conn = db.connect(os.getenv("DATABASE_URL") or None)
    try:
        tables = db.table_names(conn)
        canonical = wanted
        if "performer_aliases" in tables:
            alias_row = conn.execute(
                "SELECT canonical FROM performer_aliases WHERE lower(alias) = lower(%s)",
                (wanted,),
            ).fetchone()
            if alias_row:
                canonical = alias_row[0]
        cols = set(db.table_columns(conn, "performer_bios")) if "performer_bios" in tables else set()
        nat_col = "nationality" if "nationality" in cols else "''"
        row = (conn.execute(
            f"SELECT bio, birth, aliases, updated_at, {nat_col} FROM performer_bios "
            "WHERE lower(name) = lower(%s)", (canonical,)).fetchone()
            if "performer_bios" in tables else None)
        aliases = ([r[0] for r in conn.execute(
            "SELECT alias FROM performer_aliases WHERE lower(canonical) = lower(%s)",
            (canonical,)).fetchall()] if "performer_aliases" in tables else [])
        photo = (conn.execute(
            "SELECT thumb FROM performer_photos WHERE lower(name) = lower(%s)",
            (canonical,)).fetchone() if "performer_photos" in tables else None)
    finally:
        conn.close()
    if row is None and (photo is None or not photo[0]):
        raise HTTPException(status_code=404, detail=f"No info for performer '{wanted}'")
    bio, birth, aliases_raw, updated_at, nationality = row if row else (None, None, None, None, "")
    try:
        stored_aliases = _json.loads(aliases_raw) if aliases_raw else []
    except Exception:
        stored_aliases = []
    return {
        "name": canonical,
        "bio": bio or "",
        "birth": birth or "",
        "nationality": nationality or "",
        "aliases": sorted({*(stored_aliases if isinstance(stored_aliases, list) else []),
                           *aliases}),
        "photo_url": photo[0] if photo and photo[0] else "",
        "updated_at": updated_at or 0,
    }


@api.get("/api/performer-bio")
async def performer_bio(request: Request, name: str = ""):
    """Performer information from PBC, StashDB, and local photos.

    Populated by sync (backfill_pbc, backfill_tpdb, backfill_stashdb); resolver
    only reads (DATABASE_URL). Missing row -> 404. Profile data is admin-only,
    so a request blocked from the admin surface must not read it either.
    """
    if admin_module._admin_blocked(request):
        return JSONResponse({"error": "Admin is only available locally."}, status_code=403)
    wanted = (name or "").strip()
    if not wanted:
        raise HTTPException(status_code=404, detail="Missing performer name")
    try:
        return await asyncio.to_thread(_performer_bio_payload, wanted)
    except HTTPException:
        raise
    except Exception as exc:
        # Drivers can echo hosts, credentials or SQL in their messages.
        logger.warning(f"Performer bio lookup failed ({type(exc).__name__}).")
        raise HTTPException(status_code=502, detail="HUB DB lookup failed.")


def _parse_play_id_or_400(video_id: str) -> Tuple[str, str]:
    """parse_composite_id mapped to HTTP errors: unknown prefix -> 400."""
    try:
        return parse_composite_id(video_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _get_stream_candidate_ids(requested_id: str) -> list[str]:
    """Look up canonical video ID and all active failover sources from the database."""
    try:
        conn = db.connect(os.getenv("DATABASE_URL") or None)
        try:
            canonical_id = requested_id
            tables = db.table_names(conn)
            if "video_sources" in tables:
                alias_row = conn.execute(
                    "SELECT video_id FROM video_sources WHERE source_id=%s AND video_id!=%s AND is_active=1",
                    (requested_id, requested_id)).fetchone()
                if alias_row and alias_row[0]:
                    canonical_id = alias_row[0]
            if canonical_id == requested_id and "video_duplicate_decisions" in tables:
                dec_row = conn.execute(
                    "SELECT keeper_id FROM video_duplicate_decisions WHERE loser_id=%s AND state='active'",
                    (requested_id,)).fetchone()
                if dec_row and dec_row[0]:
                    canonical_id = dec_row[0]

            candidates = [canonical_id]
            if "video_sources" in tables:
                sources = conn.execute(
                    "SELECT source_id FROM video_sources WHERE video_id=%s AND is_active=1 ORDER BY priority ASC, id ASC",
                    (canonical_id,)).fetchall()
                for (s,) in sources:
                    if s and s not in candidates:
                        candidates.append(s)
            if requested_id not in candidates:
                candidates.append(requested_id)
            return candidates
        finally:
            conn.close()
    except Exception as exc:
        logger.debug(f"Source candidate lookup error for {requested_id}: {exc}")
        return [requested_id]


@api.get("/play/{video_id:path}/variants")
@api.get("/api/videos/{video_id:path}/variants")
async def get_video_variants(video_id: str):
    """Get available video variants/qualities.

    Returns the variant contract for Android and desktop clients.
    Distinguishes:
      - 'ready': alternatives available
      - 'original_only': recognition finished, only original available
      - 'processing': Telegram server transcoding in progress
    """
    candidates = await asyncio.to_thread(_get_stream_candidate_ids, video_id)
    last_error = None

    for cand_id in candidates:
        try:
            provider_name, raw_id = _parse_play_id_or_400(cand_id)
        except Exception:
            continue

        if provider_name == "telegram":
            channel_ref = (raw_id or "").split(":", 1)[0]
            try:
                tg_prov = get_provider("telegram")
            except KeyError:
                last_error = HTTPException(status_code=502, detail="Telegram provider not available")
                continue

            auth_check = getattr(tg_prov, "is_channel_authorized", None)
            if callable(auth_check) and not auth_check(channel_ref):
                last_error = HTTPException(status_code=403, detail="Telegram channel is not authorized")
                continue

            variants_fn = getattr(tg_prov, "get_video_variants", None)
            if not callable(variants_fn):
                continue

            try:
                res = await variants_fn(raw_id)
                res["video_id"] = cand_id
                return res
            except HTTPException as exc:
                last_error = exc
                continue
            except ValueError as exc:
                last_error = HTTPException(status_code=404, detail=str(exc))
                continue
            except Exception as exc:
                last_error = exc
                logger.warning(f"Failed to fetch variants for {cand_id}: {exc}")
                continue

    if last_error is not None:
        if isinstance(last_error, HTTPException):
            raise last_error
        logger.error(f"Failed to resolve variants for {video_id}: {last_error}")
        raise HTTPException(status_code=502, detail=f"Failed to resolve video variants: {last_error}")

    raise HTTPException(status_code=404, detail=f"No video found for {video_id}")


@api.api_route("/play/{video_id:path}/stream", methods=["GET", "HEAD"])
async def play_stream(
    request: Request,
    video_id: str,
    mode: str = Query("redirect", pattern="^(redirect|proxy)$"),
    variant: Optional[str] = Query(None),
    variant_id: Optional[str] = Query(None),
):
    """
    Resolve stream and redirect client (302) or proxy.
    video_id format: '<provider>:<id>' or just '<id>' (Telegram-only).
    Supports multi-source failover from video_sources.
    Supports variant selection for alternative video qualities.
    """
    chosen_variant = (variant or variant_id or "").strip() or None
    candidates = await asyncio.to_thread(_get_stream_candidate_ids, video_id)
    stream_url = None
    active_cache_key = None
    last_error = None

    for cand_id in candidates:
        try:
            provider_name, raw_id = _parse_play_id_or_400(cand_id)
        except Exception:
            continue
        cache_key = f"{provider_name}:{raw_id}"
        if chosen_variant and chosen_variant.lower() != "original":
            cache_key = f"{cache_key}:variant:{chosen_variant}"

        if provider_name == "telegram":
            channel_ref = (raw_id or "").split(":", 1)[0]
            try:
                tg_prov = get_provider("telegram")
                auth_check = getattr(tg_prov, "is_channel_authorized", None)
                if callable(auth_check) and not auth_check(channel_ref):
                    last_error = HTTPException(status_code=403, detail="Channel not authorized")
                    continue
            except KeyError:
                pass

        use_cache = (provider_name != "telegram")
        cand_stream = await asyncio.to_thread(cache.get, cache_key) if use_cache else None
        if cand_stream:
            if cand_stream.startswith("tg://"):
                pass
            else:
                alive = await upstream_url_alive(cand_stream)
                if alive is False:
                    logger.info(f"Cached stream for {cache_key} is dead, re-resolving")
                    await asyncio.to_thread(cache.invalidate, cache_key)
                    cand_stream = None

        if not cand_stream:
            try:
                async with _stream_semaphore:
                    if use_cache:
                        cand_stream = await asyncio.to_thread(cache.get, cache_key)
                    if not cand_stream:
                        prov = get_provider(provider_name)
                        if chosen_variant:
                            cand_stream = await prov.resolve_stream(raw_id, variant=chosen_variant)
                        else:
                            cand_stream = await prov.resolve_stream(raw_id)
                        if not cand_stream:
                            raise ValueError(f"Empty stream URL resolved for {cache_key}")
                        if cand_stream.startswith("tg://"):
                            logger.info(f"Resolved new telegram stream for {cache_key}")
                        else:
                            if await upstream_url_alive(cand_stream) is False:
                                raise ValueError(f"Freshly resolved URL is dead for {cache_key}")
                            if use_cache:
                                await asyncio.to_thread(cache.set, cache_key, cand_stream, ttl_seconds=cache_ttl_for_provider(provider_name))
                            logger.info(f"Resolved new stream for {cache_key}")
            except HTTPException as e:
                last_error = e
                logger.warning(f"Failed to resolve {cache_key}: {e}")
                continue
            except Exception as e:
                if chosen_variant and "not found" in str(e).lower():
                    last_error = HTTPException(status_code=404, detail=str(e))
                else:
                    last_error = e
                logger.warning(f"Failed to resolve {cache_key}: {e}")
                continue

        if cand_stream:
            stream_url = cand_stream
            active_cache_key = cache_key
            if cand_id != video_id:
                logger.info(f"Multi-source failover active for {video_id}: playing fallback {cand_id}")
            break

    if not stream_url:
        if isinstance(last_error, HTTPException):
            raise last_error
        logger.error(f"Failed to resolve stream for {video_id}: {last_error}")
        raise HTTPException(status_code=502, detail=f"Failed to resolve stream: {last_error or 'No working stream source'}")

    if (stream_url or "").startswith("tg://"):
        return await proxy_fetch_response(request, stream_url, active_cache_key)

    if mode == "redirect":
        return RedirectResponse(url=stream_url, status_code=302)

    await _validate_proxy_url_async(stream_url)
    return await proxy_fetch_response(request, stream_url, active_cache_key)


async def upstream_url_alive(url: str) -> Optional[bool]:
    """Check whether a cached upstream URL still serves content.

    Returns True (alive), False (dead: 401/403/404/410, or 502/504 = the
    upstream file is gone at the edge) or None (unknown: network
    error/timeout/other 5xx — caller should keep using the cached URL).
    """
    try:
        await _validate_proxy_url_async(url)
        client = await get_shared_client()
        parsed = urlparse(url)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Range": "bytes=0-0",
        }
        if parsed.netloc:
            # Neutral: referer derives from the checked URL itself, no
            # hardcoded provider domains.
            headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
        async with client.stream("GET", url, headers=headers, timeout=5.0, follow_redirects=False) as resp:
            if resp.status_code in (200, 206, 301, 302, 307, 308):
                return True
            if resp.status_code in (401, 403, 404, 410, 502, 504):
                return False
            return None
    except Exception as e:
        logger.debug(f"Stream liveness check failed (treating as unknown): {e}")
        return None








def _record_playback_file_size(ref: str, size_bytes: int) -> None:
    """Best-effort fill of videos.file_size_bytes learned while proxying playback."""
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    unique_id = str(ref).rsplit(":", 1)[-1].strip()
    if not db_url or not unique_id:
        return
    try:
        conn = db.connect(db_url)
        try:
            with conn:
                conn.execute(
                    "UPDATE videos SET file_size_bytes=%s "
                    "WHERE id=%s AND file_size_bytes IS NULL",
                    (int(size_bytes), f"telegram:{unique_id}"),
                )
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Playback file-size fill skipped for %s: %s", ref, exc)


async def _proxy_telegram_response(request: Request, url: str, cache_key: Optional[str] = None):
    """Compatibility facade for the Telegram HTTP range proxy."""
    is_variant = bool(cache_key and ":variant:" in cache_key)
    services = TelegramProxyServices(
        diagnostics=diagnostics,
        get_provider=get_provider,
        parse_range_request=_parse_range_request,
        asyncio=asyncio,
        inspect=inspect,
        time=time,
        record_file_size=(None if is_variant else _record_playback_file_size),
    )
    return await _proxy_telegram_response_impl(request, url, cache_key, services=services)

async def proxy_fetch_response(request: Request, url: str, cache_key: Optional[str] = None):
    """Fetch a supported upstream stream, or a Telegram range stream."""
    if (url or "").startswith("tg://"):
        return await _proxy_telegram_response(request, url, cache_key)
    await _validate_proxy_url_async(url)
    parsed = urlparse(url)
    if looks_like_playlist(url):
        raise HTTPException(status_code=502, detail="HLS proxying is not supported")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if parsed.netloc:
        headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
    client = await get_shared_client()

    client_range = request.headers.get("range")
    if client_range:
        headers["Range"] = client_range
    try:
        ctx, sresp = await _safe_proxy_stream(
            client, url, headers=headers, timeout=httpx.Timeout(None, connect=10.0, pool=5.0),
            method="HEAD" if (request.method or "GET").upper() == "HEAD" else "GET",
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {e}")

    if sresp.status_code >= 400:
        try:
            await ctx.__aexit__(None, None, None)
        except Exception:
            pass
        if sresp.status_code in (502, 504) and cache_key:
            await asyncio.to_thread(cache.invalidate, cache_key)
        raise HTTPException(status_code=502, detail=f"Upstream fetch failed (HTTP {sresp.status_code})")

    # Keep the upstream's framing and media type visible to Jellyfin/FFmpeg.
    # In particular, a known Content-Length makes a finite media resource
    # distinguishable from an open-ended stream for arbitrary containers.
    out_headers = {}
    for name in ("Content-Range", "Accept-Ranges", "Content-Length", "Content-Type"):
        value = sresp.headers.get(name)
        if value is not None:
            out_headers[name] = value
    media_type = sresp.headers.get("content-type", "application/octet-stream")

    if (request.method or "GET").upper() == "HEAD":
        try:
            await ctx.__aexit__(None, None, None)
        except Exception:
            pass
        return Response(status_code=sresp.status_code, headers=out_headers, media_type=media_type)

    async def proxy_stream():
        try:
            async for chunk in sresp.aiter_bytes(chunk_size=65536):
                yield chunk
        except asyncio.CancelledError:
            raise
        finally:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass

    return StreamingResponse(proxy_stream(), status_code=sresp.status_code,
                             headers=out_headers, media_type=media_type)


app.include_router(api)


def _legacy_redirect(request: Request, new_path: str):
    qs = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(url=new_path + qs, status_code=307)


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def root_redirect():
    return RedirectResponse(url="/admin", status_code=307)


@app.get("/health", include_in_schema=False)
async def legacy_health():
    return RedirectResponse(url="/admin/health", status_code=307)


@app.get("/api/{rest:path}", include_in_schema=False)
async def legacy_api(request: Request, rest: str):
    return _legacy_redirect(request, f"/admin/api/{rest}")


@app.api_route("/play/{rest:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def legacy_play(request: Request, rest: str):
    return _legacy_redirect(request, f"/admin/play/{rest}")
