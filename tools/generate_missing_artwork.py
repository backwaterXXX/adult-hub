#!/usr/bin/env python3
"""CLI utility to generate missing video artwork thumbnails from streams."""

import argparse
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

def _load_all_env() -> None:
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())
from common import db
from common.image_utils import extract_video_frame, is_black_image
from common.video_variants import fetch_and_select_best_variant
from sync.jellyfin_db import trigger_library_refresh


def find_missing_artwork_videos(
    conn,
    media_dir: Path,
    studio_pattern: str | None = None,
    title_pattern: str | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Find videos whose artwork file is absent, empty, or black."""
    where_clauses = []
    params: list[str] = []

    if studio_pattern and title_pattern:
        where_clauses.append("(v.studio ILIKE %s OR v.title ILIKE %s)")
        params.extend([f"%{studio_pattern}%", f"%{title_pattern}%"])
    elif studio_pattern:
        where_clauses.append("v.studio ILIKE %s")
        params.append(f"%{studio_pattern}%")
    elif title_pattern:
        where_clauses.append("v.title ILIKE %s")
        params.append(f"%{title_pattern}%")

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    query = f"""
        SELECT v.id, v.title, v.studio, v.duration, sf.file_path
        FROM videos v
        JOIN synced_files sf ON sf.video_id = v.id
        {where_sql}
        ORDER BY v.title
    """
    rows = conn.execute(query, tuple(params) if params else None).fetchall()

    missing = []
    for vid, title, studio, duration, file_path in rows:
        if not file_path:
            continue
        base = file_path[:-5] if file_path.endswith(".strm") else file_path
        dest = media_dir / f"{base}.jpg"

        is_missing = False
        if not dest.exists():
            is_missing = True
        elif dest.stat().st_size == 0:
            is_missing = True
        elif is_black_image(dest):
            is_missing = True

        if is_missing:
            missing.append({
                "video_id": vid,
                "title": title,
                "studio": studio,
                "duration": duration or 0,
                "file_path": file_path,
                "dest_path": dest,
            })
            if limit and len(missing) >= limit:
                break

    return missing


def process_single_video(
    item: dict,
    api_base: str,
    offset_seconds: int = 60,
    timeout: float = 45.0,
) -> dict:
    """Extract a thumbnail frame for a single video."""
    vid = item["video_id"]
    title = item["title"]
    dest = item["dest_path"]
    duration = item["duration"]

    t0 = time.time()
    result = {
        "video_id": vid,
        "title": title,
        "success": False,
        "error": "",
        "size": 0,
        "variant": None,
        "elapsed": 0.0,
    }

    try:
        variant = fetch_and_select_best_variant(api_base, vid, purpose="artwork")
        result["variant"] = str(variant or "original")
        stream_query = f"?variant={quote(str(variant), safe='')}" if variant else ""
        stream_url = f"{api_base}/play/{quote(vid, safe='')}/stream{stream_query}"

        # Adjust offset if video is short
        effective_offset = offset_seconds
        if duration > 0 and effective_offset >= duration:
            effective_offset = max(5, int(duration * 0.2))

        success, error = extract_video_frame(
            stream_url,
            dest,
            offset_seconds=effective_offset,
            timeout=timeout,
        )

        # Retry with different offset if failed or empty
        if not success or not dest.exists() or dest.stat().st_size == 0 or is_black_image(dest):
            alt_offset = 30 if effective_offset != 30 else 90
            success, alt_error = extract_video_frame(
                stream_url,
                dest,
                offset_seconds=alt_offset,
                timeout=timeout,
            )
            if success:
                error = ""
            else:
                error = alt_error or error

        if success and dest.exists() and dest.stat().st_size > 0 and not is_black_image(dest):
            dest.with_suffix(".thumb_fail").unlink(missing_ok=True)
            result["success"] = True
            result["size"] = dest.stat().st_size
        else:
            result["error"] = error or "Empty or black image extracted."

    except Exception as exc:
        result["error"] = str(exc)

    result["elapsed"] = time.time() - t0
    return result


def record_job_entry(conn, result: dict, offset_seconds: int = 60):
    """Record completed or failed artwork generation job in video_duplicate_jobs."""
    now = int(time.time())
    job_id = uuid.uuid4().hex
    state = "done" if result["success"] else "failed"
    payload = json.dumps({
        "video_id": result["video_id"],
        "offset_seconds": offset_seconds,
    })

    if result["success"]:
        res_data = {
            "local_state": "artwork_generated",
            "jellyfin_refresh": "updated",
            "variant": result.get("variant", "original"),
            "events": [
                {"stage": "starting", "updated_at": now},
                {"stage": "generating_artwork", "updated_at": now},
                {"stage": "refreshing_jellyfin", "updated_at": now},
                {"stage": "completed", "updated_at": now},
            ],
            "progress": {"stage": "completed", "updated_at": now},
        }
    else:
        res_data = {
            "events": [
                {"stage": "starting", "updated_at": now},
                {"stage": "generating_artwork", "updated_at": now},
            ],
            "progress": {"stage": "generating_artwork", "updated_at": now},
            "error": f"Failed to generate artwork: {result.get('error', 'Unknown error')}",
        }

    try:
        conn.execute(
            """
            INSERT INTO video_duplicate_jobs (id, action, state, payload, result, created_at, updated_at)
            VALUES (%s, 'artwork_generate', %s, %s, %s, %s, %s)
            """,
            (job_id, state, payload, json.dumps(res_data), now, now),
        )
        conn.commit()
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description="Generate missing artwork for videos from stream.")
    parser.add_argument("--pattern", type=str, default="Dragon", help="Search pattern for title/studio (default: Dragon)")
    parser.add_argument("--studio", type=str, default=None, help="Exact studio pattern match")
    parser.add_argument("--title", type=str, default=None, help="Exact title pattern match")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of videos to process")
    parser.add_argument("--concurrency", type=int, default=2, help="Number of concurrent worker threads (default: 2)")
    parser.add_argument("--offset-seconds", type=int, default=60, help="Frame seek offset in seconds (default: 60)")
    parser.add_argument("--timeout", type=float, default=45.0, help="Timeout in seconds per frame extraction (default: 45.0)")
    parser.add_argument("--dry-run", action="store_true", help="List missing artwork without generating")
    parser.add_argument("--skip-jellyfin", action="store_true", help="Skip Jellyfin library refresh at the end")
    args = parser.parse_args()

    _load_all_env()
    from migrate_db import _normalize_host_url
    db_url = _normalize_host_url(os.getenv("DATABASE_URL"))
    conn = db.connect(db_url)
    media_dir = Path(os.getenv("MEDIA_DIR", "media/adult"))

    studio_pat = args.studio or args.pattern
    title_pat = args.title or args.pattern

    print(f"Scanning for missing artwork matching pattern='{args.pattern}' in {media_dir}...")
    missing_items = find_missing_artwork_videos(
        conn,
        media_dir,
        studio_pattern=studio_pat,
        title_pattern=title_pat,
        limit=args.limit,
    )

    print(f"Found {len(missing_items)} video(s) with missing artwork.")
    if not missing_items:
        print("All matching videos already have valid artwork!")
        return

    if args.dry_run:
        print("\n[DRY RUN] Videos that would be processed:")
        for idx, item in enumerate(missing_items, 1):
            print(f"  {idx}. [{item['studio'] or 'NO_STUDIO'}] {item['title']} -> {item['dest_path'].name}")
        return

    api_base = os.getenv("RESOLVER_LOCAL_API_URL", "http://127.0.0.1:8090/admin")
    print(f"\nStarting artwork generation using {args.concurrency} worker(s)...")
    start_time = time.time()
    succeeded = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {
            executor.submit(
                process_single_video,
                item,
                api_base,
                offset_seconds=args.offset_seconds,
                timeout=args.timeout,
            ): item
            for item in missing_items
        }

        for idx, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            record_job_entry(conn, res, offset_seconds=args.offset_seconds)

            if res["success"]:
                succeeded += 1
                status_str = f"OK ({res['size']} B, {res['elapsed']:.1f}s)"
            else:
                failed += 1
                status_str = f"FAILED: {res['error']} ({res['elapsed']:.1f}s)"

            print(f"[{idx}/{len(missing_items)}] {res['title'][:55]} -> {status_str}")

    elapsed = time.time() - start_time
    print(f"\nCompleted {len(missing_items)} videos in {elapsed:.1f}s.")
    print(f"Success: {succeeded} | Failed: {failed}")

    if not args.skip_jellyfin and succeeded > 0:
        jf_url = (os.getenv("JELLYFIN_URL") or "").rstrip("/")
        jf_key = os.getenv("JELLYFIN_API_KEY")
        if jf_url and jf_key:
            import httpx
            headers = {
                "Authorization": f'MediaBrowser Client="adult-hub", Device="sync", DeviceId="adult-hub-sync", Version="1.0.0", Token="{jf_key}"',
                "X-Emby-Token": jf_key,
            }
            print("\nRefreshing item metadata and images in Jellyfin...")
            try:
                # Find Jellyfin items corresponding to processed files
                resp = httpx.get(
                    f"{jf_url}/Items",
                    headers=headers,
                    params={"IncludeItemTypes": "Movie", "recursive": "true", "fields": "Path"},
                    timeout=30.0,
                )
                if resp.status_code == 200:
                    jf_items = resp.json().get("Items", [])
                    processed_names = {item["dest_path"].with_suffix(".strm").name for item in missing_items}
                    matched_items = [it for it in jf_items if Path(it.get("Path", "")).name in processed_names]
                    print(f"Triggering image refresh for {len(matched_items)} matched Jellyfin item(s)...")
                    with httpx.Client(headers=headers, timeout=15.0) as client:
                        for it in matched_items:
                            client.post(
                                f"{jf_url}/Items/{it['Id']}/Refresh",
                                params={
                                    "MetadataRefreshMode": "Default",
                                    "ImageRefreshMode": "FullRefresh",
                                    "ReplaceAllMetadata": "false",
                                    "ReplaceAllImages": "true",
                                },
                            )
            except Exception as e:
                print(f"Item-level Jellyfin refresh encountered error: {e}")

            print("Triggering global Jellyfin library refresh...")
            refreshed = trigger_library_refresh(jf_url, jf_key, http_client_factory=httpx.Client)
            print("Jellyfin refresh request status:", "Success" if refreshed else "Failed")

    conn.close()


if __name__ == "__main__":
    main()
