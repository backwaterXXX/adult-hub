#!/usr/bin/env python3
"""Reparse performer names from video titles using improved multi-part title parsing."""
import json
import os
from common import db
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

from common.json_helpers import json_list_or_empty
from common.performers import is_valid_performer_name, NATIONALITY_PREFIXES
from common.performer_titles import parse_title_performers_with_known_map

DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None


def _name_is_word_subset(short_name: str, long_name: str) -> bool:
    short_words = str(short_name or "").lower().split()
    long_words = str(long_name or "").lower().split()
    return bool(short_words) and any(
        long_words[index:index + len(short_words)] == short_words
        for index in range(len(long_words) - len(short_words) + 1)
    )


def _load_known_map(conn) -> dict:
    known_names = [
        r[0]
        for r in conn.execute("SELECT name FROM performer_bios").fetchall()
        if is_valid_performer_name(r[0])
    ]
    known_map = {n.lower(): n for n in known_names}
    known_map.update({
        str(alias).strip().lower(): str(canonical).strip()
        for alias, canonical in conn.execute(
            "SELECT alias, canonical FROM performer_aliases"
        ).fetchall()
        if str(alias).strip() and str(canonical).strip()
    })
    return known_map


def _renames_existing_name(old_perfs: list, new_perfs: list) -> bool:
    return any(
        old_name.lower() not in {name.lower() for name in new_perfs}
        and (
            any(_name_is_word_subset(old_name, new_name) for new_name in new_perfs)
            or any(_name_is_word_subset(new_name, old_name) for new_name in new_perfs)
        )
        for old_name in old_perfs
    )


def _has_nationality_prefix(old_perfs: list) -> bool:
    return any(
        any(old_name.lower().startswith(p + " ") for p in NATIONALITY_PREFIXES)
        for old_name in old_perfs
    )


def _should_update(old_perfs: list, new_perfs: list) -> bool:
    old_multi = sum(1 for p in old_perfs if len(p.split()) >= 2)
    new_multi = sum(1 for p in new_perfs if len(p.split()) >= 2)

    if new_perfs == old_perfs:
        return False
    if not old_perfs:
        return True
    if new_multi > old_multi:
        return True
    if new_multi == old_multi and len(new_perfs) > len(old_perfs):
        return True
    if all(len(p.split()) == 1 for p in old_perfs) and new_multi > 0:
        return True
    if _renames_existing_name(old_perfs, new_perfs):
        return True
    return _has_nationality_prefix(old_perfs)


def _process_row(conn, row, known_map: dict) -> bool:
    """Reparse one Telegram video; return whether the row was updated."""
    vid_id, title, perfs_raw = row
    old_perfs = json_list_or_empty(perfs_raw)

    raw_title = title or ""
    new_perfs = parse_title_performers_with_known_map(raw_title, known_map)

    if not new_perfs or not _should_update(old_perfs, new_perfs):
        return False

    conn.execute(
        "UPDATE videos SET performers = %s WHERE id = %s",
        (json.dumps(new_perfs, ensure_ascii=False), vid_id),
    )
    return True


def run():
    conn = db.connect(DB_URL)
    known_map = _load_known_map(conn)

    rows = conn.execute(
        "SELECT id, title, performers FROM videos WHERE provider = 'telegram'"
    ).fetchall()

    videos_updated = 0

    print(f"Scanning {len(rows)} Telegram videos...")
    with conn:
        for row in rows:
            videos_updated += int(_process_row(conn, row, known_map))

    conn.close()
    print(f"Database update complete: {videos_updated} videos updated.")

if __name__ == "__main__":
    run()
