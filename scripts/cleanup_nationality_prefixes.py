#!/usr/bin/env python3
"""Cleanup nationality/demonym prefixes across DB, NFOs, and Jellyfin metadata."""
import json
import os
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

from common import db
from common.json_helpers import json_list_or_empty
from common.performer_cleanup_support import build_video_nfo, create_db_backup, iter_jellyfin_person_dirs
from common.performers import (
    is_valid_performer_name,
    is_safe_alias,
    normalize_performers,
    strip_nationality_prefix,
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
    return create_db_backup(DB_URL, BACKUP_DIR, "adulthub-pre-nationality-cleanup")


def _clean_aliases(conn) -> None:
    # Strips nationality prefixes. The performer and scene cleanups use other rules, so they stay separate.
    print("Checking performer_aliases for nationality prefixes...")
    aliases = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
    deleted_aliases = 0
    updated_aliases = 0
    with conn:
        for a, c in aliases:
            sa = strip_nationality_prefix(a)
            sc = strip_nationality_prefix(c)
            # If alias had nationality prefix, remove it if redundant or update
            if sa != a or sc != c:
                new_c = sc if is_valid_performer_name(sc) else c
                if sa.lower() == new_c.lower() or not is_safe_alias(sa, new_c):
                    conn.execute("DELETE FROM performer_aliases WHERE alias = %s", (a,))
                    deleted_aliases += 1
                else:
                    conn.execute("DELETE FROM performer_aliases WHERE alias = %s", (a,))
                    conn.execute(
                        "INSERT INTO performer_aliases (alias, canonical) VALUES (%s, %s) "
                        "ON CONFLICT (alias) DO UPDATE SET canonical = EXCLUDED.canonical",
                        (sa, new_c),
                    )
                    updated_aliases += 1
    print(f"Aliases cleaned: {deleted_aliases} deleted, {updated_aliases} updated/migrated.")


def _clean_name_table(conn, table: str) -> None:
    rows = [r[0] for r in conn.execute(f"SELECT name FROM {table}").fetchall()]
    to_clean = [n for n in rows if strip_nationality_prefix(n) != n]
    deleted_count = 0
    updated_count = 0
    existing_names = set(rows)
    with conn:
        for n in to_clean:
            cleaned_name = strip_nationality_prefix(n)
            cleaned_name = strip_scene_suffix(cleaned_name)
            if not cleaned_name or cleaned_name in existing_names or not is_valid_performer_name(cleaned_name):
                conn.execute(f"DELETE FROM {table} WHERE name = %s", (n,))
                deleted_count += 1
            else:
                conn.execute(f"UPDATE {table} SET name = %s WHERE name = %s", (cleaned_name, n))
                existing_names.add(cleaned_name)
                updated_count += 1
    print(f"{table}: {deleted_count} deleted, {updated_count} updated.")


def _remove_person_dir(person_dir: Path) -> bool:
    """Remove one Jellyfin People folder; return True when it was removed."""
    try:
        shutil.rmtree(person_dir)
        return True
    except Exception as e:
        try:
            import subprocess
            rel = person_dir.relative_to(JF_PEOPLE_DIR)
            res = subprocess.run(
                ["docker", "exec", "jellyfin", "rm", "-rf", f"/config/metadata/People/{rel}"],
                capture_output=True,
            )
            if res.returncode == 0:
                return True
            print(f"  Error removing {person_dir}: {e}")
            return False
        except Exception:
            print(f"  Error removing {person_dir}: {e}")
            return False


def _clean_jellyfin_people() -> None:
    people_deleted = 0
    for person_dir in iter_jellyfin_person_dirs(JF_PEOPLE_DIR):
        pname = person_dir.name
        if strip_nationality_prefix(pname) != pname and _remove_person_dir(person_dir):
            people_deleted += 1
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
    """Normalize performers on videos (and their NFOs); return (videos, nfos) updated."""
    print("Selecting videos with nationality-prefixed performers...")
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

            if not old_perfs:
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
    print("Creating DB backup before nationality cleanup...")
    backup_file = _create_backup()
    print(f"Backup created successfully: {backup_file}")

    conn = db.connect(DB_URL)

    # 1. Clean performer_aliases
    _clean_aliases(conn)

    # 2. Clean performer_photos, performer_bios, jellyfin_person_sync
    for table in ("performer_photos", "performer_bios", "jellyfin_person_sync"):
        _clean_name_table(conn, table)

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
