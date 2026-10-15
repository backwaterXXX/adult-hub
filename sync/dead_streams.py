"""Dead stream detection, resolver CDN probes, and catalog denylisting."""

import re
from common import db
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

try:
    import httpx
except ImportError:
    httpx = None


RESOLVER_DEAD_MARKERS: Tuple[str, ...] = (
    # NOTE: deliberately narrow — the generic "failed to resolve stream"
    # wrapper also covers transient network errors, which must never strike.
    "freshly resolved url is dead",
    "could not resolve",
    "upstream playlist unavailable",
    "not found",
    "failed to resolve stream: client error '403",
    "failed to resolve stream: client error '404",
    "failed to resolve stream: client error '410",
    "client error '403 forbidden'",
    "client error '404 not found'",
    "could not resolve playable stream",
)

TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class DeadStreamServices:
    """Dependencies supplied dynamically by the sync.py facade."""

    stream_mode_for_provider: Callable
    dead_stream_providers: Callable
    is_cleanup_allowed: Callable
    purge_videos: Callable
    probe_stream_via_resolver: Callable
    resolver_api_url: str
    logger: Any
    default_headers: Dict[str, str]
    time: Any = _time
    resolver_timeout: float = 120.0
    sleep_seconds: float = 1.0
    denylist_days: int = 90
    recheck_days: int = 7
    check_per_run: int = 25
    strikes_to_purge: int = 1
    max_purge_per_run: int = 10


def extract_embed_host(page_html: str) -> str:
    """Extract the embed iframe host from a video post page ('' if none)."""
    if not page_html:
        return ""
    m = re.search(r'itemprop=["\']embedUrl["\']\s+content=["\']([^"\']+)["\']', page_html) or \
        re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', page_html)
    if not m:
        return ""
    url = m.group(1).strip()
    if url.startswith("//"):
        url = "https:" + url
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def is_unsupported_embed_host(host: str, deny: tuple) -> bool:
    """Check if the given host matches any entry in the denylist."""
    return bool(host) and any(h in host for h in deny)


def denylist_ids(conn: db.Connection, comp_ids: List[str], reason: str, *,
                 time_fn: Optional[Callable[[], float]] = None) -> None:
    """Record proven-dead video IDs so future syncs skip them (no re-add,
    no re-probe) while they are still listed upstream."""
    if not comp_ids:
        return
    try:
        now = int((time_fn or _time.time)())
        with conn:
            for vid in comp_ids:
                try:
                    conn.execute(
                        "INSERT INTO dead_streams (video_id, reason, detected_at)"
                        " VALUES (%s, %s, %s) ON CONFLICT (video_id) DO NOTHING", (vid, reason, now))
                except Exception:
                    pass
    except Exception:
        pass


def deadlisted_ids(conn: db.Connection) -> Set[str]:
    """Retrieve all video IDs currently marked in the dead_streams table."""
    try:
        return {r[0] for r in conn.execute("SELECT video_id FROM dead_streams").fetchall()}
    except Exception:
        return set()


def probe_playlist_url(playlist_url: str, *, user_agent: Optional[str] = None) -> Optional[bool]:
    """Range-probe a (usually signed) playlist URL. True/False/None."""
    if httpx is None or not playlist_url.startswith(("http://", "https://")):
        return None
    ua = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    try:
        parsed = urlparse(playlist_url)
        referer = f"{parsed.scheme}://{parsed.netloc}/"
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0),
                          follow_redirects=True) as client:
            with client.stream("GET", playlist_url,
                               headers={"User-Agent": ua,
                                        "Referer": referer, "Range": "bytes=0-0"}) as r:
                if r.status_code in (200, 206):
                    return True
                if r.status_code in (404, 410):
                    return False
                return None
    except Exception:
        return None


def probe_stream_via_resolver(comp_id: str, *,
                              services: DeadStreamServices,
                              probe_playlist_fn: Optional[Callable[[str], Optional[bool]]] = None) -> Optional[bool]:
    """Full play-chain probe through the resolver (what the player gets).

    True = playlist serves, False = proven dead upstream, None = unknown
    (timeout / resolver down / unexpected status — never a strike).
    """
    if httpx is None:
        return None
    mode = services.stream_mode_for_provider(comp_id)
    url = f"{services.resolver_api_url.rstrip('/')}/admin/play/{comp_id}/stream"
    if mode != "redirect":
        url += "?mode=proxy"
    probe_fn = probe_playlist_fn or (lambda u: probe_playlist_url(u, user_agent=services.default_headers.get("User-Agent")))
    try:
        with httpx.Client(
            timeout=httpx.Timeout(services.resolver_timeout, connect=10.0),
            follow_redirects=False,
        ) as client:
            with client.stream(
                "GET",
                url,
                headers={"User-Agent": services.default_headers.get("User-Agent", "Mozilla/5.0"),
                         "Range": "bytes=0-4096"},
            ) as r:
                r_status = r.status_code
                r_headers = dict(r.headers)
                chunk = b""
                if r_status != 302:
                    for c in r.iter_bytes(4096):
                        chunk = c
                        break
    except Exception as e:
        services.logger.debug(f"Stream probe failed for {comp_id}: {e}")
        return None
    if mode == "redirect":
        if r_status == 302 and r_headers.get("location"):
            return probe_fn(r_headers["location"])
        text = chunk.decode("utf-8", errors="ignore")
        if r_status == 502 and any(m in text.lower() for m in RESOLVER_DEAD_MARKERS):
            return False
        return None
    body = chunk.decode("utf-8", errors="ignore")
    if r_status in (200, 206) and ("#EXTM3U" in body.upper() or b"ftyp" in chunk[:32]):
        return True
    if r_status == 502 and any(m in body.lower() for m in RESOLVER_DEAD_MARKERS):
        return False
    return None


def _network_failure(exc: Exception) -> bool:
    if httpx is not None and isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    name = type(exc).__name__.lower()
    return any(token in name for token in ("timeout", "connect", "network"))


def _probe_playlist_outcome(url: str, *, user_agent: str) -> str:
    if httpx is None or not url.startswith(("http://", "https://")):
        return "unknown"
    try:
        parsed = urlparse(url)
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True) as client:
            with client.stream("GET", url, headers={"User-Agent": user_agent,
                                                     "Referer": f"{parsed.scheme}://{parsed.netloc}/",
                                                     "Range": "bytes=0-0"}) as response:
                if response.status_code in (200, 206):
                    return "available"
                if response.status_code in (404, 410):
                    return "unavailable"
                if response.status_code in TRANSIENT_HTTP_STATUSES:
                    return "transient_error"
                return "unknown"
    except Exception as exc:
        return "transient_error" if _network_failure(exc) else "unknown"


def probe_stream_via_resolver_outcome(comp_id: str, *, services: DeadStreamServices) -> str:
    """Detailed, bounded probe outcome for the Admin worker."""
    if httpx is None:
        return "unknown"
    mode = services.stream_mode_for_provider(comp_id)
    url = f"{services.resolver_api_url.rstrip('/')}/admin/play/{comp_id}/stream"
    if mode != "redirect":
        url += "?mode=proxy"
    try:
        with httpx.Client(timeout=httpx.Timeout(services.resolver_timeout, connect=10.0),
                          follow_redirects=False) as client:
            with client.stream("GET", url, headers={
                "User-Agent": services.default_headers.get("User-Agent", "Mozilla/5.0"),
                "Range": "bytes=0-4096",
            }) as response:
                status = response.status_code
                location = response.headers.get("location")
                chunk = b""
                if status != 302:
                    for chunk in response.iter_bytes(4096):
                        break
    except Exception as exc:
        services.logger.debug("Detailed stream availability probe failed (%s)", type(exc).__name__)
        return "transient_error" if _network_failure(exc) else "unknown"
    if mode == "redirect":
        if status == 302 and location:
            return _probe_playlist_outcome(location, user_agent=services.default_headers.get("User-Agent", "Mozilla/5.0"))
        body = chunk.decode("utf-8", errors="ignore").lower()
        if status == 502 and any(marker in body for marker in RESOLVER_DEAD_MARKERS):
            return "unavailable"
        return "transient_error" if status in TRANSIENT_HTTP_STATUSES else "unknown"
    body = chunk.decode("utf-8", errors="ignore")
    if status in (200, 206) and ("#EXTM3U" in body.upper() or b"ftyp" in chunk[:32]):
        return "available"
    if status == 502 and any(marker in body.lower() for marker in RESOLVER_DEAD_MARKERS):
        return "unavailable"
    if status in TRANSIENT_HTTP_STATUSES:
        return "transient_error"
    return "unknown"


def hide_dead_streams(collected: Dict[str, Dict[str, Any]], conn: db.Connection,
                      media_dir: Path,
                      providers_succeeded: Optional[Set[str]] = None,
                      providers: Optional[Iterable[str]] = None, *,
                      services: DeadStreamServices) -> int:
    """Drop denylisted corpses from the catalog and CDN-probe a capped number
    of proxy-provider videos (two strikes on separate runs before purge).

    ``providers`` selects the probed providers (default: extension-registered
    dead-stream providers; empty without the extension = denylist filtering
    only, no probes). Historical purges go through the retention guard: only
    registered, enabled and this-run-succeeded providers lose rows/files."""
    now = int(services.time.time())
    # 0. Denylist pre-filter: re-added corpses never touch disk or probes.
    dead = deadlisted_ids(conn)
    listed = set(collected)
    for comp_id in [c for c in collected if c in dead]:
        del collected[comp_id]
    # Prune old denylist entries no longer listed upstream (allow
    # resurrection). Both conditions: partial-fetch runs must not nuke.
    if dead and listed:
        try:
            with conn:
                conn.execute(
                    "DELETE FROM dead_streams WHERE detected_at < %s AND video_id NOT IN "
                    f"({','.join(['%s'] * len(listed))})",
                    (now - services.denylist_days * 86400, *listed))
        except Exception:
            pass

    cutoff = now - services.recheck_days * 86400
    probed_providers = tuple(providers) if providers is not None else services.dead_stream_providers()
    try:
        if not probed_providers:
            return 0
        prov_placeholders = ",".join(["%s"] * len(probed_providers))
        rows = conn.execute(
            f"""SELECT id, COALESCE(stream_fails, 0) FROM videos
               WHERE provider IN ({prov_placeholders}) AND playable = 1
                 AND (stream_checked IS NULL OR stream_checked < %s
                      OR COALESCE(stream_fails, 0) > 0)
                 AND id NOT IN (SELECT video_id FROM dead_streams)
               ORDER BY COALESCE(stream_fails, 0) DESC, last_seen DESC LIMIT %s""",
            (*probed_providers, cutoff, services.check_per_run)).fetchall()
    except Exception as e:
        services.logger.debug(f"Dead-stream candidate query failed: {e}")
        return 0

    to_purge: List[str] = []
    probed = 0
    unknown_streak = 0
    for vid_id, fails in rows:
        if vid_id in collected or vid_id in dead:
            continue  # fresh videos ride the embed filters; corpses stay out
        result = services.probe_stream_via_resolver(vid_id)
        probed += 1
        if result is True:
            try:
                with conn:
                    conn.execute(
                        "UPDATE videos SET stream_checked = %s, stream_fails = 0 WHERE id = %s",
                        (now, vid_id))
            except Exception:
                pass
            unknown_streak = 0
        elif result is False:
            fails = (fails or 0) + 1
            try:
                with conn:
                    conn.execute(
                        "UPDATE videos SET stream_checked = %s, stream_fails = %s WHERE id = %s",
                        (now, fails, vid_id))
            except Exception:
                pass
            unknown_streak = 0
            if fails >= services.strikes_to_purge:
                try:
                    with conn:
                        conn.execute("UPDATE videos SET playable = 0 WHERE id = %s", (vid_id,))
                except Exception:
                    pass
                to_purge.append(vid_id)
        else:
            unknown_streak += 1
            if unknown_streak >= 3:
                services.logger.warning("Dead-stream probe: 3 consecutive unknowns, stopping this run")
                break
        services.time.sleep(services.sleep_seconds)

    if probed:
        services.logger.info(f"Dead-stream probe: {probed} videos checked, {len(to_purge)} to purge")
    if len(to_purge) > services.max_purge_per_run:
        # Mass-purge protection: a provider-wide bad day must not wipe the
        # library — the rest keeps its strikes and drains over next runs.
        services.logger.warning(f"Dead-stream purge capped: {len(to_purge)} candidates,"
                                f" purging {services.max_purge_per_run} this run")
        to_purge = to_purge[:services.max_purge_per_run]
    to_purge = [v for v in to_purge
                if services.is_cleanup_allowed(v, providers_succeeded=providers_succeeded)]
    if to_purge:
        denylist_ids(conn, to_purge, "dead CDN stream (probe)", time_fn=services.time.time)
    return services.purge_videos(conn, media_dir, to_purge, "dead CDN stream (probe)",
                                 providers_succeeded=providers_succeeded)
