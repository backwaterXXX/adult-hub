#!/usr/bin/env python3
"""Cleanup bogus performers, unsafe aliases, and fix performer attributions in DB and NFO files."""
import json
import os
import shutil
from common import db
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "sync"))
sys.path.insert(0, str(BASE_DIR / "resolver"))

from common.performers import is_valid_performer_name, is_safe_alias, normalize_performers
from common.performer_titles import parse_title_performers_with_known_map
from resolver.providers.telegram import match_known_performers
import sync as S

DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None
BACKUP_DIR = BASE_DIR / "data" / "backups"
MEDIA_DIR = BASE_DIR / "media" / "adult"
JF_PEOPLE_DIR = BASE_DIR / "jf-config" / "metadata" / "People"


def _json_list(raw) -> list:
    try:
        return json.loads(raw or "[]")
    except Exception:
        return []


def _json_set(raw) -> set:
    try:
        return set(json.loads(raw)) if raw else set()
    except Exception:
        return set()


def _clean_aliases(conn) -> None:
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
    # 2. Clean performer_bios, photos, sync
    false_names = {'fucked', 'fuck', 'fucking', 'lucas', 'falcon', 'black', 'jordan', 'connor'}
    _delete_invalid_names(conn, "performer_bios", "performer bios", false_names)
    _delete_invalid_names(conn, "performer_photos", "performer photos", false_names)
    _delete_invalid_names(conn, "jellyfin_person_sync", "jellyfin_person_sync entries", false_names)


def _remove_person_dir_via_docker(person_dir: Path, error: Exception) -> int:
    """Return how many folders this fallback counts as removed (0 or 1)."""
    removed = 0
    try:
        import subprocess
        rel = person_dir.relative_to(JF_PEOPLE_DIR)
        res = subprocess.run(["docker", "exec", "jellyfin", "rm", "-rf", f"/config/metadata/People/{rel}"], capture_output=True)
        if res.returncode == 0:
            removed += 1
            print(f"  Removed Jellyfin People folder (via docker): {person_dir.relative_to(BASE_DIR)}")
            return removed
        print(f"  Error removing {person_dir}: {error}")
        return removed
    except Exception:
        print(f"  Error removing {person_dir}: {error}")
        return removed


def _remove_invalid_person_dir(person_dir: Path) -> int:
    """Remove one invalid Jellyfin People folder; return how many removals to count."""
    removed = 0
    try:
        shutil.rmtree(person_dir)
        removed += 1
        print(f"  Removed Jellyfin People folder: {person_dir.relative_to(BASE_DIR)}")
        return removed
    except Exception as e:
        return removed + _remove_person_dir_via_docker(person_dir, e)


def _clean_jellyfin_people() -> None:
    # 3. Clean Jellyfin People directory
    people_deleted = 0
    if JF_PEOPLE_DIR.exists():
        for letter_dir in JF_PEOPLE_DIR.iterdir():
            if not letter_dir.is_dir():
                continue
            for person_dir in list(letter_dir.iterdir()):
                if not person_dir.is_dir():
                    continue
                pname = person_dir.name
                if not is_valid_performer_name(pname):
                    people_deleted += _remove_invalid_person_dir(person_dir)
    print(f"Total Jellyfin People folders removed: {people_deleted}")


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
        "SELECT id, provider, title, slug, duration, thumbnail, performers, categories, tags, studio, first_seen "
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


def _regenerate_video_nfo(conn, vid_id, prov, title, slug, dur, thumb, cats_raw, tags_raw,
                          studio, first_seen, new_perfs, nfo_path) -> bool:
    tag_set = _json_set(tags_raw)
    genres = _json_set(cats_raw)

    video_dict = {
        "id": vid_id,
        "provider": prov,
        "provider_video_id": vid_id.split(":", 1)[-1],
        "title": title or vid_id,
        "slug": slug or "",
        "duration": dur or 0,
        "thumbnail": thumb or "",
        "performers": new_perfs,
        "performer_thumbs": S.load_performer_thumbs(conn, new_perfs),
        "studio": studio or "",
        "dateadded": S.format_dateadded(first_seen or time.time()),
    }
    try:
        S.create_nfo_file(video_dict, genres, tag_set, nfo_path)
        return True
    except Exception as e:
        print(f"  Error updating NFO {nfo_path}: {e}")
        return False


def _correct_video(conn, row, known_map, known_names, safe_aliases) -> tuple[bool, bool]:
    """Correct one video's performers; return (video_updated, nfo_updated)."""
    (
        vid_id,
        prov,
        title,
        slug,
        dur,
        thumb,
        perfs_raw,
        cats_raw,
        tags_raw,
        studio,
        first_seen,
    ) = row

    old_perfs = _json_list(perfs_raw)
    raw_title = title or ""
    new_perfs = _compute_new_performers(
        prov, raw_title, old_perfs, known_map, known_names, safe_aliases
    )

    if new_perfs == old_perfs:
        return False, False

    conn.execute(
        "UPDATE videos SET performers = %s WHERE id = %s",
        (json.dumps(new_perfs, ensure_ascii=False), vid_id),
    )

    # Update NFO file if it exists
    nfo_path = MEDIA_DIR / f"{S.stable_media_basename(vid_id)}.nfo"
    nfo_updated = False
    if nfo_path.exists():
        nfo_updated = _regenerate_video_nfo(
            conn, vid_id, prov, title, slug, dur, thumb, cats_raw, tags_raw,
            studio, first_seen, new_perfs, nfo_path,
        )
    return True, nfo_updated


def _correct_videos(conn, rows, known_map, known_names, safe_aliases) -> tuple[int, int]:
    videos_updated = 0
    nfos_updated = 0

    print(f"Checking {len(rows)} video candidates...")
    with conn:
        for row in rows:
            video_updated, nfo_updated = _correct_video(
                conn, row, known_map, known_names, safe_aliases
            )
            videos_updated += int(video_updated)
            nfos_updated += int(nfo_updated)
    return videos_updated, nfos_updated


def run_cleanup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    print("Creating backup...")
    backup_file = db.pg_backup(DB_URL, BACKUP_DIR, prefix="adulthub-pre-performer-cleanup")
    print(f"Backup created successfully: {backup_file}")

    conn = db.connect(DB_URL)

    # 1. Clean performer_aliases
    _clean_aliases(conn)
    _normalize_alias_canonicals(conn)

    # 2. Clean performer_bios, photos, sync
    _clean_invalid_names(conn)

    # 3. Clean Jellyfin People directory
    _clean_jellyfin_people()

    # 4. Clean and fix videos table & NFO files
    known_names, known_map, safe_aliases = _load_known_maps(conn)
    rows = _fetch_candidate_rows(conn)
    videos_updated, nfos_updated = _correct_videos(conn, rows, known_map, known_names, safe_aliases)

    print(f"Cleanup finished: {videos_updated} videos updated in DB, {nfos_updated} NFO files regenerated.")
    conn.close()


if __name__ == "__main__":
    run_cleanup()
