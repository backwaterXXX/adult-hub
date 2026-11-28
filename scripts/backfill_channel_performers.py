#!/usr/bin/env python3
"""Preview or apply title-derived cast for one Telegram channel.

The normal sync backfill runs over every empty-cast video. This script targets a
single channel so a large source can be measured before the whole catalog is
changed. The default is a dry run.
"""

from __future__ import annotations

import argparse
import json
import os

from common import db
from common.performer_titles import parse_title_performers_with_known_map
from common.performers import is_known_non_performer_label
from sync.performer_enrichment import _known_performer_names


def _db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return url.replace("@postgres:5432", "@127.0.0.1:5432")


def _candidate_rows(conn, channel: str, limit: int | None):
    sql = (
        "SELECT id, title FROM videos "
        "WHERE provider='telegram' AND channel_name=%s "
        "AND (performers IS NULL OR performers='[]'::jsonb) "
        "AND COALESCE(title, '') <> '' ORDER BY id"
    )
    params: list = [channel]
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    return conn.execute(sql, params).fetchall()


def _clean_names(names) -> list[str]:
    out = []
    seen = set()
    for raw in names or []:
        name = str(raw or "").strip()
        key = name.casefold()
        if not name or key in seen or is_known_non_performer_label(name):
            continue
        seen.add(key)
        out.append(name)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", required=True, help="exact videos.channel_name")
    ap.add_argument("--limit", type=int, default=None, help="maximum videos to inspect")
    ap.add_argument("--apply", action="store_true", help="write performer lists (default: dry run)")
    ap.add_argument("--show", type=int, default=20, help="sample rows to print")
    args = ap.parse_args()

    conn = db.connect(_db_url())
    try:
        before = conn.execute(
            "SELECT count(*) FROM videos WHERE provider='telegram' AND channel_name=%s "
            "AND (performers IS NULL OR performers='[]'::jsonb)", (args.channel,)
        ).fetchone()[0]
        known = _known_performer_names(conn)
        rows = _candidate_rows(conn, args.channel, args.limit)
        changes = []
        for video_id, title in rows:
            names = _clean_names(parse_title_performers_with_known_map(title or "", known))
            if names:
                changes.append((str(video_id), str(title or ""), names))

        print(f"Channel: {args.channel}")
        print(f"Empty-cast videos before: {before}")
        print(f"Inspected: {len(rows)}")
        print(f"Rows with a cast candidate: {len(changes)}")
        print(f"Names proposed: {sum(len(names) for _id, _title, names in changes)}")
        for video_id, title, names in changes[:max(0, args.show)]:
            print(f"  {video_id} | {title[:90]} -> {', '.join(names)}")

        if args.apply and changes:
            with conn:
                for video_id, _title, names in changes:
                    conn.execute(
                        "UPDATE videos SET performers=%s WHERE id=%s",
                        (json.dumps(names, ensure_ascii=False), video_id),
                    )
            after = conn.execute(
                "SELECT count(*) FROM videos WHERE provider='telegram' AND channel_name=%s "
                "AND (performers IS NULL OR performers='[]'::jsonb)", (args.channel,)
            ).fetchone()[0]
            print(f"Applied {len(changes)} rows; empty-cast videos after: {after}")
        else:
            print("Dry run only; pass --apply to write.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
