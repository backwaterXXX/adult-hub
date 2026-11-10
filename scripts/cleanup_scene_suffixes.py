#!/usr/bin/env python3
"""Cleanup trailing scene/season/episode suffixes (e.g. s1, s2, s3) across DB, NFOs, and Jellyfin."""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

from common import db
from common.json_helpers import json_list_or_empty
from common.performer_cleanup_support import (
    build_video_nfo,
    create_db_backup,
    iter_jellyfin_person_dirs,
    remove_person_dir_counted,
    remove_person_dir_via_docker,
)
from common.performers import (
    SCENE_SUFFIX_RE,
    STANDALONE_SCENE_RE,
    is_valid_performer_name,
    normalize_performers,
    strip_scene_suffix,
)
from sync import sync as S

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


def _create_backup() -> Path:
    return create_db_backup(DB_URL, BACKUP_DIR, "adulthub-pre-suffix-cleanup")


def _clean_aliases(conn) -> None:
    # Drops scene-suffixed aliases. The nationality and performer cleanups use other rules, so they stay separate.
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


def _clean_suffixed_names(conn, table: str) -> None:
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


def _remove_person_dir_via_docker(person_dir: Path, error: Exception) -> int:
    """Return how many folders this fallback counts as removed (0 or 1)."""
    return remove_person_dir_via_docker(person_dir, error, base_dir=BASE_DIR, people_dir=JF_PEOPLE_DIR)


def _remove_person_dir(person_dir: Path) -> int:
    """Remove one suffixed Jellyfin People folder; return how many removals to count."""
    return remove_person_dir_counted(person_dir, base_dir=BASE_DIR, people_dir=JF_PEOPLE_DIR)


def _clean_jellyfin_people() -> None:
    people_deleted = 0
    for person_dir in iter_jellyfin_person_dirs(JF_PEOPLE_DIR):
        pname = person_dir.name
        if SCENE_SUFFIX_RE.search(pname) or STANDALONE_SCENE_RE.match(pname):
            people_deleted += _remove_person_dir(person_dir)
    print(f"Total Jellyfin People folders removed: {people_deleted}")


def _regenerate_video_nfo(conn, row, new_perfs: list, nfo_path: Path) -> bool:
    video_dict, genres, tag_set = build_video_nfo(conn, row, new_perfs, sync_api=S)
    try:
        S.create_nfo_file(video_dict, genres, tag_set, nfo_path)
        return True
    except Exception as e:
        print(f"  Error updating NFO {nfo_path}: {e}")
        return False


def _clean_videos(conn) -> tuple[int, int]:
    """Normalize suffixed performers on videos (and their NFOs); return (videos, nfos) updated."""
    print("Selecting videos with scene-suffixed performers...")
    rows = conn.execute(
        "SELECT id, provider, title, slug, duration, thumbnail, performers, categories, tags, studio, first_seen "
        "FROM videos WHERE performers <> '[]'::jsonb"
    ).fetchall()

    videos_updated = 0
    nfos_updated = 0

    with conn:
        for row in rows:
            vid_id, perfs_raw = row[0], row[6]
            old_perfs = json_list_or_empty(perfs_raw)

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
                if nfo_path.exists() and _regenerate_video_nfo(conn, row, new_perfs, nfo_path):
                    nfos_updated += 1

    return videos_updated, nfos_updated


def _invalidate_resolver_cache() -> None:
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8090/api/performers/fuzzy?force_refresh=true")
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"Invalidated resolver fuzzy cache: HTTP {resp.status}")
    except Exception as e:
        print(f"Note: Could not invalidate resolver fuzzy cache via HTTP: {e}")


def run_cleanup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    print("Creating DB backup before suffix cleanup...")
    backup_file = _create_backup()
    print(f"Backup created successfully: {backup_file}")

    conn = db.connect(DB_URL)

    # 1. Clean performer_aliases
    _clean_aliases(conn)

    # 2. Clean performer_photos, performer_bios, jellyfin_person_sync
    for table in ("performer_photos", "performer_bios", "jellyfin_person_sync"):
        _clean_suffixed_names(conn, table)

    # 3. Clean Jellyfin People directory
    _clean_jellyfin_people()

    # 4. Clean videos table and NFO files
    videos_updated, nfos_updated = _clean_videos(conn)

    print(f"Cleanup finished: {videos_updated} videos updated in DB, {nfos_updated} NFO files regenerated.")
    conn.close()

    # Invalidate resolver fuzzy cache
    _invalidate_resolver_cache()


if __name__ == "__main__":
    run_cleanup()
