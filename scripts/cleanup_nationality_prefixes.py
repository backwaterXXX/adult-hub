#!/usr/bin/env python3
"""Cleanup nationality/demonym prefixes across DB, NFOs, and Jellyfin metadata."""
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
    is_valid_performer_name,
    is_safe_alias,
    normalize_performers,
    strip_nationality_prefix,
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


def _docker_pg_dump_backup() -> Path:
    import gzip
    import subprocess
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_file = BACKUP_DIR / f"adulthub-pre-nationality-cleanup-{stamp}.sql.gz"
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
    return backup_file


def _create_backup() -> Path:
    try:
        return db.pg_backup(DB_URL, BACKUP_DIR, prefix="adulthub-pre-nationality-cleanup")
    except RuntimeError:
        return _docker_pg_dump_backup()


def _clean_aliases(conn) -> None:
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
    if JF_PEOPLE_DIR.exists():
        for letter_dir in JF_PEOPLE_DIR.iterdir():
            if not letter_dir.is_dir():
                continue
            for person_dir in list(letter_dir.iterdir()):
                if not person_dir.is_dir():
                    continue
                pname = person_dir.name
                if strip_nationality_prefix(pname) != pname and _remove_person_dir(person_dir):
                    people_deleted += 1
    print(f"Total Jellyfin People folders removed: {people_deleted}")


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


def _regenerate_video_nfo(conn, row, new_perfs: list, nfo_path: Path) -> None:
    (
        vid_id,
        prov,
        title,
        slug,
        dur,
        thumb,
        _perfs_raw,
        cats_raw,
        tags_raw,
        studio,
        first_seen,
    ) = row
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
            old_perfs = _json_list(perfs_raw)

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
