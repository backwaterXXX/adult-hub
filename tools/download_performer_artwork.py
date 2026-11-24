#!/usr/bin/env python3
"""Download provider performer thumbnails into the local media tree.

Rows in ``performer_photos`` usually carry a provider URL. The resolver only
serves regular files inside the media root, so those rows never produce
artwork. This tool materialises them under ``<media>/performers`` and rewrites
the row to the relative path, in bounded, resumable, idempotent batches.

Usage:
    python3 tools/download_performer_artwork.py --limit 200 --dry-run
    python3 tools/download_performer_artwork.py --workers 4
"""

from __future__ import annotations

import argparse
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from common import db
from sync.performer_artwork_download import download_performer_artwork

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_URL = "dbname=adulthub"


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


CATALOGUE_CANDIDATES_SQL = """
SELECT c.name
FROM performer_catalog c
JOIN video_performers vp ON lower(vp.performer_name) = lower(c.name)
LEFT JOIN performer_photos ph ON lower(ph.name) = lower(c.name)
WHERE c.active
  AND c.image_url <> ''
  AND (ph.thumb IS NULL OR ph.thumb = '' OR ph.thumb LIKE 'https://%')
GROUP BY c.name
ORDER BY lower(c.name)
"""


def _candidate_names(conn, *, limit: int | None, refresh: bool, catalog: bool = False) -> list[str]:
    """Return performer names that still need a local thumbnail."""
    names: list[str] = []
    if catalog:
        names.extend(str(row[0]) for row in conn.execute(CATALOGUE_CANDIDATES_SQL).fetchall())
    if not catalog:
        where = "" if refresh else "WHERE thumb LIKE 'https://%'"
        rows = conn.execute(f"SELECT name FROM performer_photos {where} ORDER BY lower(name)").fetchall()
        names.extend(str(row[0]) for row in rows)

    unique = list(dict.fromkeys(name for name in names if str(name or "").strip()))
    return unique[:limit] if limit else unique


class _Worker:
    """One database connection per thread; downloads never share a cursor."""

    def __init__(self, db_url, media_dir: Path):
        self.db_url = db_url
        self.media_dir = media_dir
        self._local = threading.local()

    def _connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = db.connect(self.db_url)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def __call__(self, name: str) -> dict:
        result = download_performer_artwork(self._connection(), self.media_dir, name)
        result["name"] = name
        return result


def _report(totals: dict, result: dict, index: int, total: int) -> None:
    state = str(result.get("local_state", ""))
    totals[state] = totals.get(state, 0) + 1
    if index % 25 == 0 or index == total:
        summary = ", ".join(f"{key}={value}" for key, value in sorted(totals.items()))
        print(f"[{index}/{total}] {summary}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="Maximum rows to process")
    parser.add_argument("--workers", type=int, default=4, help="Concurrent downloads (1-8)")
    parser.add_argument("--dry-run", action="store_true", help="List candidates without downloading")
    parser.add_argument("--refresh", action="store_true", help="Re-check rows that are already local")
    parser.add_argument(
        "--catalog",
        action="store_true",
        help="Use catalogue cover images for listed performers without a photo row",
    )
    args = parser.parse_args(argv)

    _load_all_env()
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("HUB_DB_URL") or DEFAULT_DB_URL
    media_dir = Path(os.environ.get("MEDIA_DIR") or os.environ.get("HUB_MEDIA_DIR") or "/media/adult").resolve()
    workers = max(1, min(int(args.workers or 4), 8))

    conn = db.connect(db_url)
    try:
        names = _candidate_names(conn, limit=args.limit, refresh=args.refresh, catalog=args.catalog)
    finally:
        conn.close()

    print(f"Candidates: {len(names)} (media root {media_dir}, workers {workers})", flush=True)
    if args.dry_run or not names:
        for name in names[:20]:
            print(f"  - {name}")
        return 0

    worker = _Worker(db_url, media_dir)
    totals: dict[str, int] = {}
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(worker, name) for name in names]
        for index, future in enumerate(as_completed(futures), 1):
            try:
                _report(totals, future.result(), index, len(futures))
            except Exception as exc:  # keep the batch alive on a single bad row
                totals["error"] = totals.get("error", 0) + 1
                print(f"row failed: {type(exc).__name__}: {exc}", flush=True)

    elapsed = time.time() - started
    print(f"Done in {elapsed:.1f}s: " + ", ".join(f"{key}={value}" for key, value in sorted(totals.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
