#!/usr/bin/env python3
"""Report catalog metadata coverage overall and by Telegram channel."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from common import db


def _load_local_dotenv() -> None:
    """Make a host-side ``make metadata-report`` work without exporting .env."""
    if os.getenv("DATABASE_URL"):
        return
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "DATABASE_URL":
            os.environ.setdefault(key.strip(), value.strip())


def _db_url() -> str:
    _load_local_dotenv()
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return url.replace("@postgres:5432", "@127.0.0.1:5432")


def build_report(conn, limit: int) -> dict:
    overall = conn.execute("""
        SELECT count(*) AS videos,
               count(*) FILTER (WHERE plot IS NULL OR btrim(plot)='') AS no_plot,
               count(*) FILTER (WHERE studio IS NULL OR btrim(studio)='') AS no_studio,
               count(*) FILTER (WHERE performers IS NULL OR performers='[]'::jsonb) AS no_cast,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM admin_audit_items a
                   WHERE a.video_id=videos.id AND a.kind='missing_metadata' AND a.status='open'
               )) AS open_missing_metadata
        FROM videos
    """).fetchone()
    rows = conn.execute("""
        SELECT COALESCE(NULLIF(btrim(channel_name), ''), '(no channel)') AS channel,
               count(*) AS videos,
               count(*) FILTER (WHERE plot IS NULL OR btrim(plot)='') AS no_plot,
               count(*) FILTER (WHERE studio IS NULL OR btrim(studio)='') AS no_studio,
               count(*) FILTER (WHERE performers IS NULL OR performers='[]'::jsonb) AS no_cast,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM admin_audit_items a
                   WHERE a.video_id=videos.id AND a.kind='missing_metadata' AND a.status='open'
               )) AS open_missing_metadata
        FROM videos
        GROUP BY 1
        ORDER BY videos DESC, channel
        LIMIT %s
    """, (max(1, min(int(limit), 500)),)).fetchall()
    fields = ("videos", "no_plot", "no_studio", "no_cast", "open_missing_metadata")
    attempts: dict[str, dict[str, dict[str, int]]] = {}
    for channel, source, outcome, count in conn.execute("""
        SELECT COALESCE(NULLIF(btrim(v.channel_name), ''), '(no channel)') AS channel,
               a.source, a.outcome, count(*)
        FROM external_metadata_attempts a
        JOIN videos v ON v.id=a.entity_key
        WHERE a.entity_type='video'
        GROUP BY 1,2,3
    """).fetchall():
        attempts.setdefault(channel, {}).setdefault(source, {})[outcome] = int(count)
    return {
        "overall": dict(zip(fields, overall)),
        "channels": [
            {"channel": row[0], **dict(zip(fields, row[1:])),
             "attempts": attempts.get(row[0], {})}
            for row in rows
        ],
    }


def _pct(part: int, total: int) -> str:
    return f"{part / total * 100:5.1f}%" if total else "  n/a"


def print_report(report: dict) -> None:
    overall = report["overall"]
    print(f"Videos: {overall['videos']}")
    print(f"  no plot:              {overall['no_plot']:6}  ({_pct(overall['no_plot'], overall['videos'])})")
    print(f"  no studio:            {overall['no_studio']:6}  ({_pct(overall['no_studio'], overall['videos'])})")
    print(f"  no cast:              {overall['no_cast']:6}  ({_pct(overall['no_cast'], overall['videos'])})")
    print(f"  open missing_metadata:{overall['open_missing_metadata']:6}")
    print()
    print(f"{'channel':40} {'videos':>7} {'no plot':>9} {'no studio':>11} {'no cast':>9}")
    for row in report["channels"]:
        print(f"{row['channel'][:40]:40} {row['videos']:7} "
              f"{row['no_plot']:9} {row['no_studio']:11} {row['no_cast']:9}")
        fingerprint = row["attempts"].get("telegram_fingerprint", {})
        title_exact = row["attempts"].get("telegram_caption_title_exact_v1", {})
        if fingerprint or title_exact:
            print("    attempts: "
                  f"fingerprint found={fingerprint.get('found', 0)} "
                  f"no_match={fingerprint.get('no_match', 0)} "
                  f"error={fingerprint.get('error', 0)}; "
                  f"title_exact found={title_exact.get('found', 0)} "
                  f"no_match={title_exact.get('no_match', 0)} "
                  f"error={title_exact.get('error', 0)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=20, help="channels to print")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = ap.parse_args()

    conn = db.connect(_db_url())
    try:
        report = build_report(conn, args.limit)
    finally:
        conn.close()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
