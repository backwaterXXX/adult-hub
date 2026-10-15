#!/usr/bin/env python3
"""Cleanup trailing scene/season/episode suffixes (e.g. s1, s2, s3) across DB, NFOs, and Jellyfin."""
import json
import os
import shutil
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "sync"))
sys.path.insert(0, str(BASE_DIR / "resolver"))

from common import db
from common.performers import (
    SCENE_SUFFIX_RE,
    STANDALONE_SCENE_RE,
    is_valid_performer_name,
    normalize_performers,
    strip_scene_suffix,
)
import sync as S

RAW_DB_URL = (os.getenv("DATABASE_URL") or "").strip()
if not RAW_DB_URL:
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                RAW_DB_URL = line.split("=", 1)[1].strip()
                break

# Resolve docker hostname to loopback when running on host
if "@postgres:" in RAW_DB_URL:
    try:
        import socket
        socket.gethostbyname("postgres")
        DB_URL = RAW_DB_URL
    except Exception:
        DB_URL = RAW_DB_URL.replace("@postgres:", "@127.0.0.1:")
else:
    DB_URL = RAW_DB_URL or None

BACKUP_DIR = BASE_DIR / "data" / "backups"
MEDIA_DIR = BASE_DIR / "media" / "adult"
JF_PEOPLE_DIR = BASE_DIR / "jf-config" / "metadata" / "People"


def run_cleanup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    print("Creating DB backup before suffix cleanup...")
    try:
        backup_file = db.pg_backup(DB_URL, BACKUP_DIR, prefix="adulthub-pre-suffix-cleanup")
    except RuntimeError:
        import gzip
        import subprocess
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_file = BACKUP_DIR / f"adulthub-pre-suffix-cleanup-{stamp}.sql.gz"
        res = subprocess.run(
            ["docker", "exec", "postgres", "pg_dump", "-U", "adulthub", "adulthub"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(f"docker pg_dump failed: {res.stderr.decode()[:300]}")
        with gzip.open(backup_file, "wb") as f:
            f.write(res.stdout)
    print(f"Backup created successfully: {backup_file}")

    conn = db.connect(DB_URL)

    # 1. Clean performer_aliases
    print("Checking performer_aliases for scene suffixes...")
    aliases = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
    deleted_aliases = 0
    updated_aliases = 0
    with conn:
        for a, c in aliases:
            a_has_suffix = bool(SCENE_SUFFIX_RE.search(a) or STANDALONE_SCENE_RE.match(a))
            c_has_suffix = bool(SCENE_SUFFIX_RE.search(c) or STANDALONE_SCENE_RE.match(c))
            if a_has_suffix:
                # Aliases with scene numbers (e.g. 'derek kage s1') are removed since suffix stripping is automatic
                conn.execute("DELETE FROM performer_aliases WHERE alias = %s", (a,))
                deleted_aliases += 1
            elif c_has_suffix:
                new_c = strip_scene_suffix(c)
                if is_valid_performer_name(new_c):
                    conn.execute("UPDATE performer_aliases SET canonical = %s WHERE alias = %s", (new_c, a))
                    updated_aliases += 1
                else:
                    conn.execute("DELETE FROM performer_aliases WHERE alias = %s", (a,))
                    deleted_aliases += 1
    print(f"Aliases cleaned: {deleted_aliases} deleted, {updated_aliases} updated canonical.")

    # 2. Clean performer_photos, performer_bios, jellyfin_person_sync
    for table in ("performer_photos", "performer_bios", "jellyfin_person_sync"):
        rows = [r[0] for r in conn.execute(f"SELECT name FROM {table}").fetchall()]
        to_clean = [n for n in rows if SCENE_SUFFIX_RE.search(n) or STANDALONE_SCENE_RE.match(n)]
        deleted_count = 0
        updated_count = 0
        with conn:
            for n in to_clean:
                cleaned_name = strip_scene_suffix(n)
                if cleaned_name and cleaned_name in rows:
                    conn.execute(f"DELETE FROM {table} WHERE name = %s", (n,))
                    deleted_count += 1
                elif cleaned_name and is_valid_performer_name(cleaned_name):
                    conn.execute(f"UPDATE {table} SET name = %s WHERE name = %s", (cleaned_name, n))
                    updated_count += 1
                else:
                    conn.execute(f"DELETE FROM {table} WHERE name = %s", (n,))
                    deleted_count += 1
        print(f"{table}: {deleted_count} deleted, {updated_count} updated.")

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
                if SCENE_SUFFIX_RE.search(pname) or STANDALONE_SCENE_RE.match(pname):
                    try:
                        shutil.rmtree(person_dir)
                        people_deleted += 1
                        print(f"  Removed Jellyfin People folder: {person_dir.relative_to(BASE_DIR)}")
                    except Exception as e:
                        try:
                            import subprocess
                            rel = person_dir.relative_to(JF_PEOPLE_DIR)
                            res = subprocess.run(
                                ["docker", "exec", "jellyfin", "rm", "-rf", f"/config/metadata/People/{rel}"],
                                capture_output=True,
                            )
                            if res.returncode == 0:
                                people_deleted += 1
                                print(f"  Removed Jellyfin People folder (via docker): {person_dir.relative_to(BASE_DIR)}")
                            else:
                                print(f"  Error removing {person_dir}: {e}")
                        except Exception:
                            print(f"  Error removing {person_dir}: {e}")
    print(f"Total Jellyfin People folders removed: {people_deleted}")

    # 4. Clean videos table and NFO files
    print("Selecting videos with scene-suffixed performers...")
    rows = conn.execute(
        "SELECT id, provider, title, slug, duration, thumbnail, performers, categories, tags, studio, first_seen "
        "FROM videos WHERE performers <> '[]'::jsonb"
    ).fetchall()

    videos_updated = 0
    nfos_updated = 0

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

            if not any(SCENE_SUFFIX_RE.search(p) or STANDALONE_SCENE_RE.match(p) for p in old_perfs):
                continue

            new_perfs = normalize_performers(old_perfs)

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

    # Invalidate resolver fuzzy cache
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8090/api/performers/fuzzy?force_refresh=true")
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"Invalidated resolver fuzzy cache: HTTP {resp.status}")
    except Exception as e:
        print(f"Note: Could not invalidate resolver fuzzy cache via HTTP: {e}")


if __name__ == "__main__":
    run_cleanup()
