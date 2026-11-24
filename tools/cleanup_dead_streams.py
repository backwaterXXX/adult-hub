#!/usr/bin/env python3
"""Report and reconcile the dead-stream bookkeeping.

``dead_streams`` is written by the sync worker and read by the catalog, which
hides those videos. Rows survive their video, and the ``videos.playable`` flag
is not always updated when a stream is blacklisted, so counts drift. This tool
reports the drift and, with ``--apply``, drops the orphan rows and turns the
flag off for the videos that are still listed as playable.

Usage:
    python3 -m tools.cleanup_dead_streams
    python3 -m tools.cleanup_dead_streams --apply
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from common import db

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


def collect_dead_stream_report(conn) -> dict:
    """Return the counts needed to judge whether a cleanup pass is warranted."""
    row = conn.execute(
        "SELECT count(*), "
        "count(*) FILTER (WHERE v.id IS NULL), "
        "count(*) FILTER (WHERE v.id IS NOT NULL AND COALESCE(v.playable, 1) <> 0) "
        "FROM dead_streams d LEFT JOIN videos v ON v.id = d.video_id"
    ).fetchone()
    total, orphans, playable_on = (int(value or 0) for value in row)
    unplayable_without_row = int(conn.execute(
        "SELECT count(*) FROM videos v WHERE COALESCE(v.playable, 1) = 0 "
        "AND NOT EXISTS (SELECT 1 FROM dead_streams d WHERE d.video_id = v.id)"
    ).fetchone()[0] or 0)
    return {
        "dead_streams": total,
        "orphans": orphans,
        "dead_but_playable": playable_on,
        "unplayable_without_row": unplayable_without_row,
    }


def drop_orphan_dead_streams(conn, *, apply: bool = False) -> int:
    """Delete dead entries whose video no longer exists."""
    count = int(conn.execute(
        "SELECT count(*) FROM dead_streams d "
        "WHERE NOT EXISTS (SELECT 1 FROM videos v WHERE v.id = d.video_id)"
    ).fetchone()[0] or 0)
    if apply and count:
        with conn:
            conn.execute(
                "DELETE FROM dead_streams d "
                "WHERE NOT EXISTS (SELECT 1 FROM videos v WHERE v.id = d.video_id)"
            )
    return count


def mark_dead_streams_unplayable(conn, *, apply: bool = False) -> int:
    """Turn ``playable`` off for videos that are denylisted as dead."""
    count = int(conn.execute(
        "SELECT count(*) FROM dead_streams d JOIN videos v ON v.id = d.video_id "
        "WHERE COALESCE(v.playable, 1) <> 0"
    ).fetchone()[0] or 0)
    if apply and count:
        with conn:
            conn.execute(
                "UPDATE videos v SET playable = 0 FROM dead_streams d "
                "WHERE v.id = d.video_id AND COALESCE(v.playable, 1) <> 0"
            )
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the cleanup instead of only reporting")
    args = parser.parse_args(argv)

    _load_all_env()
    db_url = os.environ.get("DATABASE_URL") or os.environ.get("HUB_DB_URL") or DEFAULT_DB_URL
    conn = db.connect(db_url)
    try:
        before = collect_dead_stream_report(conn)
        print(
            "dead_streams={dead_streams} orphans={orphans} "
            "dead_but_playable={dead_but_playable} "
            "unplayable_without_row={unplayable_without_row}".format(**before)
        )
        dropped = drop_orphan_dead_streams(conn, apply=args.apply)
        flagged = mark_dead_streams_unplayable(conn, apply=args.apply)
        verb = "removed" if args.apply else "would remove"
        print(f"{verb} {dropped} orphan row(s); "
              f"{'marked' if args.apply else 'would mark'} {flagged} video(s) unplayable")
        if args.apply:
            print("after: " + ", ".join(f"{k}={v}" for k, v in collect_dead_stream_report(conn).items()))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
