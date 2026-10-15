"""Link health monitor for Adult-Hub providers.

Samples random videos per provider from the hub database, resolves each via
the resolver `/admin/play/{id}/stream` endpoint (no redirect following) and then
fetches the resulting playlist URL. Reports per-provider success rates so
upstream breakage (embed changes, new anti-leech, dead CDN) is caught before
it matters at playback time.

Usage (inside the sync container, resolver reachable as http://resolver:8090):
    docker compose exec sync python linkcheck.py
    docker compose exec sync python linkcheck.py --sample 5 --provider telegram

Exit code 0 = all providers healthy, 1 = at least one provider fully broken
or failure rate above --max-fail-rate.
"""

import argparse
import os
from common import db
import sys
import time
from pathlib import Path
from typing import List, Tuple
from common.logging_setup import configure_logging

try:
    import httpx
except ImportError:
    httpx = None

DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None
RESOLVER_API_URL = os.getenv("RESOLVER_API_URL", "http://localhost:8090")
LINKCHECK_LOG_PATH = Path(os.getenv("LINKCHECK_LOG_PATH", "/data/linkcheck.log"))
LOGGER = configure_logging("sync.linkcheck")


def report(message: str) -> None:
    """Write each result to the terminal and the log shown in the admin panel."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    LOGGER.info("%s", message)
    try:
        LINKCHECK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LINKCHECK_LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")
    except OSError as e:
        LOGGER.error("Could not write linkcheck log: %s", e)

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


# Telegram-only: neutral default ("redirect") applies to unknown providers.
_STREAM_MODE_PER_PROVIDER = {
    "telegram": os.getenv("STREAM_MODE_TELEGRAM", "proxy"),
}


def stream_mode_for_provider(video_id: str) -> str:
    provider = video_id.partition(":")[0]
    return _STREAM_MODE_PER_PROVIDER.get(provider, "redirect")


def sample_ids(conn: db.Connection, provider: str, n: int) -> List[str]:
    cur = conn.execute(
        "SELECT id FROM videos WHERE provider = %s ORDER BY RANDOM() LIMIT %s",
        (provider, n),
    )
    return [r[0] for r in cur.fetchall()]


def check_one(client: "httpx.Client", video_id: str) -> Tuple[bool, str]:
    """Returns (ok, detail)."""
    mode = stream_mode_for_provider(video_id)
    try:
        headers = dict(BROWSER_HEADERS)
        headers["Range"] = "bytes=0-8192"
        with client.stream(
            "GET",
            f"{RESOLVER_API_URL.rstrip('/')}/admin/play/{video_id}/stream?mode={mode}",
            headers=headers,
            follow_redirects=False,
            timeout=35.0,
        ) as r:
            status_code = r.status_code
            resp_headers = dict(r.headers)
            chunk = b""
            if status_code in (200, 206):
                for c in r.iter_bytes(8192):
                    chunk = c
                    break
            elif status_code != 302:
                for c in r.iter_bytes(1024):
                    chunk = c
                    break
    except Exception as e:
        return False, f"resolver error: {e}"

    if status_code in (200, 206):
        ctype = resp_headers.get("content-type", "")
        text_preview = chunk.decode("utf-8", errors="ignore").lstrip()
        if "mpegurl" in ctype or text_preview.startswith("#EXTM3U"):
            return True, f"proxied playlist OK ({len(chunk)}B)"
        if b"ftyp" in chunk[:32] or "video/" in ctype:
            return True, f"proxied stream OK ({len(chunk)}B, {ctype or 'video'})"
        return True, f"direct stream OK ({len(chunk)}B)"

    if status_code == 302 and resp_headers.get("location"):
        playlist_url = resp_headers["location"]
    else:
        body = chunk.decode("utf-8", errors="ignore")[:120].replace("\n", " ")
        return False, f"resolve HTTP {status_code}: {body}"

    try:
        headers = dict(BROWSER_HEADERS)
        headers["Range"] = "bytes=0-8192"
        with client.stream(
            "GET",
            playlist_url,
            headers=headers,
            follow_redirects=True,
            timeout=20.0,
        ) as pr:
            pr_status = pr.status_code
            pr_headers = dict(pr.headers)
            pr_chunk = b""
            if pr_status in (200, 206):
                for c in pr.iter_bytes(8192):
                    pr_chunk = c
                    break
            else:
                for c in pr.iter_bytes(1024):
                    pr_chunk = c
                    break
    except Exception as e:
        return False, f"playlist fetch error: {e}"

    if pr_status in (200, 206):
        ctype = pr_headers.get("content-type", "")
        text_preview = pr_chunk.decode("utf-8", errors="ignore").lstrip()
        if "mpegurl" in ctype or text_preview.startswith("#EXTM3U"):
            return True, f"playlist OK ({len(pr_chunk)}B)"
        return True, f"stream OK ({len(pr_chunk)}B, {ctype or 'no ctype'})"
    return False, f"playlist HTTP {pr_status}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Adult-Hub link health check")
    ap.add_argument("--sample", type=int,
                    default=int(os.getenv("LINKCHECK_SAMPLE_PER_PROVIDER", "3")))
    ap.add_argument("--provider", action="append", default=None)
    ap.add_argument("--max-fail-rate", type=float,
                    default=float(os.getenv("LINKCHECK_MAX_FAIL_RATE", "0.5")))
    args = ap.parse_args()

    if httpx is None:
        LOGGER.error("httpx is required")
        return 2
    conn = db.connect(DB_URL)
    providers = [r[0] for r in conn.execute(
        "SELECT DISTINCT provider FROM videos ORDER BY provider").fetchall()]
    if args.provider:
        providers = [p for p in providers if p in args.provider]
    if not providers:
        LOGGER.error("No providers found in DB")
        return 2

    try:
        LINKCHECK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LINKCHECK_LOG_PATH.write_text("", encoding="utf-8")
    except OSError as e:
        LOGGER.error("Could not initialize linkcheck log: %s", e)

    overall_fail = False
    with httpx.Client(follow_redirects=False,
                      timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        for provider in providers:
            ids = sample_ids(conn, provider, args.sample)
            if not ids:
                report(f"[{provider}] no videos in DB, skipping")
                continue
            ok = 0
            for vid in ids:
                good, detail = check_one(client, vid)
                ok += good
                report(f"[{provider}] {'OK  ' if good else 'FAIL'} {vid}: {detail}")
                time.sleep(1.0)
            fail_rate = 1.0 - ok / len(ids)
            status = "HEALTHY" if fail_rate <= args.max_fail_rate and ok > 0 else "BROKEN"
            report(f"[{provider}] {ok}/{len(ids)} ok -> {status}")
            if status == "BROKEN":
                overall_fail = True
    conn.close()
    return 1 if overall_fail else 0


if __name__ == "__main__":
    sys.exit(main())
