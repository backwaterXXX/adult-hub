#!/usr/bin/env python3
"""Merge duplicate performers in Adult-Hub database (removing bracketed nicknames).

Example:
  "Adam Snow (AdamSnowXXX)" -> "Adam Snow"
  "Joey Mills (JoeyMillsXXX)" -> "Joey Mills"

The script updates:
  1. videos.performers (JSON list of performers for each video)
  2. performer_photos (migrates photos to canonical name and removes old entry)
  3. performer_bios (migrates bio/birth to canonical name and removes old entry)
  4. performer_aliases (saves aliases: "name (nick)" -> "name", "nick" -> "name")
  5. jellyfin_person_sync (removes obsolete entries)
  6. .nfo files on disk (rewrites <actor>, <set>, <tag> tags)

Usage:
  python3 -m sync.merge_performer_dupes            # dry-run
  python3 -m sync.merge_performer_dupes --apply    # apply changes
"""

import argparse
import html
import json
import os
import re
from common import db
from common.logging_setup import configure_logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

# Access to sync module

DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./media/adult"))

logger = configure_logging("merge_performer_dupes")

PAREN_PATTERN = re.compile(r"^(.*?)\s*\((.*?)\)\s*$")


def find_performer_mappings(conn: db.Connection) -> Dict[str, Tuple[str, str]]:
    """Zwraca mapowanie: raw_performer -> (base_name, nick) ze wszystkich tabel."""
    unique_performers = set()

    # 1. videos
    # videos_performers_array_ck keeps the column a JSON array of strings, the
    # <> '[]' filter excludes the NULL case, and the db facade yields jsonb as text.
    rows = conn.execute(
        "SELECT performers FROM videos WHERE performers <> '[]'::jsonb"
    ).fetchall()
    for (raw_json,) in rows:
        for p in json.loads(raw_json):
            if "(" in p or ")" in p:
                unique_performers.add(p.strip())

    # 2. performer_photos, performer_bios, jellyfin_person_sync
    tables = db.table_names(conn)
    for tbl in ("performer_photos", "performer_bios", "jellyfin_person_sync"):
        if tbl in tables:
            for (n,) in conn.execute(
                f"SELECT name FROM {tbl} WHERE name LIKE '%(%)%'"
            ).fetchall():
                unique_performers.add(n.strip())

    mapping = {}
    for p in sorted(unique_performers):
        m = PAREN_PATTERN.match(p)
        if m:
            base = html.unescape(m.group(1)).strip()
            nick = html.unescape(m.group(2)).strip()
            if base:
                mapping[p] = (base, nick)
    return mapping


def plan_video_updates(
    conn: db.Connection, mapping: Dict[str, Tuple[str, str]]
) -> List[Tuple[str, List[str], List[str]]]:
    """Zwraca liste krotek (video_id, stare_performers, nowe_performers)."""
    rows = conn.execute(
        "SELECT id, performers FROM videos WHERE performers <> '[]'::jsonb"
    ).fetchall()

    updates = []
    for vid_id, raw_json in rows:
        arr = json.loads(raw_json)

        changed = False
        new_list = []
        seen = set()
        for p in arr:
            target = mapping[p][0] if p in mapping else p
            if target and target not in seen:
                seen.add(target)
                new_list.append(target)
            if target != p or len(new_list) != len(arr):
                changed = True

        if changed and new_list != arr:
            updates.append((vid_id, arr, new_list))

    return updates


def make_backup(db_url=None, backup_dir=None) -> Path:
    """Create a compressed PostgreSQL backup with pg_dump (see ``common.db``)."""
    return Path(str(db.pg_backup(db_url or DB_URL, backup_dir,
                                 prefix="adulthub-pre-performer-merge")))


def trigger_jellyfin_refresh(url: str, api_key: str):
    """Zgloszenie odswiezenia biblioteki do Jellyfin API."""
    if not url or not api_key:
        return
    try:
        from sync.jellyfin_db import trigger_library_refresh as _trig
        _trig(url, api_key, logger=logger)
    except Exception as e:
        logger.warning("Nie udalo sie wywolac Jellyfin Library/Refresh: %s", e)


def _protected_override_names(conn: db.Connection, mapping: Dict[str, Tuple[str, str]]) -> list:
    from common.performer_profiles import merge_override_names
    return sorted({
        name
        for raw_name, (base_name, _) in mapping.items()
        for name in merge_override_names(conn, raw_name, base_name)
    }, key=str.casefold)


def _plan_photo_updates(conn: db.Connection, tables, mapping: Dict[str, Tuple[str, str]]) -> tuple:
    photos_to_migrate = []
    photos_to_delete = []
    if "performer_photos" in tables:
        photo_rows = dict(
            conn.execute("SELECT name, thumb FROM performer_photos").fetchall()
        )
        for raw_name, (base_name, _) in mapping.items():
            if raw_name in photo_rows:
                raw_thumb = photo_rows[raw_name]
                base_thumb = photo_rows.get(base_name, "")
                if raw_thumb and not base_thumb:
                    photos_to_migrate.append((base_name, raw_thumb))
                photos_to_delete.append(raw_name)
    return photos_to_migrate, photos_to_delete


def _plan_bio_updates(conn: db.Connection, tables, mapping: Dict[str, Tuple[str, str]]) -> tuple:
    bios_to_migrate = []
    bios_to_delete = []
    if "performer_bios" in tables:
        bio_rows = {
            r[0]: r[1:]
            for r in conn.execute(
                "SELECT name, bio, birth, aliases, updated_at FROM performer_bios"
            ).fetchall()
        }
        for raw_name, (base_name, _) in mapping.items():
            if raw_name in bio_rows:
                raw_bio, raw_birth, raw_aliases, raw_updated_at = bio_rows[raw_name]
                if base_name not in bio_rows:
                    bios_to_migrate.append(
                        (base_name, raw_bio, raw_birth, raw_aliases, raw_updated_at)
                    )
                else:
                    base_bio, base_birth, _, _ = bio_rows[base_name]
                    if (not base_bio and raw_bio) or (not base_birth and raw_birth):
                        bios_to_migrate.append(
                            (
                                base_name,
                                base_bio or raw_bio,
                                base_birth or raw_birth,
                                raw_aliases,
                                raw_updated_at,
                            )
                        )
                bios_to_delete.append(raw_name)
    return bios_to_migrate, bios_to_delete


def _print_sample_mappings(mapping: Dict[str, Tuple[str, str]]) -> None:
    print("\n--- Sample merges (first 20): ---")
    for raw_name in sorted(mapping.keys())[:20]:
        base, nick = mapping[raw_name]
        print(f"  '{raw_name}' -> '{base}' (alias nick: '{nick}')")
    print("------------------------------------------\n")


def _apply_video_updates(conn: db.Connection, video_updates) -> None:
    logger.info("Updating %d videos in database...", len(video_updates))
    with conn:
        for vid_id, _, new_list in video_updates:
            conn.execute(
                "UPDATE videos SET performers = %s WHERE id = %s",
                (json.dumps(new_list), vid_id),
            )


def _apply_photo_updates(conn: db.Connection, photos_to_migrate, photos_to_delete) -> None:
    if photos_to_migrate or photos_to_delete:
        now_ts = int(time.time())
        with conn:
            for base_name, thumb in photos_to_migrate:
                conn.execute(
                    "INSERT INTO performer_photos (name, thumb, updated_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (name) DO UPDATE SET thumb=EXCLUDED.thumb, updated_at=EXCLUDED.updated_at",
                    (base_name, thumb, now_ts),
                )
            for raw_name in photos_to_delete:
                conn.execute(
                    "DELETE FROM performer_photos WHERE name = %s", (raw_name,)
                )
        logger.info("Updated performer_photos.")


def _apply_bio_updates(conn: db.Connection, bios_to_migrate, bios_to_delete) -> None:
    if bios_to_migrate or bios_to_delete:
        with conn:
            for base_name, bio, birth, aliases, upd_at in bios_to_migrate:
                conn.execute(
                    """
                    INSERT INTO performer_bios (name, bio, birth, aliases, updated_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (name) DO UPDATE SET bio=EXCLUDED.bio, birth=EXCLUDED.birth,
                        aliases=EXCLUDED.aliases, updated_at=EXCLUDED.updated_at
                    """,
                    (base_name, bio, birth, aliases, upd_at),
                )
            for raw_name in bios_to_delete:
                conn.execute(
                    "DELETE FROM performer_bios WHERE name = %s", (raw_name,)
                )
        logger.info("Updated performer_bios.")


def _apply_alias_rows(conn: db.Connection, tables, mapping: Dict[str, Tuple[str, str]]) -> None:
    if "performer_aliases" in tables:
        added_aliases = 0
        with conn:
            for raw_name, (base_name, nick) in mapping.items():
                conn.execute(
                    "INSERT INTO performer_aliases (alias, canonical) VALUES (%s, %s) ON CONFLICT (alias) DO NOTHING",
                    (raw_name.lower(), base_name),
                )
                added_aliases += 1
                if nick and nick.lower() != base_name.lower():
                    conn.execute(
                        "INSERT INTO performer_aliases (alias, canonical) VALUES (%s, %s) ON CONFLICT (alias) DO NOTHING",
                        (nick.lower(), base_name),
                    )
                    added_aliases += 1
        logger.info("Saved aliases in performer_aliases table (added: %d).", added_aliases)


def _clean_jellyfin_person_sync(conn: db.Connection, tables, mapping: Dict[str, Tuple[str, str]]) -> None:
    if "jellyfin_person_sync" in tables:
        with conn:
            for raw_name in mapping.keys():
                conn.execute(
                    "DELETE FROM jellyfin_person_sync WHERE lower(name) = lower(%s)",
                    (raw_name,),
                )
        logger.info("Cleaned old entries in jellyfin_person_sync.")


def _rewrite_video_nfo(conn: db.Connection, S, vid_id: str, new_perfs, media_dir: Path) -> bool:
    """Rewrite one video's NFO; return True when the file was written."""
    row = conn.execute(
        "SELECT provider, title, slug, duration, thumbnail, performers,"
        " categories, tags, studio, first_seen FROM videos WHERE id = %s",
        (vid_id,),
    ).fetchone()
    if not row:
        return False
    (
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

    # The schema keeps both columns NOT NULL jsonb arrays of strings
    # (videos_tags_array_ck, videos_categories_array_ck), so no guard needed.
    tag_set = set(json.loads(tags_raw))
    genres = set(json.loads(cats_raw))

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
        "dateadded": S.format_dateadded(
            first_seen or time.time()
        ),
    }
    nfo_path = media_dir / f"{S.stable_media_basename(vid_id)}.nfo"
    try:
        S.create_nfo_file(video_dict, genres, tag_set, nfo_path)
        return True
    except Exception as e:
        logger.warning("Could not update NFO %s: %s", nfo_path, e)
        return False


def _rewrite_nfo_files(conn: db.Connection, video_updates, media_dir: Path) -> None:
    logger.info("Updating .nfo files for modified videos...")
    try:
        from sync import sync as S
        nfo_updated = 0
        for vid_id, _, new_perfs in video_updates:
            if _rewrite_video_nfo(conn, S, vid_id, new_perfs, media_dir):
                nfo_updated += 1
        logger.info("Updated %d .nfo files.", nfo_updated)
    except Exception as e:
        logger.error("Error updating NFO files: %s", e)


def _resolve_jellyfin_credentials() -> tuple:
    jf_url = os.getenv("JELLYFIN_URL", "")
    jf_key = os.getenv("JELLYFIN_API_KEY", "")
    if not jf_url or not jf_key:
        env_file = Path(__file__).parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("JELLYFIN_URL="):
                    jf_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("JELLYFIN_API_KEY="):
                    jf_key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return jf_url, jf_key


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge duplicate performers (remove bracketed nicknames)"
    )
    parser.add_argument("--db-url", default=DB_URL, help="PostgreSQL URL (or DATABASE_URL env)")
    parser.add_argument("--media", default=str(MEDIA_DIR), help="Path to media directory")
    parser.add_argument(
        "--apply", action="store_true", help="Apply changes (default: dry-run)"
    )
    parser.add_argument(
        "--skip-backup", action="store_true", help="Skip creating database backup"
    )
    args = parser.parse_args()

    media_dir = Path(args.media).resolve()

    conn = db.connect(args.db_url or DB_URL)
    try:
        mapping = find_performer_mappings(conn)
        logger.info("Found %d performers with bracketed nicknames.", len(mapping))

        video_updates = plan_video_updates(conn, mapping)
        logger.info(
            "Number of videos to update performers for: %d", len(video_updates)
        )

        # Check auxiliary tables
        tables = db.table_names(conn)
        if args.apply and "performer_profile_overrides" in tables:
            protected = _protected_override_names(conn, mapping)
            if protected:
                logger.error(
                    "Refusing to merge performers with manual profile overrides; reset these profiles first: %s",
                    ", ".join(protected[:8]),
                )
                return 1

        photos_to_migrate, photos_to_delete = _plan_photo_updates(conn, tables, mapping)
        bios_to_migrate, bios_to_delete = _plan_bio_updates(conn, tables, mapping)

        logger.info(
            "Photos: %d to migrate, %d to remove from old keys.",
            len(photos_to_migrate),
            len(photos_to_delete),
        )
        logger.info(
            "Bios: %d to migrate, %d to remove from old keys.",
            len(bios_to_migrate),
            len(bios_to_delete),
        )

        # Print sample mappings
        _print_sample_mappings(mapping)

        if not args.apply:
            logger.info("DRY-RUN mode finished. No changes saved.")
            logger.info("To apply changes, run with the --apply flag.")
            return 0

        # Create database backup before changes
        if not args.skip_backup:
            logger.info("Creating database backup...")
            try:
                dest = make_backup(args.db_url or DB_URL)
                logger.info("Backup created: %s", dest)
            except Exception as e:
                logger.error("Backup creation failed: %s. Aborting.", e)
                return 1

        # 1. Update videos
        _apply_video_updates(conn, video_updates)
        # 2. Update performer_photos
        _apply_photo_updates(conn, photos_to_migrate, photos_to_delete)
        # 3. Update performer_bios
        _apply_bio_updates(conn, bios_to_migrate, bios_to_delete)
        # 4. Save aliases in performer_aliases
        _apply_alias_rows(conn, tables, mapping)
        # 5. Clean jellyfin_person_sync
        _clean_jellyfin_person_sync(conn, tables, mapping)
        # 6. Update .nfo files on disk
        _rewrite_nfo_files(conn, video_updates, media_dir)

        # 7. Refresh Jellyfin library
        jf_url, jf_key = _resolve_jellyfin_credentials()
        if jf_url and jf_key:
            trigger_jellyfin_refresh(jf_url, jf_key)

        logger.info("SUCCESS: All performers with bracketed nicknames were successfully merged.")
        return 0

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
