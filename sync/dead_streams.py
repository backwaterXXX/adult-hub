"""Dead-stream denylisting and resolver CDN probes for availability checks."""

from common import db
import time as _time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse
import logging

logger = logging.getLogger(__name__)


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
    resolver_api_url: str
    logger: Any
    default_headers: Dict[str, str]
    time: Any = _time
    resolver_timeout: float = 120.0
    denylist_days: int = 90


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
                    logger.debug("Ignored exception in denylist_ids", exc_info=True)
    except Exception:
        logger.debug("Ignored exception in denylist_ids", exc_info=True)


def deadlisted_ids(conn: db.Connection) -> Set[str]:
    """Retrieve all video IDs currently marked in the dead_streams table."""
    try:
        return {r[0] for r in conn.execute("SELECT video_id FROM dead_streams").fetchall()}
    except Exception:
        logger.debug("Ignored exception in deadlisted_ids", exc_info=True)
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
        logger.debug("Ignored exception in probe_playlist_url", exc_info=True)
        return None


def _resolver_play_url(services: DeadStreamServices, comp_id: str, mode: str) -> str:
    url = f"{services.resolver_api_url.rstrip('/')}/admin/play/{comp_id}/stream"
    if mode != "redirect":
        url += "?mode=proxy"
    return url


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
    url = _resolver_play_url(services, comp_id, mode)
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
        logger.debug("Ignored exception in _probe_playlist_outcome", exc_info=True)
        return "transient_error" if _network_failure(exc) else "unknown"


def _outcome_from_redirect_mode(services: DeadStreamServices, status: int, location, chunk: bytes) -> str:
    if status == 302 and location:
        return _probe_playlist_outcome(location, user_agent=services.default_headers.get("User-Agent", "Mozilla/5.0"))
    body = chunk.decode("utf-8", errors="ignore").lower()
    if status == 502 and any(marker in body for marker in RESOLVER_DEAD_MARKERS):
        return "unavailable"
    return "transient_error" if status in TRANSIENT_HTTP_STATUSES else "unknown"


def _outcome_from_proxy_mode(status: int, chunk: bytes) -> str:
    body = chunk.decode("utf-8", errors="ignore")
    if status in (200, 206) and ("#EXTM3U" in body.upper() or b"ftyp" in chunk[:32]):
        return "available"
    if status == 502 and any(marker in body.lower() for marker in RESOLVER_DEAD_MARKERS):
        return "unavailable"
    if status in TRANSIENT_HTTP_STATUSES:
        return "transient_error"
    return "unknown"


def probe_stream_via_resolver_outcome(comp_id: str, *, services: DeadStreamServices) -> str:
    """Detailed, bounded probe outcome for the Admin worker."""
    if httpx is None:
        return "unknown"
    mode = services.stream_mode_for_provider(comp_id)
    url = _resolver_play_url(services, comp_id, mode)
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
        return _outcome_from_redirect_mode(services, status, location, chunk)
    return _outcome_from_proxy_mode(status, chunk)


def hide_dead_streams(collected: Dict[str, Dict[str, Any]], conn: db.Connection, *,
                      services: DeadStreamServices) -> None:
    """Drop denylisted corpses from the catalog before they reach disk.

    Availability evidence comes from the Admin worker (dead_streams rows); this
    pass only filters collected IDs and prunes denylist entries that are no
    longer listed upstream (allowing resurrection). Both the dead set and the
    collected set must be non-empty: a partial fetch must not nuke the table."""
    now = int(services.time.time())
    dead = deadlisted_ids(conn)
    listed = set(collected)
    for comp_id in [c for c in collected if c in dead]:
        del collected[comp_id]
    if dead and listed:
        try:
            with conn:
                conn.execute(
                    "DELETE FROM dead_streams WHERE detected_at < %s AND video_id NOT IN "
                    f"({','.join(['%s'] * len(listed))})",
                    (now - services.denylist_days * 86400, *listed))
        except Exception:
            logger.debug("Ignored exception in hide_dead_streams", exc_info=True)
