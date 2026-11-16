#!/usr/bin/env python3
"""CLI utility to progressively backfill missing video file sizes from Telegram metadata."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Optional, Tuple
from urllib.parse import quote

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common import db  # noqa: E402

logger = logging.getLogger("backfill_file_sizes")
_STOP_REQUESTED = False


def save_db_file_size(ref: str, size: int, db_url: Optional[str] = None) -> bool:
    """Save file_size_bytes to PostgreSQL videos table if currently NULL."""
    clean_ref = str(ref or "").strip()
    if not clean_ref or not isinstance(size, int) or size <= 0 or ":variant:" in clean_ref:
        return False
    unique_id = clean_ref.rsplit(":", 1)[-1].strip()
    candidate_ids = [
        clean_ref if clean_ref.startswith("telegram:") else f"telegram:{clean_ref}",
    ]
    if unique_id and f"telegram:{unique_id}" not in candidate_ids:
        candidate_ids.append(f"telegram:{unique_id}")

    try:
        with db.connection(db_url, connect_timeout=2) as conn:
            with conn:
                for candidate_id in candidate_ids:
                    cur = conn.execute(
                        "UPDATE videos SET file_size_bytes = %s "
                        "WHERE id = %s AND file_size_bytes IS NULL",
                        (int(size), candidate_id),
                    )
                    if getattr(cur, "rowcount", 0) > 0:
                        return True
    except Exception as exc:
        logger.debug("Database file_size save failed for %s: %s", ref, exc)
    return False


def _sig_handler(signum, frame):
    global _STOP_REQUESTED
    if _STOP_REQUESTED:
        # A second signal means the operator wants out now, not after the current item.
        raise KeyboardInterrupt
    _STOP_REQUESTED = True
    logger.info("Interrupt received, finishing current item and exiting (signal again to abort now)...")


def _default_resolver_url() -> str:
    return (
        os.getenv("RESOLVER_INTERNAL_URL")
        or os.getenv("RESOLVER_URL")
        or "http://127.0.0.1:8090"
    ).rstrip("/")


def _fetch_missing_size_video_ids(conn: Any, limit: Optional[int]) -> list[str]:
    sql = (
        "SELECT id FROM videos "
        "WHERE provider = 'telegram' AND file_size_bytes IS NULL "
        "ORDER BY first_seen DESC"
    )
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"
    return [r[0] for r in conn.execute(sql).fetchall()]


def _process_single_video(http_client: Any, base_url: str, vid: str) -> Optional[bool]:
    """Fetch variants for one video and save its file size.

    Returns True when a size was saved, None when the resolver has no size for the
    video (not an error: the video simply has no reachable variant), and False when
    the request itself failed.
    """
    raw_ref = vid[len("telegram:") :] if vid.startswith("telegram:") else vid
    url = f"{base_url}/admin/play/{quote(vid, safe=':-_')}/variants"

    try:
        resp = http_client.get(url)
        if resp.status_code == 200:
            payload = resp.json()
            variants = payload.get("variants") or []
            orig = next((v for v in variants if v.get("is_original")), None)
            size = orig.get("file_size") if orig else None
            if isinstance(size, int) and size > 0:
                save_db_file_size(raw_ref, size)
                return True
            return None
        if resp.status_code in (404, 403):
            logger.debug("Variants not available for %s (%d)", vid, resp.status_code)
            return None
        logger.warning("Variants query failed for %s (status %d)", vid, resp.status_code)
        return False
    except Exception as exc:
        logger.debug("Variants request error for %s: %s", vid, exc)
        return False


def backfill_telegram_file_sizes(
    conn: Any,
    *,
    resolver_url: Optional[str] = None,
    limit: Optional[int] = 500,
    delay_sec: float = 0.8,
    client: Optional[httpx.Client] = None,
) -> Tuple[int, int, int]:
    """Progressively query resolver variants to populate missing videos.file_size_bytes."""
    base_url = (resolver_url or _default_resolver_url()).rstrip("/")
    video_ids = _fetch_missing_size_video_ids(conn, limit)
    total_target = len(video_ids)
    if not total_target:
        logger.info("No Telegram videos with missing file size found.")
        return 0, 0, 0

    logger.info(
        "Starting file size backfill for %d videos (delay: %.2fs, resolver: %s)...",
        total_target,
        delay_sec,
        base_url,
    )

    checked = 0
    updated = 0
    failed = 0
    unavailable = 0
    http_client = client or httpx.Client(timeout=10.0)

    try:
        for vid in video_ids:
            if _STOP_REQUESTED:
                logger.info("Backfill stopped early upon request.")
                break

            checked += 1
            outcome = _process_single_video(http_client, base_url, vid)
            if outcome is True:
                updated += 1
            elif outcome is None:
                unavailable += 1
            else:
                failed += 1

            if checked % 25 == 0 or checked == total_target:
                logger.info(
                    "Progress: %d/%d checked, %d updated, %d unavailable, %d failed",
                    checked,
                    total_target,
                    updated,
                    unavailable,
                    failed,
                )

            if delay_sec > 0 and not _STOP_REQUESTED:
                time.sleep(delay_sec)
    finally:
        if client is None:
            http_client.close()

    logger.info(
        "File size backfill finished: %d checked, %d updated, %d unavailable, %d failed.",
        checked,
        updated,
        unavailable,
        failed,
    )
    return checked, updated, failed


def main() -> int:
    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    parser = argparse.ArgumentParser(description="Backfill missing Telegram video file sizes")
    parser.add_argument("--limit", type=int, default=500, help="Max videos to process (0 for unlimited)")
    parser.add_argument("--delay", type=float, default=0.8, help="Delay between requests in seconds")
    parser.add_argument("--resolver-url", type=str, default=None, help="Base resolver URL")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db_url = os.getenv("DATABASE_URL")
    limit = args.limit if args.limit > 0 else None

    with db.connection(db_url) as conn:
        checked, updated, failed = backfill_telegram_file_sizes(
            conn,
            resolver_url=args.resolver_url,
            limit=limit,
            delay_sec=args.delay,
        )

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
