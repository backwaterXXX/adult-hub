#!/usr/bin/env python3
"""CLI utility to generate missing artwork specifically for items appearing in Jellyfin Latest shelf."""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.generate_missing_artwork import (  # noqa: E402
    _load_all_env,
    process_single_video,
    record_job_entry,
    update_job_jellyfin_result,
)
from common import db  # noqa: E402
from common.image_utils import is_black_image  # noqa: E402
from sync.jellyfin_db import trigger_library_refresh  # noqa: E402
from migrate_db import _normalize_host_url  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate missing artwork for Jellyfin Latest shelf items.")
    parser.add_argument("--limit-shelf", type=int, default=100, help="Number of items to fetch from Jellyfin Latest (default: 100)")
    parser.add_argument("--concurrency", type=int, default=3, help="Concurrent worker threads (default: 3)")
    parser.add_argument("--offset-seconds", type=int, default=60, help="Frame seek offset (default: 60)")
    parser.add_argument("--timeout", type=float, default=45.0, help="FFmpeg timeout per item (default: 45.0)")
    return parser


def _read_settings():
    jf_url = (os.getenv("JELLYFIN_URL") or "").rstrip("/")
    jf_token = os.getenv("JELLYFIN_API_KEY")
    user_id = os.getenv("JELLYFIN_USER_ID")
    parent_id = os.getenv("JELLYFIN_ADULT_LIBRARY_ID")
    api_base = os.getenv("RESOLVER_LOCAL_API_URL", "http://127.0.0.1:8090/admin")
    media_dir = Path(os.getenv("MEDIA_DIR", "./media/adult"))
    return jf_url, jf_token, user_id, parent_id, api_base, media_dir


def _exit_if_config_missing(jf_url, jf_token, user_id, parent_id) -> None:
    missing_config = [key for key, value in (
        ("JELLYFIN_URL", jf_url), ("JELLYFIN_API_KEY", jf_token),
        ("JELLYFIN_USER_ID", user_id), ("JELLYFIN_ADULT_LIBRARY_ID", parent_id),
    ) if not value]
    if missing_config:
        print("Error: required Jellyfin settings are missing: " + ", ".join(missing_config))
        sys.exit(1)


def _fetch_latest_shelf(jf_url, jf_token, user_id, parent_id, limit_shelf):
    shelf_url = f"{jf_url}/Users/{user_id}/Items/Latest?Limit={limit_shelf}&ParentId={parent_id}&Fields=Path"
    headers = {"Authorization": f'MediaBrowser Token="{jf_token}"'}
    req = urllib.request.Request(shelf_url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def _needs_artwork(it: dict, dest: Path) -> bool:
    if "Primary" not in it.get("ImageTags", {}):
        return True
    return not dest.exists() or dest.stat().st_size == 0 or is_black_image(dest)


def _item_to_process(jf_id, it: dict, p: Path, dest: Path, conn):
    content = p.read_text().strip()
    m = re.search(r"/admin/play/([^/?]+)", content)
    if not m:
        print(f"Warning: could not parse video_id from {p}")
        return None

    vid = urllib.parse.unquote(m.group(1))
    row = conn.execute("SELECT duration, title, studio FROM videos WHERE id = %s", (vid,)).fetchone()
    dur = row[0] if row and row[0] else 0
    title = row[1] if row and row[1] else it.get("Name", "")
    studio = row[2] if row and row[2] else ""

    return {
        "jf_id": jf_id,
        "video_id": vid,
        "title": title,
        "studio": studio,
        "duration": dur,
        "file_path": p.name,
        "dest_path": dest,
    }


def _collect_items_needing_art(jf_items, media_dir: Path, conn) -> list[dict]:
    items_to_process = []
    for it in jf_items:
        jf_id = it.get("Id")
        jf_path = it.get("Path", "")
        if not jf_path:
            continue

        p = media_dir / Path(jf_path).name
        dest = p.with_suffix(".jpg")

        # Check if artwork is missing or invalid
        if not _needs_artwork(it, dest):
            continue

        if not p.exists():
            print(f"Warning: STRM file missing on disk: {p}")
            continue

        item = _item_to_process(jf_id, it, p, dest, conn)
        if item is not None:
            items_to_process.append(item)
    return items_to_process


def _generate_frames(items_to_process: list[dict], conn, api_base: str, args):
    print(f"\nStarting generation using {args.concurrency} concurrent worker(s)...")
    start_time = time.time()
    succeeded_items = []
    failed_count = 0
    jobs_by_video: dict[str, list[str]] = {}

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        future_to_item = {
            executor.submit(
                process_single_video,
                item,
                api_base,
                offset_seconds=args.offset_seconds,
                timeout=args.timeout,
            ): item
            for item in items_to_process
        }

        for idx, fut in enumerate(as_completed(future_to_item), 1):
            item = future_to_item[fut]
            res = fut.result()
            job_id = record_job_entry(conn, res, offset_seconds=args.offset_seconds)

            if res["success"]:
                succeeded_items.append(item)
                jobs_by_video.setdefault(str(res["video_id"]), []).append(job_id)
                status_str = f"OK ({res['size']} B, {res['elapsed']:.1f}s)"
            else:
                failed_count += 1
                status_str = f"FAILED: {res['error']} ({res['elapsed']:.1f}s)"

            print(f"[{idx}/{len(items_to_process)}] {item['title'][:50]} -> {status_str}")

    elapsed = time.time() - start_time
    print(f"\nFinished frame extraction in {elapsed:.1f}s. Success: {len(succeeded_items)} | Failed: {failed_count}")
    return succeeded_items, jobs_by_video


def _refresh_items_in_jellyfin(succeeded_items, jf_url: str, jf_token: str) -> dict[str, list[str]]:
    import httpx
    jf_headers = {
        "Authorization": f'MediaBrowser Client="adult-hub", Device="sync", DeviceId="adult-hub-sync", Version="1.0.0", Token="{jf_token}"',
        "X-Emby-Token": jf_token,
    }
    refresh_results: dict[str, list[str]] = {}
    try:
        with httpx.Client(headers=jf_headers, timeout=15.0) as client:
            for item in succeeded_items:
                try:
                    response = client.post(
                        f"{jf_url}/Items/{item['jf_id']}/Refresh",
                        params={
                            "MetadataRefreshMode": "Default",
                            "ImageRefreshMode": "FullRefresh",
                            "ReplaceAllMetadata": "false",
                            "ReplaceAllImages": "true",
                        },
                    )
                    response.raise_for_status()
                    refresh_results.setdefault(str(item["video_id"]), []).append("updated")
                except Exception as err:
                    refresh_results.setdefault(str(item["video_id"]), []).append("failed")
                    print(f"Error refreshing item {item['jf_id']}: {type(err).__name__}")
    except Exception as err:
        print(f"Item refresh client failed: {type(err).__name__}")
        for item in succeeded_items:
            refresh_results.setdefault(str(item["video_id"]), []).append("failed")
    return refresh_results


def _trigger_global_refresh(jf_url: str, jf_token: str) -> str:
    import httpx
    print("Triggering global Jellyfin library scan...")
    try:
        refreshed = trigger_library_refresh(jf_url, jf_token, http_client_factory=httpx.Client)
        library_status = "updated" if refreshed else "failed"
    except Exception as err:
        library_status = "failed"
        print(f"Global refresh failed: {type(err).__name__}")
    print("Global refresh triggered:", "Success" if library_status == "updated" else "Failed")
    return library_status


def _refresh_jellyfin(succeeded_items, jf_url: str, jf_token: str, jobs_by_video, conn) -> None:
    print("\nRefreshing item metadata and images in Jellyfin...")
    refresh_results = _refresh_items_in_jellyfin(succeeded_items, jf_url, jf_token)
    library_status = _trigger_global_refresh(jf_url, jf_token)
    for video_id, job_ids in jobs_by_video.items():
        outcomes = refresh_results.get(video_id, [])
        status = (
            "failed" if "failed" in outcomes else
            "unavailable" if not outcomes or "unavailable" in outcomes else "updated"
        )
        for job_id in job_ids:
            update_job_jellyfin_result(conn, job_id, item_status=status, library_status=library_status)


def main():
    args = _build_parser().parse_args()

    _load_all_env()

    jf_url, jf_token, user_id, parent_id, api_base, media_dir = _read_settings()
    _exit_if_config_missing(jf_url, jf_token, user_id, parent_id)

    print(f"Connecting to Jellyfin at {jf_url}...")
    jf_items = _fetch_latest_shelf(jf_url, jf_token, user_id, parent_id, args.limit_shelf)

    print(f"Fetched {len(jf_items)} items from Jellyfin Latest shelf.")

    db_url = _normalize_host_url(os.getenv("DATABASE_URL"))
    conn = db.connect(db_url)

    items_to_process = _collect_items_needing_art(jf_items, media_dir, conn)

    print(f"Found {len(items_to_process)} item(s) on Latest shelf needing artwork.")
    if not items_to_process:
        print("All items in Latest shelf have valid artwork!")
        conn.close()
        return

    succeeded_items, jobs_by_video = _generate_frames(items_to_process, conn, api_base, args)

    if succeeded_items:
        _refresh_jellyfin(succeeded_items, jf_url, jf_token, jobs_by_video, conn)

    conn.close()


if __name__ == "__main__":
    main()
