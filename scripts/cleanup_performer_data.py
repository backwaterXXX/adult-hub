#!/usr/bin/env python3
"""Cleanup bogus performers, unsafe aliases, and fix performer attributions in the DB."""
import json
import os
from common import db
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

from common.json_helpers import json_list_or_empty
from common.performers import is_valid_performer_name, is_safe_alias, normalize_performers
from common.performer_titles import parse_title_performers_with_known_map
from resolver.providers.telegram import match_known_performers

DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None
BACKUP_DIR = BASE_DIR / "data" / "backups"


def _clean_aliases(conn) -> None:
    # Removes unsafe aliases. The nationality and scene cleanups use other rules, so they stay separate.
    aliases = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
    unsafe_aliases = [(a, c) for a, c in aliases if not is_safe_alias(a, c)]
    print(f"Deleting {len(unsafe_aliases)} unsafe aliases from performer_aliases...")
    with conn:
        for a, c in unsafe_aliases:
            conn.execute("DELETE FROM performer_aliases WHERE alias = %s", (a,))


def _normalize_alias_canonicals(conn) -> None:
    # Fix any canonical names with nationality prefixes
    remaining_aliases = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
    fixed_aliases = 0
    with conn:
        for a, c in remaining_aliases:
            c_norm = normalize_performers([c])
            if c_norm and c_norm[0] != c and is_valid_performer_name(c_norm[0]):
                new_c = c_norm[0]
                conn.execute("UPDATE performer_aliases SET canonical = %s WHERE alias = %s", (new_c, a))
                fixed_aliases += 1
    if fixed_aliases:
        print(f"Normalized {fixed_aliases} alias canonical names (stripped nationality prefixes).")


def _delete_invalid_names(conn, table: str, label: str, false_names: set) -> None:
    names = [r[0] for r in conn.execute(f"SELECT name FROM {table}").fetchall()]
    invalid = [n for n in names if not is_valid_performer_name(n) or n.lower() in false_names]
    print(f"Deleting {len(invalid)} invalid {label}...")
    with conn:
        for n in invalid:
            conn.execute(f"DELETE FROM {table} WHERE name = %s", (n,))


def _clean_invalid_names(conn) -> None:
    # 2. Clean performer_bios and photos
    false_names = {'fucked', 'fuck', 'fucking', 'lucas', 'falcon', 'black', 'jordan', 'connor'}
    _delete_invalid_names(conn, "performer_bios", "performer bios", false_names)
    _delete_invalid_names(conn, "performer_photos", "performer photos", false_names)


def _load_known_maps(conn):
    known_names = [r[0] for r in conn.execute("SELECT name FROM performer_bios").fetchall() if is_valid_performer_name(r[0])]
    known_map = {n.lower(): n for n in known_names}
    alias_rows = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
    safe_aliases = {a: c for a, c in alias_rows if is_safe_alias(a, c)}
    known_map.update(safe_aliases)
    return known_names, known_map, safe_aliases


def _fetch_candidate_rows(conn) -> list:
    # Target videos: all rows where performers is populated OR Telegram videos where title contains performer markers
    print("Selecting candidate videos for performer correction...")
    return conn.execute(
        "SELECT id, provider, title, performers "
        "FROM videos WHERE performers <> '[]'::jsonb "
        "OR (provider = 'telegram' AND (title LIKE '%&%' OR title LIKE '%,%' OR title LIKE '%fuck%' OR title LIKE '%[%]%' OR title LIKE '%(%' OR title LIKE '% - %'))"
    ).fetchall()


def _repair_performers(prov, raw_title, old_perfs, has_false_paddy, known_names, safe_aliases) -> list:
    filtered = [p for p in old_perfs if is_valid_performer_name(p)]
    if has_false_paddy:
        filtered = [p for p in filtered if p != "Paddy O'Brian"]
    if prov == "telegram":
        matched = match_known_performers(raw_title, known_names, safe_aliases)
        combined = []
        for p in filtered + matched:
            if p not in combined and is_valid_performer_name(p):
                combined.append(p)
        return combined
    return filtered


def _compute_new_performers(prov, raw_title, old_perfs, known_map, known_names, safe_aliases) -> list:
    has_invalid = any(not is_valid_performer_name(p) for p in old_perfs)
    has_false_paddy = any(
        "paddy o'brian" in p.lower() for p in old_perfs
    ) and ("paddy" not in raw_title.lower() and "o'brian" not in raw_title.lower())

    # Try parsing title with our syntax parser
    parsed_title_perfs = parse_title_performers_with_known_map(raw_title, known_map) if prov == "telegram" else []

    if parsed_title_perfs:
        new_perfs = parsed_title_perfs
    elif has_invalid or has_false_paddy:
        new_perfs = _repair_performers(
            prov, raw_title, old_perfs, has_false_paddy, known_names, safe_aliases
        )
    else:
        new_perfs = old_perfs

    if "Cock Sure Men" in raw_title:
        new_perfs = ["Morgan Black", "Heath Jordan", "Conner Habib"]
    if "The Straight Man Part 4" in raw_title:
        new_perfs = ["Connor Halstead", "Eli Hunter", "Jared Summers"]
    return new_perfs


def _correct_video(conn, row, known_map, known_names, safe_aliases) -> bool:
    """Correct one video's performers; return whether the row was updated."""
    vid_id, prov, title, perfs_raw = row

    old_perfs = json_list_or_empty(perfs_raw)
    raw_title = title or ""
    new_perfs = _compute_new_performers(
        prov, raw_title, old_perfs, known_map, known_names, safe_aliases
    )

    if new_perfs == old_perfs:
        return False

    conn.execute(
        "UPDATE videos SET performers = %s WHERE id = %s",
        (json.dumps(new_perfs, ensure_ascii=False), vid_id),
    )
    return True


def _correct_videos(conn, rows, known_map, known_names, safe_aliases) -> int:
    videos_updated = 0

    print(f"Checking {len(rows)} video candidates...")
    with conn:
        for row in rows:
            videos_updated += int(_correct_video(
                conn, row, known_map, known_names, safe_aliases
            ))
    return videos_updated


def run_cleanup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    print("Creating backup...")
    backup_file = db.pg_backup(DB_URL, BACKUP_DIR, prefix="adulthub-pre-performer-cleanup")
    print(f"Backup created successfully: {backup_file}")

    conn = db.connect(DB_URL)

    # 1. Clean performer_aliases
    _clean_aliases(conn)
    _normalize_alias_canonicals(conn)

    # 2. Clean performer_bios and photos
    _clean_invalid_names(conn)

    # 4. Clean and fix videos table
    known_names, known_map, safe_aliases = _load_known_maps(conn)
    rows = _fetch_candidate_rows(conn)
    videos_updated = _correct_videos(conn, rows, known_map, known_names, safe_aliases)

    print(f"Cleanup finished: {videos_updated} videos updated in DB.")
    conn.close()


if __name__ == "__main__":
    run_cleanup()
