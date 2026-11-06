"""Reconcile managed Jellyfin collections, shared by resolver and sync worker."""
import json
from pathlib import Path

from common.db import connect


def _jellyfin_headers(api_key: str) -> dict:
    return {
        "Authorization": f'MediaBrowser Client="adult-hub", Device="admin", DeviceId="adult-hub-admin", Version="1.0.0", Token="{api_key}"',
        "X-Emby-Authorization": f'MediaBrowser Client="adult-hub", Device="admin", DeviceId="adult-hub-admin", Version="1.0.0", Token="{api_key}"',
        "X-Emby-Token": api_key,
    }


def _chunks(values: set[str], size: int = 100) -> list[list[str]]:
    ordered = sorted(values)
    return [ordered[index:index + size] for index in range(0, len(ordered), size)]


async def _jellyfin_items(client, url: str, params: dict, headers: dict, *, max_items: int = 50000) -> list[dict]:
    items = []
    start = 0
    while start < max_items:
        query = {**params, "startIndex": start, "limit": min(500, max_items - start)}
        response = await client.get(url, params=query, headers=headers)
        response.raise_for_status()
        payload = response.json()
        page = payload.get("Items", [])
        items.extend(page)
        start += len(page)
        if not page or start >= int(payload.get("TotalRecordCount", start)):
            break
    return items


async def refresh_managed_collections(client, base_url: str, api_key: str, db_url=None) -> dict:
    """Create and reconcile Adult Hub collections from its synced metadata."""
    records = _catalog_records(db_url)
    headers = _jellyfin_headers(api_key)
    items_by_file = await _jellyfin_file_index(client, base_url, headers)
    videos = _catalog_videos(records, items_by_file)
    desired = await _desired_collections(client, base_url, headers, videos)

    response = await client.get(
        f"{base_url}/Items", params={"recursive": "true", "includeItemTypes": "BoxSet", "limit": 1000}, headers=headers,
    )
    response.raise_for_status()
    existing = {item.get("Name"): item.get("Id") for item in response.json().get("Items", []) if item.get("Name", "").startswith("Adult Hub · ")}
    changes = []
    # Keep the membership of previously managed groups reconciled even when
    # their desired set drops below the creation threshold or their metadata
    # label changes. Empty collections are retained; only stale membership is
    # removed, and no media item or operator-owned collection is deleted.
    for name in sorted(set(desired) | set(existing)):
        change = await _reconcile_collection(
            client, base_url, headers, name, desired.get(name, []), existing.get(name))
        if change is not None:
            changes.append(change)
    return {"collections": changes, "matched_videos": len(videos)}


def _catalog_records(db_url) -> list:
    conn = connect(db_url)
    try:
        return conn.execute("""SELECT v.id, v.title, v.studio, v.performers, v.tags,
                                       v.first_seen, s.file_path
                                FROM videos v LEFT JOIN synced_files s ON s.video_id = v.id""").fetchall()
    finally:
        conn.close()


async def _jellyfin_file_index(client, base_url: str, headers: dict) -> dict[str, set[str]]:
    # The catalog stores the library-relative STRM name, while Jellyfin returns
    # an absolute path. Match on the name only when it identifies exactly one
    # Jellyfin item; basename collisions across directories are ambiguous and
    # must not add the wrong video to a managed collection.
    items_by_file: dict[str, set[str]] = {}
    jellyfin_items = await _jellyfin_items(
        client, f"{base_url}/Items",
        {"recursive": "true", "includeItemTypes": "Movie,Video", "fields": "Path"}, headers,
    )
    for item in jellyfin_items:
        path = item.get("Path")
        item_id = item.get("Id")
        if path and item_id:
            items_by_file.setdefault(Path(path).name, set()).add(item_id)
    return items_by_file


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        value = []
    return value if isinstance(value, list) else []


def _catalog_videos(records, items_by_file: dict[str, set[str]]) -> list[dict]:
    videos = []
    for video_id, title, studio, performers_raw, tags_raw, first_seen, file_path in records:
        matching_ids = items_by_file.get(Path(file_path).name, set()) if file_path else set()
        jellyfin_id = next(iter(matching_ids)) if len(matching_ids) == 1 else None
        if not jellyfin_id:
            continue
        videos.append({
            "id": jellyfin_id, "title": title or video_id, "studio": (studio or "").strip(),
            "performers": _json_list(performers_raw), "tags": _json_list(tags_raw),
            "first_seen": first_seen or 0,
        })
    return videos


async def _desired_collections(client, base_url: str, headers: dict, videos: list[dict]) -> dict[str, list[str]]:
    desired: dict[str, list[str]] = {
        "Adult Hub · Recently Added": [v["id"] for v in sorted(videos, key=lambda v: v["first_seen"], reverse=True)[:60]],
    }
    favorite_items = await _jellyfin_items(
        client, f"{base_url}/Items",
        {"recursive": "true", "includeItemTypes": "Movie,Video", "isFavorite": "true", "enableUserData": "true"}, headers,
    )
    favorite_ids = {item.get("Id") for item in favorite_items if item.get("Id")}
    desired["Adult Hub · Favorites"] = sorted(favorite_ids & {video["id"] for video in videos})
    for field, label in (("studio", "Studio"), ("performers", "Performer"), ("tags", "Tag")):
        _add_attribute_collections(desired, videos, field, label)
    return desired


def _add_attribute_collections(desired: dict[str, list[str]], videos: list[dict], field: str, label: str) -> None:
    """Add up to 10 studio, performer, or tag collections with at least 3 videos."""
    groups: dict[str, set[str]] = {}
    for video in videos:
        values = video[field] if isinstance(video[field], list) else [video[field]]
        for value in values:
            name = str(value or "").strip()
            if name:
                groups.setdefault(name, set()).add(video["id"])
    for name, ids in sorted(groups.items(), key=lambda pair: (-len(pair[1]), pair[0].casefold())):
        if len(ids) >= 3 and len([n for n in desired if n.startswith(f"Adult Hub · {label} · ")]) < 10:
            desired[f"Adult Hub · {label} · {name}"] = sorted(ids)


async def _reconcile_collection(client, base_url: str, headers: dict, name: str, wanted_ids, collection_id):
    """Bring one managed collection in line with its desired membership.

    Returns the change record, or None when a collection is not needed yet.
    """
    wanted = set(wanted_ids)
    if not collection_id:
        if not wanted:
            return None
        await _create_collection(client, base_url, headers, name, wanted)
        return {"name": name, "items": len(wanted), "created": True}
    await _sync_membership(client, base_url, headers, collection_id, wanted)
    return {"name": name, "items": len(wanted), "created": False}


async def _create_collection(client, base_url: str, headers: dict, name: str, wanted: set[str]) -> None:
    batches = _chunks(wanted)
    response = await client.post(f"{base_url}/Collections", params={"name": name, "ids": ",".join(batches[0])}, headers=headers)
    response.raise_for_status()
    created_id = response.json().get("Id")
    if len(batches) > 1 and not created_id:
        raise RuntimeError("Jellyfin did not return the collection identifier")
    if created_id:
        for batch in batches[1:]:
            response = await client.post(
                f"{base_url}/Collections/{created_id}/Items", params={"ids": ",".join(batch)}, headers=headers,
            )
            response.raise_for_status()


async def _sync_membership(client, base_url: str, headers: dict, collection_id, wanted: set[str]) -> None:
    current_items = await _jellyfin_items(
        client, f"{base_url}/Items", {"parentId": collection_id, "recursive": "true"}, headers,
    )
    current = {item.get("Id") for item in current_items if item.get("Id")}
    add_ids, remove_ids = wanted - current, current - wanted
    for batch in _chunks(add_ids):
        response = await client.post(
            f"{base_url}/Collections/{collection_id}/Items", params={"ids": ",".join(batch)}, headers=headers,
        )
        response.raise_for_status()
    for batch in _chunks(remove_ids):
        response = await client.delete(
            f"{base_url}/Collections/{collection_id}/Items", params={"ids": ",".join(batch)}, headers=headers,
        )
        response.raise_for_status()
