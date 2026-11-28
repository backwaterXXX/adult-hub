#!/usr/bin/env python3
"""Remove known studios, channels, and series labels from performer data.

Labels such as ``Naughty`` or ``Boy Fun`` come from Telegram channel names or
studio prefixes. Once a label is stored in ``videos.performers`` it is counted
as a performer with thousands of videos, so it dominates every "most films"
list even though no such actor exists.

The same exact-label set used by ingestion is applied here, and the
materialised ``performer_stats`` table is rebuilt afterwards. Run with
``--apply`` to write; the default is a dry run.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

from common import db
from common.json_helpers import json_list_or_empty
from common.performer_cleanup_support import create_db_backup
from common.performers import is_known_non_performer_label

BACKUP_DIR = BASE_DIR / "data" / "backups"
VIDEO_TABLES = ("performer_bios", "performer_photos")


def _without_labels(names) -> list:
    return [
        str(name).strip() for name in names
        if str(name).strip() and not is_known_non_performer_label(name)
    ]


def _label_rows(conn, table: str, column: str) -> list:
    rows = conn.execute(f"SELECT {column} FROM {table}").fetchall()
    return [row[0] for row in rows if row[0] and is_known_non_performer_label(row[0])]


def _clean_videos(conn, apply: bool) -> int:
    rows = conn.execute(
        "SELECT id, performers FROM videos WHERE performers <> '[]'::jsonb"
    ).fetchall()
    changes = []
    for video_id, raw in rows:
        old = json_list_or_empty(raw)
        new = _without_labels(old)
        if new != old:
            changes.append((video_id, old, new))
    labels = sorted({name for _vid, old, new in changes for name in old if name not in new})
    print(f"Videos to repair: {len(changes)} (labels: {', '.join(labels) or 'none'})")
    if apply and changes:
        with conn:
            for video_id, _old, new in changes:
                conn.execute(
                    "UPDATE videos SET performers = %s WHERE id = %s",
                    (json.dumps(new, ensure_ascii=False), video_id),
                )
    return len(changes)


def _clean_named_table(conn, table: str, apply: bool) -> int:
    names = _label_rows(conn, table, "name")
    print(f"{table} labels to delete: {len(names)} ({', '.join(sorted(set(names))) or 'none'})")
    if apply and names:
        with conn:
            for name in names:
                conn.execute(f"DELETE FROM {table} WHERE name = %s", (name,))
    return len(names)


def _deactivate_catalog_labels(conn, apply: bool) -> int:
    rows = conn.execute("SELECT id, name FROM performer_catalog WHERE active = TRUE").fetchall()
    ids = [str(performer_id) for performer_id, name in rows
           if name and is_known_non_performer_label(name)]
    print(f"performer_catalog labels to deactivate: {len(ids)}")
    if apply and ids:
        with conn:
            conn.execute(
                "UPDATE performer_catalog SET active = FALSE WHERE id = ANY(%s)", (ids,)
            )
    return len(ids)


def _reject_label_proposals(conn, apply: bool) -> int:
    rows = conn.execute(
        "SELECT id, candidate FROM telegram_performer_proposals WHERE status='pending'"
    ).fetchall()
    ids = [int(proposal_id) for proposal_id, candidate in rows
           if candidate and is_known_non_performer_label(candidate)]
    print(f"pending performer proposals to reject: {len(ids)}")
    if apply and ids:
        with conn:
            conn.execute(
                "UPDATE telegram_performer_proposals SET status='rejected' "
                "WHERE id = ANY(%s) AND status='pending'", (ids,)
            )
    return len(ids)


def _clean_aliases(conn, apply: bool) -> int:
    rows = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
    bad = [
        (alias, canonical) for alias, canonical in rows
        if is_known_non_performer_label(alias) or is_known_non_performer_label(canonical)
    ]
    print(f"performer_aliases labels to delete: {len(bad)}")
    if apply and bad:
        with conn:
            for alias, canonical in bad:
                conn.execute(
                    "DELETE FROM performer_aliases WHERE alias = %s AND canonical = %s",
                    (alias, canonical),
                )
    return len(bad)


def _refresh_stats(conn) -> None:
    try:
        from resolver.client_catalog import refresh_performer_stats
        rows = refresh_performer_stats(conn)
        print(f"performer_stats rebuilt: {rows} rows")
    except Exception as exc:
        print(f"Could not rebuild performer_stats: {exc}")


def _db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    # The application reaches PostgreSQL through the compose service name;
    # host-side one-off scripts use the published port.
    return url.replace("@postgres:5432", "@127.0.0.1:5432")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = ap.parse_args()

    db_url = _db_url()
    if args.apply:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        print(f"Backup: {create_db_backup(db_url, BACKUP_DIR, 'adulthub-pre-label-cleanup')}")

    conn = db.connect(db_url)
    try:
        _clean_videos(conn, args.apply)
        for table in VIDEO_TABLES:
            _clean_named_table(conn, table, args.apply)
        _clean_aliases(conn, args.apply)
        _reject_label_proposals(conn, args.apply)
        _deactivate_catalog_labels(conn, args.apply)
        if args.apply:
            _refresh_stats(conn)
    finally:
        conn.close()
    print("Applied." if args.apply else "Dry run only; pass --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
