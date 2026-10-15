"""Jellyfin reconciliation through its supported HTTP API."""

import json
import logging
from common import db
import time
import hashlib
import xml.etree.ElementTree as ET
import urllib.request
from pathlib import Path

_LOGGER = logging.getLogger("sync")


def _load_jellyfin_sibling(module_name):
    """Load a sibling module both as a package and from direct-file imports."""
    import importlib
    import importlib.util
    import sys

    if __package__:
        return importlib.import_module(f".{module_name}", __package__)
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise
    cache_name = f"_adult_hub_{module_name}"
    cached = sys.modules.get(cache_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        cache_name, Path(__file__).with_name(f"{module_name}.py"),
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Jellyfin module {module_name!r}")
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[cache_name] = loaded
    try:
        spec.loader.exec_module(loaded)
    except Exception:
        sys.modules.pop(cache_name, None)
        raise
    return loaded
_LAST_LIBRARY_REFRESH_TIME = 0.0
_LAST_LIBRARY_REFRESH_RESULT = False


def trigger_library_refresh(url, api_key, *, min_interval=3.0, http_client_factory=None,
                            logger=None, time_fn=time.time):
    """Ask Jellyfin to refresh its library, throttling only recent successes."""
    global _LAST_LIBRARY_REFRESH_TIME, _LAST_LIBRARY_REFRESH_RESULT
    log = logger or _LOGGER
    if not url or not api_key:
        log.warning("Jellyfin refresh skipped: JELLYFIN_URL/JELLYFIN_API_KEY not set")
        return False
    now = time_fn()
    if _LAST_LIBRARY_REFRESH_RESULT and (now - _LAST_LIBRARY_REFRESH_TIME) < min_interval:
        log.info("Jellyfin library refresh throttled (recent refresh succeeded)")
        return True
    try:
        endpoint = f"{url.rstrip('/')}/Library/Refresh"
        headers = {
            "Authorization": f'MediaBrowser Client="adult-hub", Device="sync", DeviceId="adult-hub-sync", Version="1.0.0", Token="{api_key}"',
            "X-Emby-Token": api_key,
        }
        client = http_client_factory() if callable(http_client_factory) else None
        if client:
            response = client.post(endpoint, headers=headers, timeout=10.0)
            log.info("Jellyfin library refresh triggered: HTTP %s", response.status_code)
            ok = 200 <= response.status_code < 300
        else:
            request = urllib.request.Request(endpoint, method="POST", headers=headers)
            with urllib.request.urlopen(request, timeout=10) as response:
                status = response.status
                log.info("Jellyfin library refresh triggered: HTTP %s", status)
                ok = 200 <= status < 300
        _LAST_LIBRARY_REFRESH_TIME = time_fn()
        _LAST_LIBRARY_REFRESH_RESULT = ok
        return ok
    except Exception as error:
        log.warning("Could not trigger Jellyfin refresh: %s", error)
        _LAST_LIBRARY_REFRESH_TIME = time_fn()
        _LAST_LIBRARY_REFRESH_RESULT = False
        return False


def confirm_metadata_import_jobs(confirmed_files, db_url, *, logger=None, time_fn=time.time):
    """Link Jellyfin-confirmed NFO hashes to completed metadata tasks."""
    log = logger or _LOGGER
    try:
        conn = db.connect(db_url)
        try:
            confirmed = {Path(str(name)).with_suffix(".nfo").name: digest
                         for name, digest in confirmed_files.items()
                         if isinstance(name, str) and isinstance(digest, str)}
            jobs = conn.execute(
                "SELECT id,payload,result FROM video_duplicate_jobs WHERE action='metadata_update' "
                "AND state='done' AND result IS NOT NULL ORDER BY updated_at DESC LIMIT 1000"
            ).fetchall()
            for job_id, raw_payload, raw_result in jobs:
                try:
                    payload = raw_payload if isinstance(raw_payload, dict) else json.loads(raw_payload or "{}")
                    outcome = raw_result if isinstance(raw_result, dict) else json.loads(raw_result or "{}")
                except (TypeError, ValueError):
                    continue
                video_id = str(payload.get("video_id") or "") if isinstance(payload, dict) else ""
                if (not video_id or not isinstance(outcome, dict)
                        or outcome.get("jellyfin_import") != "not_confirmed"
                        or outcome.get("nfo_written") is not True):
                    continue
                expected_hashes = outcome.get("nfo_content_hashes")
                if (not isinstance(expected_hashes, dict)
                        or not any(confirmed.get(name) == digest
                                   for name, digest in expected_hashes.items())):
                    continue
                outcome["jellyfin_import"] = "confirmed"
                outcome["jellyfin_import_confirmed_at"] = int(time_fn())
                with conn:
                    conn.execute(
                        "UPDATE video_duplicate_jobs SET result=%s,updated_at=%s WHERE id=%s AND state='done'",
                        (json.dumps(outcome, ensure_ascii=False), int(time_fn()), job_id),
                    )
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Could not reconcile confirmed metadata imports (%s)", type(exc).__name__)


def _telegram_nfo(path):
    """Return the import contract; refuse network artwork before unlocking an item."""
    raw = path.read_bytes()
    root = ET.fromstring(raw)
    if root.findtext('lockdata', '').lower() != 'true':
        raise ValueError('Telegram NFO must contain lockdata=true')
    if any((node.text or '').strip().startswith(('http://', 'https://'))
           for node in list(root.findall('thumb')) + list(root.findall('fanart/thumb'))):
        raise ValueError('Telegram NFO contains remote artwork')
    return hashlib.sha256(raw).hexdigest(), {
        'Name': (root.findtext('title') or root.findtext('name') or '').strip(),
        'OriginalTitle': (root.findtext('originaltitle') or '').strip(),
        'Overview': (root.findtext('plot') or '').strip(),
        'ProviderIds': {node.get('type', '').strip(): (node.text or '').strip()
                        for node in root.findall('uniqueid') if node.get('type') and (node.text or '').strip()},
    }


def audit_telegram_import_state(url, api_key, media_dir, *, state_path, session=None, progress=None):
    """Compatibility facade for the read-only Jellyfin NFO import audit."""
    implementation = _load_jellyfin_sibling("jellyfin_import_audit")
    return implementation.audit_telegram_import_state(
        url, api_key, media_dir, state_path=state_path, session=session, progress=progress,
    )


def _matches_complete_nfo(dto, path):
    """Conservatively adopt an already imported, locked NFO without refreshing."""
    root = ET.fromstring(path.read_bytes())
    def normalized(values):
        return sorted({(value or '').strip() for value in values if (value or '').strip()})
    expected_genres = [part for node in root.findall('genre') for part in (node.text or '').split('/')]
    if normalized(dto.get('Genres', [])) != normalized(expected_genres):
        return False
    if normalized(dto.get('Tags', [])) != normalized(node.text for node in root.findall('tag')):
        return False
    if normalized(studio.get('Name') for studio in dto.get('Studios', [])) != normalized(node.text for node in root.findall('studio')):
        return False
    people = sorted((actor.findtext('name', '').strip(), actor.findtext('role', '').strip(), 'Actor')
                    for actor in root.findall('actor') if actor.findtext('name', '').strip())
    actual_people = sorted((person.get('Name', '').strip(), (person.get('Role') or '').strip(), person.get('Type'))
                           for person in dto.get('People', []))
    if people != actual_people:
        return False
    runtime = root.findtext('runtime')
    if runtime:
        # Generated NFO stores whole minutes, floored, with a one-minute minimum.
        # Previously reconciled/probed durations can retain more precise seconds.
        minute = 60 * 10000000
        expected_ticks = int(runtime) * minute
        actual_ticks = dto.get('RunTimeTicks') or 0
        if not (expected_ticks <= actual_ticks < expected_ticks + minute
                or (expected_ticks == minute and 0 < actual_ticks < 2 * minute)):
            return False
    ids = {key.lower(): value for key, value in dto.get('ProviderIds', {}).items()}
    expected_ids = {node.get('type', '').lower(): node.text for node in root.findall('uniqueid')}
    # Do not adopt foreign online IDs left behind by a previous incorrect match.
    return ids == expected_ids


def sync_jellyfin_telegram_metadata(url, api_key, media_dir, *, state_path=None,
                                   limit=200, item_ids=None, audit_only=False,
                                   poll_timeout=120, logger=None, session=None,
                                   workers=1, adopt_matching=False):
    """Serialize writers sharing the same persistent reconciliation state."""
    options = dict(state_path=state_path, limit=limit, item_ids=item_ids,
                   audit_only=audit_only, poll_timeout=poll_timeout, logger=logger,
                   session=session, workers=workers, adopt_matching=adopt_matching)
    if audit_only or not url or not api_key:
        return _sync_jellyfin_telegram_metadata(url, api_key, media_dir, **options)
    import fcntl
    lock_path = Path(state_path or '/data/telegram-metadata-state.json').with_suffix('.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            (logger or _LOGGER).warning('Telegram metadata reconciliation already running; skipping')
            return {'checked': 0, 'needed': 0, 'updated': 0, 'failed': 0,
                    'adopted': 0, 'skipped': 'already_running'}
        try:
            return _sync_jellyfin_telegram_metadata(url, api_key, media_dir, **options)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _sync_jellyfin_telegram_metadata(url, api_key, media_dir, *, state_path=None,
                                   limit=200, item_ids=None, audit_only=False,
                                   poll_timeout=120, logger=None, session=None,
                                   workers=1, adopt_matching=False):
    """Compatibility facade for the Telegram NFO API reconciliation worker."""
    implementation = _load_jellyfin_sibling("jellyfin_telegram_metadata")
    return implementation.sync_jellyfin_telegram_metadata(
        url, api_key, media_dir, state_path=state_path, limit=limit, item_ids=item_ids,
        audit_only=audit_only, poll_timeout=poll_timeout, logger=logger, session=session,
        workers=workers, adopt_matching=adopt_matching, _core=globals(),
    )


if __name__ == '__main__':
    import argparse
    import os
    parser = argparse.ArgumentParser(description='Audit/import Telegram NFO via Jellyfin API')
    parser.add_argument('--media-dir', default=os.getenv('MEDIA_DIR', '/media/adult'))
    parser.add_argument('--state-path', default='/data/telegram-metadata-state.json')
    parser.add_argument('--limit', type=int, default=200, help='Maximum imports; 0 means all')
    parser.add_argument('--poll-timeout', type=float, default=120,
                        help='Seconds to wait for an item import (must be positive)')
    parser.add_argument('--item-id', action='append')
    parser.add_argument('--workers', type=int, default=1, choices=range(1, 5))
    parser.add_argument('--adopt-matching', action='store_true')
    parser.add_argument('--apply', action='store_true', help='Default is read-only audit')
    args = parser.parse_args()
    if args.poll_timeout <= 0:
        parser.error('--poll-timeout must be positive')
    result = sync_jellyfin_telegram_metadata(
        os.getenv('JELLYFIN_URL'), os.getenv('JELLYFIN_API_KEY'), args.media_dir,
        state_path=args.state_path, limit=args.limit, item_ids=args.item_id,
        audit_only=not args.apply,
        workers=args.workers, adopt_matching=args.adopt_matching,
        poll_timeout=args.poll_timeout,
    )
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(1 if result.get('failed', 0) else 0)


def sync_jellyfin_titles(hub_db_url=None, *, default_hub_db_url=None, clean_title,
                         url=None, api_key=None, logger=None, session=None, media_dir="/media/adult"):
    """Compatibility facade for Jellyfin display-title synchronization."""
    implementation = _load_jellyfin_sibling("jellyfin_titles")
    return implementation.sync_jellyfin_titles(
        hub_db_url, default_hub_db_url=default_hub_db_url, clean_title=clean_title,
        url=url, api_key=api_key, logger=logger, session=session, media_dir=media_dir,
    )
