"""Read-only audit of Telegram NFO hashes against Jellyfin's item inventory."""

import hashlib
import json
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


def audit_telegram_import_state(url, api_key, media_dir, *, state_path, session=None, progress=None):
    """Compare imported NFO hashes with Jellyfin's current Telegram items.

    The state file is keyed by Jellyfin item ID, so paths must be resolved from
    Jellyfin before comparing hashes. Fail closed as ``unknown`` when the state
    file or any page of the read-only API scan is unavailable.
    """
    if not url or not api_key:
        return {"status": "unknown", "findings": []}
    try:
        state = json.loads(Path(state_path).read_text())
        if not isinstance(state, dict):
            return {"status": "unknown", "findings": []}
        import requests
        session = session or requests.Session()
        base = url.rstrip("/")
        headers = {
            "X-Emby-Token": api_key,
            "Authorization": f'MediaBrowser Client="adult-hub", Device="sync", DeviceId="adult-hub-sync", Version="1.0.0", Token="{api_key}"',
        }
        media_dir = Path(media_dir).resolve()
        findings = []
        start = 0
        seen = set()
        expected_total = None
        while True:
            page = _read_page(session, base, headers, start, expected_total)
            if page is None:
                return {"status": "unknown", "findings": []}
            items, total, expected_total = page
            page_findings = _scan_page_items(items, media_dir, state, seen)
            if page_findings is None:
                return {"status": "unknown", "findings": []}
            findings.extend(page_findings)
            start += len(items)
            if progress is not None:
                progress(start, total)
            # An empty page before the reported end means the scan is
            # incomplete. Never let a partial Jellyfin inventory resolve old
            # import-state findings as though the whole library was checked.
            if not items and total > start:
                return {"status": "unknown", "findings": []}
            if not items or start >= total:
                break
        return {"status": "checked", "findings": findings}
    except Exception:
        # API exceptions can embed credential-bearing request details. The audit
        # report deliberately exposes only an unknown status.
        logger.debug("Ignored exception in audit_telegram_import_state", exc_info=True)
        return {"status": "unknown", "findings": []}


def _fetch_items_page(session, base: str, headers: dict, start: int):
    response = session.request("GET", base + "/Items", headers=headers, timeout=20, params={
        "Recursive": "true", "IncludeItemTypes": "Movie", "Fields": "Path",
        "StartIndex": start, "Limit": 500,
    })
    response.raise_for_status()
    return response.json()


def _read_page(session, base: str, headers: dict, start: int, expected_total):
    """Fetch one page and check it against the inventory seen so far.

    Returns (items, total, expected_total), or None when the page cannot be trusted.
    """
    parsed = _items_and_total(_fetch_items_page(session, base, headers, start))
    if parsed is None:
        return None
    items, total = parsed
    if expected_total is None:
        expected_total = total
    elif total != expected_total:
        # A changing inventory cannot be fully verified by an offset
        # scan. Do not resolve findings based on this mixed snapshot.
        return None
    if len(items) > total - start:
        return None
    return items, total, expected_total


def _items_and_total(page):
    """Return (items, total) from a page, or None if the page shape is not usable."""
    if not isinstance(page, dict) or not isinstance(page.get("Items"), list):
        return None
    total = page.get("TotalRecordCount")
    if type(total) is not int or total < 0:
        return None
    return page["Items"], total


def _scan_page_items(items: list, media_dir: Path, state: dict, seen: set) -> list | None:
    """Return (filename, reason) findings for one page, or None if the page cannot be trusted."""
    findings = []
    for item in items:
        if not isinstance(item, dict):
            return None
        item_id, raw_path = item.get("Id"), item.get("Path")
        if not item_id or not isinstance(raw_path, str) or not raw_path:
            return None
        if item_id in seen:
            return None
        seen.add(item_id)
        if not raw_path.startswith("/media/adult/"):
            continue
        filename = Path(raw_path).name
        if not filename.startswith("telegram [") or not filename.lower().endswith(".strm"):
            continue
        nfo_path = media_dir / Path(filename).with_suffix(".nfo")
        if nfo_path.is_symlink() or not nfo_path.resolve().is_relative_to(media_dir):
            continue
        try:
            digest = hashlib.sha256(nfo_path.read_bytes()).hexdigest()
        except OSError:
            # The local audit reports an absent/unreadable sidecar separately.
            continue
        recorded = state.get(str(item_id))
        if recorded != digest:
            findings.append((filename, "nfo_import_state_missing" if recorded is None
                             else "nfo_import_state_mismatch"))
    return findings


