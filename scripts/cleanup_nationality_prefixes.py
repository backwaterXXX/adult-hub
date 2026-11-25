#!/usr/bin/env python3
"""Cleanup nationality/demonym prefixes across the catalog."""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

from common import db
from common.json_helpers import json_list_or_empty
from common.performer_cleanup_support import create_db_backup
from common.performers import (
    is_valid_performer_name,
    is_safe_alias,
    normalize_performers,
    strip_nationality_prefix,
    strip_scene_suffix,
)

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


def _clean_videos(conn) -> int:
    """Normalize performers on videos; return how many videos were updated."""
    print("Selecting videos with nationality-prefixed performers...")
    rows = conn.execute(
        "SELECT id, performers FROM videos WHERE performers <> '[]'::jsonb"
    ).fetchall()

    videos_updated = 0

    with conn:
        for vid_id, perfs_raw in rows:
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

    return videos_updated


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

    # 2. Clean performer_photos and performer_bios
    for table in ("performer_photos", "performer_bios"):
        _clean_name_table(conn, table)

    # 3. Clean videos table
    videos_updated = _clean_videos(conn)

    print(f"Cleanup finished: {videos_updated} videos updated in DB.")
    conn.close()

    # Invalidate resolver fuzzy cache
    _invalidate_resolver_cache()


if __name__ == "__main__":
    run_cleanup()
