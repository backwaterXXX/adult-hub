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
    except Exception as error:
        logger.warning("Could not load titles for Jellyfin API sync (%s)", type(error).__name__)
        return {"checked": 0, "updated": 0, "failed": 1}
    finally:
        if conn_hub is not None:
            conn_hub.close()

    checked = updated = failed = 0
    start = 0
    try:
        while True:
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
            for item in items:
                if not isinstance(item, dict):
                    failed += 1
                    continue
                item_id, path = item.get("Id"), item.get("Path")
                if not item_id or not isinstance(path, str):
                    continue
                item_path = Path(path)
                if not item_path.is_relative_to(media_root):
                    continue
                filename = item_path.name
                expected = title_map.get(filename)
                if not expected or filename.startswith("telegram ["):
                    continue
                expected_name, expected_original = expected
                checked += 1
                if item.get("Name") == expected_name and item.get("OriginalTitle") == expected_original:
                    continue
                try:
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
                    updated += 1
                except Exception as error:
                    failed += 1
                    logger.warning("Jellyfin title update failed item=%s error=%s", item_id,
                                   type(error).__name__)
            start += len(items)
            if start >= total:
                break
    except Exception as error:
        failed += 1
        logger.warning("Jellyfin title scan failed error=%s", type(error).__name__)
    logger.info("Jellyfin title sync checked=%d updated=%d failed=%d", checked, updated, failed)
    return {"checked": checked, "updated": updated, "failed": failed}

