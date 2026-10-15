"""Performer photo backfill (performer pages og:image via resolver).

Performer pages carry a stable photo in meta ``og:image``. The slug
cannot be guessed from the name, so the resolver exposes
``GET /admin/api/performer-photo?provider={source}&name=...``. This module
caches those URLs locally in ``performer_photos`` so sync/NFO builds stay fast
and do not hammer the resolver. Which providers are tried (in order) is
owned by the private web extension (see register_photo_providers);
pass ``providers`` explicitly to override.

NFO Contract
------------
Shared contract with ``sync/sync.py::create_nfo_file`` (NFO implementation belongs
to the owner of ``create_nfo_file``; description only):

- video-dict carries the key ``"performer_thumbs"`` = ``{name: photo_url}``,
  e.g. ``{"John Doe": "https://cdn.example.org/.../photo.jpg"}``.
- ``create_nfo_file`` adds ``<thumb>URL</thumb>`` INSIDE ``<actor>``
  when the URL for the performer is non-empty::

      <actor>
        <name>John Doe</name>
        <role>Performer</role>
        <thumb>https://cdn.example.org/.../photo.jpg</thumb>
      </actor>

  Missing entry (or empty URL) = no ``<thumb>`` element (the ``<actor>``
  with ``<name>``/``<role>`` remains untouched).
- ``sync_unique_video`` and backfill load from the
  ``performer_photos`` table (``name`` -> ``thumb``) into the ``performer_thumbs`` key
  of the video-dict — outside this module's scope.
"""

import json
import logging
from common import db
from common.external_metadata import entity_key, record_attempt
from common.http_rate_limit import wait_for_host_slot
import time
from common.performer_profiles import get_override_fields, lock_profile_enrichment
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

try:
    import httpx
except ImportError:  # pragma: no cover - requirements pin httpx
    httpx = None  # type: ignore

logger = logging.getLogger("sync.photos")

FETCH_TIMEOUT = 15.0

# Photo-hook providers tried in order (first non-empty thumb wins).
# ETAP D: owned by the private web extension (registered at sync load via
# register_photo_providers()); empty without it = resolver photo lookups
# idle, names cache an empty thumb. Pass ``providers`` explicitly to
# override (tests, standalone runs).
_PHOTO_PROVIDERS: tuple = ()


def register_photo_providers(providers) -> None:
    """Register ordered photo-hook providers (web extension only)."""
    global _PHOTO_PROVIDERS
    _PHOTO_PROVIDERS = tuple(p for p in (providers or ()) if p)


def photo_providers() -> tuple:
    """Currently registered photo-hook providers (may be empty)."""
    return tuple(_PHOTO_PROVIDERS)


def ensure_tables(conn: db.Connection) -> None:
    """Ensure the performer photo cache table exists (managed by versioned migrations)."""
    try:
        from common.migrations import apply_migrations
        apply_migrations(conn)
    except Exception:
        pass


def _default_fetch(url: str, timeout: float = FETCH_TIMEOUT) -> Dict[str, Any]:
    """Fetch performer-photo JSON from the resolver (404 = no photo)."""
    if httpx is None:
        raise RuntimeError("httpx not available")
    resp = httpx.get(url, timeout=timeout)
    if resp.status_code == 404:
        return {}
    resp.raise_for_status()
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _call_fetch(fetch: Callable, url: str) -> Any:
    """Call an injectable fetcher as fetch(url, timeout=...) (fallback: url)."""
    wait_for_host_slot(url)
    try:
        return fetch(url, timeout=FETCH_TIMEOUT)
    except TypeError:
        return fetch(url)


def _missing_performer_names(conn: db.Connection, limit: Optional[int]) -> List[str]:
    """Distinct due names lacking a photo, ordered fairly across sync runs."""
    rows = conn.execute(
        "SELECT performers FROM videos "
        "WHERE performers <> '[]'::jsonb"
    ).fetchall()
    try:
        photos = {str(r[0]).casefold(): str(r[1] or "") for r in
                  conn.execute("SELECT name,thumb FROM performer_photos").fetchall()}
    except Exception:
        photos = {}
    try:
        attempt_rows = conn.execute(
            "SELECT entity_key,EXTRACT(EPOCH FROM next_attempt_at),"
            "EXTRACT(EPOCH FROM last_attempt_at) FROM external_metadata_attempts "
            "WHERE entity_type='performer' AND source='photos'"
        ).fetchall()
        attempts = {str(r[0]).casefold(): (float(r[1]), float(r[2])) for r in attempt_rows}
    except Exception:
        attempts = {}
    names = set()
    for (raw,) in rows:
        if not raw:
            continue
        try:
            items = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str) and item.strip():
                clean_item = item.strip()
                if not (clean_item.lower() in {"fucked", "fuck", "fucking", "fucker"} or clean_item.lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"))):
                    names.add(clean_item)
    now = time.time()
    missing = []
    for name in names:
        key = entity_key(name)
        previous = attempts.get(key)
        if previous and previous[0] > now:
            continue
        existing_photo = photos.get(key)
        if existing_photo and previous is None:
            continue
        missing.append(name)
    missing.sort(key=lambda name: (
        attempts.get(entity_key(name), (0, 0))[1], name.casefold(), name))
    if limit is not None:
        missing = missing[: max(0, int(limit))]
    return missing


def backfill_photos(
    conn: db.Connection,
    resolver_api_url: str,
    limit: int = 25,
    sleep: float = 1.0,
    fetch: Optional[Callable] = None,
    providers: Optional[tuple] = None,
) -> Tuple[int, int]:
    """Backfill performer photo URLs into ``performer_photos``.

    Takes distinct performers from ``videos.performers`` with no usable photo
    or whose source refresh is due, queries the resolver
    ``GET {resolver}/admin/api/performer-photo`` per name trying
    ``providers`` in order (default: registered photo providers; first
    non-empty thumb wins), and stores the thumb — even an empty one (with
    ``updated_at``) so the same name is not asked about on every run.
    Sleeps ``sleep`` seconds between requests. Returns ``(checked,
    updated)`` where ``updated`` counts names with a non-empty thumb.
    Never raises.
    """
    checked = 0
    updated = 0
    ordered = tuple(providers) if providers is not None else photo_providers()
    if not ordered:
        logger.debug("Performer photo backfill skipped: no photo providers registered")
        return 0, 0
    try:
        if limit is not None and int(limit) <= 0:
            return 0, 0
        try:
            ensure_tables(conn)
        except Exception as e:
            logger.warning(f"Performer photo backfill: ensure_tables failed: {e}")
            return 0, 0
        try:
            names = _missing_performer_names(conn, limit)
        except Exception as e:
            logger.warning(f"Performer photo backfill: query failed: {e}")
            return 0, 0
    except Exception as e:
        logger.warning(f"Performer photo backfill failed: {e}")
        return checked, updated

    do_fetch = fetch or _default_fetch
    base = (resolver_api_url or "").rstrip("/")
    for name in names:
        checked += 1
        failed = None
        try:
            if "photo" in get_override_fields(conn, name):
                continue
            thumb = ""
            for provider in ordered:
                url = (f"{base}/admin/api/performer-photo"
                       f"?provider={provider}&name={quote(name, safe='')}")
                try:
                    data = _call_fetch(do_fetch, url)
                except Exception as e:
                    failed = e
                    continue
                if isinstance(data, dict):
                    thumb = str(data.get("photo_url") or "").strip()
                elif data:
                    thumb = str(data).strip()
                if thumb:
                    break
            if not thumb and failed:
                record_attempt(conn, "performer", entity_key(name), "photos", "error", error=failed)
                continue
            try:
                with conn:
                    lock_profile_enrichment(conn, name)
                    if "photo" in get_override_fields(conn, name):
                        continue
                    conn.execute(
                        "INSERT INTO performer_photos (name, thumb, updated_at)"
                        " VALUES (%s, %s, %s) ON CONFLICT (name) DO UPDATE SET "
                        "thumb=CASE WHEN performer_photos.thumb IS NULL OR performer_photos.thumb='' "
                        "THEN EXCLUDED.thumb ELSE performer_photos.thumb END, "
                        "updated_at=EXCLUDED.updated_at",
                        (name, thumb, int(time.time())),
                    )
            except Exception as e:
                logger.warning(f"Performer photo backfill: store failed for {name!r}: {e}")
                continue
            if thumb:
                updated += 1
                record_attempt(conn, "performer", entity_key(name), "photos", "found")
            else:
                record_attempt(conn, "performer", entity_key(name), "photos", "no_match")
        except Exception as e:
            try:
                record_attempt(conn, "performer", entity_key(name), "photos", "error", error=e)
            except Exception:
                logger.debug("Could not save photo retry state for %r", name)
            logger.warning(f"Performer photo backfill failed for {name!r}: {e}")
        finally:
            try:
                if sleep and float(sleep) > 0:
                    time.sleep(float(sleep))
            except Exception:
                pass
    if checked:
        logger.info("Performer photo backfill: checked=%d enriched=%d", checked, updated)
    return checked, updated
