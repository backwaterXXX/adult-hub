"""Link health monitor for Adult-Hub providers.

Samples random videos per provider from the hub database, resolves each via
the resolver `/admin/play/{id}/stream` endpoint (no redirect following) and then
fetches the resulting playlist URL. Reports per-provider success rates so
upstream breakage (embed changes, new anti-leech, dead CDN) is caught before
it matters at playback time.

Usage (inside the sync container, resolver reachable as http://resolver:8090):
    docker compose exec sync python -m sync.linkcheck
    docker compose exec sync python -m sync.linkcheck --sample 5 --provider telegram

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
import logging
from common.logging_setup import configure_logging

logger = logging.getLogger(__name__)


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
        status_code, resp_headers, chunk = _sample_response(
            client,
            f"{RESOLVER_API_URL.rstrip('/')}/admin/play/{video_id}/stream?mode={mode}",
            follow_redirects=False,
            timeout=35.0,
            sample_size=_resolver_sample_size,
        )
    except Exception as e:
        logger.debug("Ignored exception in check_one", exc_info=True)
        return False, f"resolver error: {e}"

    if status_code in (200, 206):
        return True, _describe_direct_stream(resp_headers, chunk)

    if status_code == 302 and resp_headers.get("location"):
        playlist_url = resp_headers["location"]
    else:
        body = chunk.decode("utf-8", errors="ignore")[:120].replace("\n", " ")
        return False, f"resolve HTTP {status_code}: {body}"

    try:
        pr_status, pr_headers, pr_chunk = _sample_response(
            client,
            playlist_url,
            follow_redirects=True,
            timeout=20.0,
            sample_size=_playlist_sample_size,
        )
    except Exception as e:
        logger.debug("Ignored exception in check_one", exc_info=True)
        return False, f"playlist fetch error: {e}"

    if pr_status in (200, 206):
        return True, _describe_playlist(pr_headers, pr_chunk)
    return False, f"playlist HTTP {pr_status}"


def _sample_response(client: "httpx.Client", url: str, *, follow_redirects: bool,
                     timeout: float, sample_size) -> Tuple[int, dict, bytes]:
    """Request the first bytes of a URL; ``sample_size(status)`` picks how many to read."""
    headers = dict(BROWSER_HEADERS)
    headers["Range"] = "bytes=0-8192"
    with client.stream(
        "GET",
        url,
        headers=headers,
        follow_redirects=follow_redirects,
        timeout=timeout,
    ) as r:
        status_code = r.status_code
        resp_headers = dict(r.headers)
        chunk = b""
        size = sample_size(status_code)
        if size:
            for c in r.iter_bytes(size):
                chunk = c
                break
    return status_code, resp_headers, chunk


def _resolver_sample_size(status_code: int):
    if status_code in (200, 206):
        return 8192
    if status_code != 302:
        return 1024
    return None


def _playlist_sample_size(status_code: int) -> int:
    return 8192 if status_code in (200, 206) else 1024


def _describe_direct_stream(resp_headers: dict, chunk: bytes) -> str:
    ctype = resp_headers.get("content-type", "")
    text_preview = chunk.decode("utf-8", errors="ignore").lstrip()
    if "mpegurl" in ctype or text_preview.startswith("#EXTM3U"):
        return f"proxied playlist OK ({len(chunk)}B)"
    if b"ftyp" in chunk[:32] or "video/" in ctype:
        return f"proxied stream OK ({len(chunk)}B, {ctype or 'video'})"
    return f"direct stream OK ({len(chunk)}B)"


def _describe_playlist(pr_headers: dict, pr_chunk: bytes) -> str:
    ctype = pr_headers.get("content-type", "")
    text_preview = pr_chunk.decode("utf-8", errors="ignore").lstrip()
    if "mpegurl" in ctype or text_preview.startswith("#EXTM3U"):
        return f"playlist OK ({len(pr_chunk)}B)"
    return f"stream OK ({len(pr_chunk)}B, {ctype or 'no ctype'})"


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
