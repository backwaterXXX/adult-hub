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


def run_cleanup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    print("Creating backup...")
    backup_file = db.pg_backup(DB_URL, BACKUP_DIR, prefix="adulthub-pre-performer-cleanup")
    print(f"Backup created successfully: {backup_file}")

    conn = db.connect(DB_URL)

    # 1. Clean performer_aliases
    aliases = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
    unsafe_aliases = [(a, c) for a, c in aliases if not is_safe_alias(a, c)]
    print(f"Deleting {len(unsafe_aliases)} unsafe aliases from performer_aliases...")
    with conn:
        for a, c in unsafe_aliases:
            conn.execute("DELETE FROM performer_aliases WHERE alias = %s", (a,))

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

    # 2. Clean performer_bios, photos, sync
    false_names = {'fucked', 'fuck', 'fucking', 'lucas', 'falcon', 'black', 'jordan', 'connor'}
    bios = [r[0] for r in conn.execute("SELECT name FROM performer_bios").fetchall()]
    invalid_bios = [n for n in bios if not is_valid_performer_name(n) or n.lower() in false_names]
    print(f"Deleting {len(invalid_bios)} invalid performer bios...")
    with conn:
        for n in invalid_bios:
            conn.execute("DELETE FROM performer_bios WHERE name = %s", (n,))

    photos = [r[0] for r in conn.execute("SELECT name FROM performer_photos").fetchall()]
    invalid_photos = [n for n in photos if not is_valid_performer_name(n) or n.lower() in false_names]
    print(f"Deleting {len(invalid_photos)} invalid performer photos...")
    with conn:
        for n in invalid_photos:
            conn.execute("DELETE FROM performer_photos WHERE name = %s", (n,))

    syncs = [r[0] for r in conn.execute("SELECT name FROM jellyfin_person_sync").fetchall()]
    invalid_syncs = [n for n in syncs if not is_valid_performer_name(n) or n.lower() in false_names]
    print(f"Deleting {len(invalid_syncs)} invalid jellyfin_person_sync entries...")
    with conn:
        for n in invalid_syncs:
            conn.execute("DELETE FROM jellyfin_person_sync WHERE name = %s", (n,))

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
                    try:
                        shutil.rmtree(person_dir)
                        people_deleted += 1
                        print(f"  Removed Jellyfin People folder: {person_dir.relative_to(BASE_DIR)}")
                    except Exception as e:
                        try:
                            import subprocess
                            rel = person_dir.relative_to(JF_PEOPLE_DIR)
                            res = subprocess.run(["docker", "exec", "jellyfin", "rm", "-rf", f"/config/metadata/People/{rel}"], capture_output=True)
                            if res.returncode == 0:
                                people_deleted += 1
                                print(f"  Removed Jellyfin People folder (via docker): {person_dir.relative_to(BASE_DIR)}")
                            else:
                                print(f"  Error removing {person_dir}: {e}")
                        except Exception:
                            print(f"  Error removing {person_dir}: {e}")
    print(f"Total Jellyfin People folders removed: {people_deleted}")

    # 4. Clean and fix videos table & NFO files
    known_names = [r[0] for r in conn.execute("SELECT name FROM performer_bios").fetchall() if is_valid_performer_name(r[0])]
    known_map = {n.lower(): n for n in known_names}
    alias_rows = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
    safe_aliases = {a: c for a, c in alias_rows if is_safe_alias(a, c)}
    known_map.update(safe_aliases)

    # Target videos: all rows where performers is populated OR Telegram videos where title contains performer markers
    print("Selecting candidate videos for performer correction...")
    rows = conn.execute(
        "SELECT id, provider, title, slug, duration, thumbnail, performers, categories, tags, studio, first_seen "
        "FROM videos WHERE performers <> '[]'::jsonb "
        "OR (provider = 'telegram' AND (title LIKE '%&%' OR title LIKE '%,%' OR title LIKE '%fuck%' OR title LIKE '%[%]%' OR title LIKE '%(%' OR title LIKE '% - %'))"
    ).fetchall()

    videos_updated = 0
    nfos_updated = 0

    print(f"Checking {len(rows)} video candidates...")
    with conn:
        for row in rows:
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

            try:
                old_perfs = json.loads(perfs_raw or "[]")
            except Exception:
                old_perfs = []

            raw_title = title or ""
            has_invalid = any(not is_valid_performer_name(p) for p in old_perfs)
            has_false_paddy = any(
                "paddy o'brian" in p.lower() for p in old_perfs
            ) and ("paddy" not in raw_title.lower() and "o'brian" not in raw_title.lower())

            # Try parsing title with our syntax parser
            parsed_title_perfs = parse_title_performers_with_known_map(raw_title, known_map) if prov == "telegram" else []

            if parsed_title_perfs:
                new_perfs = parsed_title_perfs
            elif has_invalid or has_false_paddy:
                filtered = [p for p in old_perfs if is_valid_performer_name(p)]
                if has_false_paddy:
                    filtered = [p for p in filtered if p != "Paddy O'Brian"]
                if prov == "telegram":
                    matched = match_known_performers(raw_title, known_names, safe_aliases)
                    combined = []
                    for p in filtered + matched:
                        if p not in combined and is_valid_performer_name(p):
                            combined.append(p)
                    new_perfs = combined
                else:
                    new_perfs = filtered
            else:
                new_perfs = old_perfs

            if "Cock Sure Men" in raw_title:
                new_perfs = ["Morgan Black", "Heath Jordan", "Conner Habib"]
            if "The Straight Man Part 4" in raw_title:
                new_perfs = ["Connor Halstead", "Eli Hunter", "Jared Summers"]

            if new_perfs != old_perfs:
                videos_updated += 1
                conn.execute(
                    "UPDATE videos SET performers = %s WHERE id = %s",
                    (json.dumps(new_perfs, ensure_ascii=False), vid_id),
                )

                # Update NFO file if it exists
                nfo_path = MEDIA_DIR / f"{S.stable_media_basename(vid_id)}.nfo"
                if nfo_path.exists():
                    try:
                        tag_set = set(json.loads(tags_raw)) if tags_raw else set()
                    except Exception:
                        tag_set = set()
                    try:
                        genres = set(json.loads(cats_raw)) if cats_raw else set()
                    except Exception:
                        genres = set()

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
                        nfos_updated += 1
                    except Exception as e:
                        print(f"  Error updating NFO {nfo_path}: {e}")

    print(f"Cleanup finished: {videos_updated} videos updated in DB, {nfos_updated} NFO files regenerated.")
    conn.close()


if __name__ == "__main__":
    run_cleanup()
