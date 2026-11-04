"""Jellyfin display-title synchronization through the supported item API."""

import json
import logging
from pathlib import Path

from common import db

_LOGGER = logging.getLogger("sync")


def sync_jellyfin_titles(hub_db_url=None, *, default_hub_db_url=None, clean_title,
                         url=None, api_key=None, logger=None, session=None, media_dir="/media/adult"):
    """Sync catalog display titles through Jellyfin's supported item API."""
    logger = logger or _LOGGER
    if not url or not api_key:
        logger.info("Jellyfin title sync skipped: API URL or key is not configured")
        return {"checked": 0, "updated": 0, "failed": 0, "skipped": "not_configured"}

    import requests

    session = session or requests.Session()
    headers = {
        "X-Emby-Token": api_key,
        "Authorization": f'MediaBrowser Client="adult-hub", Device="sync", DeviceId="adult-hub-sync", Version="1.0.0", Token="{api_key}"',
    }
    base_url = url.rstrip("/")
    media_root = Path(media_dir).resolve()
    try:
        title_map = _load_title_map(hub_db_url, default_hub_db_url, clean_title)
    except Exception as error:
        logger.warning("Could not load titles for Jellyfin API sync (%s)", type(error).__name__)
        return {"checked": 0, "updated": 0, "failed": 1}

    checked, updated, failed = _scan_items(session, base_url, headers, media_root, title_map, logger)
    logger.info("Jellyfin title sync checked=%d updated=%d failed=%d", checked, updated, failed)
    return {"checked": checked, "updated": updated, "failed": failed}


def _load_title_map(hub_db_url, default_hub_db_url, clean_title) -> dict:
    """Map each STRM file name to its (display title, original title) pair."""
    conn_hub = None
    try:
        conn_hub = db.connect(hub_db_url or default_hub_db_url)
        title_map = {}
        for file_path, provider, title, slug, perfs_raw in conn_hub.execute(
            "SELECT s.file_path, v.provider, v.title, v.slug, v.performers "
            "FROM synced_files s JOIN videos v ON s.video_id = v.id"
        ).fetchall():
            if provider == "telegram":
                continue
            try:
                performers = json.loads(perfs_raw) if perfs_raw else []
            except (TypeError, ValueError):
                performers = []
            display_title = clean_title(title, performers=performers, provider=provider, slug=slug) or title
            title_map[Path(file_path).name] = (display_title, title)
        return title_map
    finally:
        if conn_hub is not None:
            conn_hub.close()


def _scan_items(session, base_url, headers, media_root, title_map, logger) -> tuple[int, int, int]:
    """Walk every Jellyfin movie page and push titles that differ from the catalog."""
    checked = updated = failed = 0
    start = 0
    try:
        while True:
            items, total = _fetch_items_page(session, base_url, headers, start)
            for item in items:
                if not isinstance(item, dict):
                    failed += 1
                    continue
                expected = _expected_titles(item, media_root, title_map)
                if expected is None:
                    continue
                checked += 1
                expected_name, expected_original = expected
                if item.get("Name") == expected_name and item.get("OriginalTitle") == expected_original:
                    continue
                try:
                    _push_titles(session, base_url, headers, item.get("Id"), item.get("Path"), expected)
                    updated += 1
                except Exception as error:
                    failed += 1
                    logger.warning("Jellyfin title update failed item=%s error=%s", item.get("Id"),
                                   type(error).__name__)
            start += len(items)
            if start >= total:
                break
    except Exception as error:
        failed += 1
        logger.warning("Jellyfin title scan failed error=%s", type(error).__name__)
    return checked, updated, failed


def _fetch_items_page(session, base_url, headers, start: int) -> tuple[list, int]:
    response = session.request(
        "GET", base_url + "/Items", headers=headers, timeout=30,
        params={"Recursive": "true", "IncludeItemTypes": "Movie", "Fields": "Path",
                "StartIndex": start, "Limit": 500},
    )
    response.raise_for_status()
    page = response.json()
    if not isinstance(page, dict) or not isinstance(page.get("Items"), list):
        raise ValueError("invalid Items response")
    items = page["Items"]
    total = page.get("TotalRecordCount")
    if not isinstance(total, int) or total < 0 or not items and start < total:
        raise ValueError("incomplete Items response")
    return items, total


def _expected_titles(item: dict, media_root: Path, title_map: dict):
    """Return the catalog titles for a Jellyfin item, or None when it is out of scope."""
    item_id, path = item.get("Id"), item.get("Path")
    if not item_id or not isinstance(path, str):
        return None
    item_path = Path(path)
    if not item_path.is_relative_to(media_root):
        return None
    filename = item_path.name
    expected = title_map.get(filename)
    if not expected or filename.startswith("telegram ["):
        return None
    return expected


def _push_titles(session, base_url, headers, item_id, path, expected: tuple[str, str]) -> None:
    """Read the full item DTO, set both title fields, and write it back."""
    expected_name, expected_original = expected
    detail_response = session.request(
        "GET", f"{base_url}/Items/{item_id}", headers=headers, timeout=30
    )
    detail_response.raise_for_status()
    dto = detail_response.json()
    if not isinstance(dto, dict) or dto.get("Id") != item_id or dto.get("Path") != path:
        raise ValueError("item changed during title sync")
    dto["Name"] = expected_name
    dto["OriginalTitle"] = expected_original
    update_response = session.request(
        "POST", f"{base_url}/Items/{item_id}", headers=headers,
        json=dto, timeout=30,
    )
    update_response.raise_for_status()
