"""Performer alias and metadata data migrations."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from common import db
from common.performers import is_safe_alias, is_valid_performer_name
import logging

logger = logging.getLogger(__name__)

REDUNDANT_PERFORMERS_VERSION = 7


def _resolve_services(services: Optional[Any]) -> Any:
    if services is not None:
        return services
    if __package__:
        from .data_migrations import _resolve_default_services
    else:
        from data_migrations import _resolve_default_services
    return _resolve_default_services(None)


def break_alias_cycles_and_flatten(conn: db.Connection) -> Tuple[int, int]:
    """Break 2-cycles and flatten alias chains in performer_aliases table."""
    tables = db.table_names(conn)
    if "performer_aliases" not in tables:
        return 0, 0

    with conn:
        conn.execute("DELETE FROM performer_aliases WHERE lower(alias) = lower(canonical)")

    rows = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
    alias_map = {str(a).strip().lower(): str(c).strip() for a, c in rows if a and c}

    video_counts: Dict[str, int] = defaultdict(int)
    if "videos" in tables:
        for (raw,) in conn.execute("SELECT performers FROM videos WHERE performers <> '[]'::jsonb").fetchall():
            try:
                for p in json.loads(raw):
                    video_counts[str(p).strip().lower()] += 1
            except Exception as _exc:
                logger.debug("Ignored exception in %s", "break_alias_cycles_and_flatten", exc_info=True)

    photos = {str(n).strip().lower() for (n,) in conn.execute("SELECT name FROM performer_photos").fetchall()} if "performer_photos" in tables else set()
    bios = {str(n).strip().lower() for (n,) in conn.execute("SELECT name FROM performer_bios").fetchall()} if "performer_bios" in tables else set()

    def score_name(name: str) -> int:
        low = name.lower()
        score = video_counts.get(low, 0) * 10
        if low in photos:
            score += 5
        if low in bios:
            score += 5
        if "'" in name:
            score += 2
        if "’" in name or "‘" in name:
            score -= 2
        return score

    broken = 0
    with conn:
        for a, c_name in list(alias_map.items()):
            if a not in alias_map:
                continue
            c_low = c_name.lower()
            if c_low in alias_map and alias_map[c_low].lower() == a:
                s_a = score_name(a)
                s_c = score_name(c_name)
                if s_a != s_c:
                    use_a = s_a > s_c
                else:
                    use_a = (a[0].isupper() if a else False, len(a)) > (c_name[0].isupper() if c_name else False, len(c_name))

                if use_a:
                    winner = a.title() if a.islower() else a
                    loser = c_low
                else:
                    winner = c_name
                    loser = a
                conn.execute("DELETE FROM performer_aliases WHERE lower(alias) = lower(%s)", (winner,))
                conn.execute("INSERT INTO performer_aliases (alias, canonical) VALUES (lower(%s), %s) "
                             "ON CONFLICT (alias) DO UPDATE SET canonical=EXCLUDED.canonical", (loser, winner))
                alias_map[loser] = winner
                alias_map.pop(winner.lower(), None)
                broken += 1

        flattened = 0
        for a, c_name in list(alias_map.items()):
            curr = c_name
            visited = {a}
            while curr.lower() in alias_map and curr.lower() not in visited:
                visited.add(curr.lower())
                curr = alias_map[curr.lower()]
            if curr != c_name:
                conn.execute("UPDATE performer_aliases SET canonical = %s WHERE alias = %s", (curr, a))
                alias_map[a] = curr
                flattened += 1

        if "performer_photos" in tables:
            photo_rows = dict(conn.execute("SELECT name, thumb FROM performer_photos").fetchall())
            for alias, canon in alias_map.items():
                for orig_name, thumb in list(photo_rows.items()):
                    if orig_name.lower() == alias:
                        canon_thumb = photo_rows.get(canon, "")
                        if thumb and not canon_thumb:
                            conn.execute(
                                "INSERT INTO performer_photos (name, thumb, updated_at) VALUES (%s, %s, %s) "
                                "ON CONFLICT (name) DO UPDATE SET thumb=EXCLUDED.thumb, updated_at=EXCLUDED.updated_at",
                                (canon, thumb, int(time.time())),
                            )
                            photo_rows[canon] = thumb
                        conn.execute("DELETE FROM performer_photos WHERE lower(name) = lower(%s)", (orig_name,))

        if "performer_bios" in tables:
            bio_rows = {r[0]: r[1:] for r in conn.execute("SELECT name, bio, birth, aliases, updated_at FROM performer_bios").fetchall()}
            for alias, canon in alias_map.items():
                for orig_name, (raw_bio, raw_birth, raw_aliases, raw_ts) in list(bio_rows.items()):
                    if orig_name.lower() == alias:
                        if canon not in bio_rows:
                            conn.execute(
                                "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at) VALUES (%s, %s, %s, %s, %s) "
                                "ON CONFLICT (name) DO UPDATE SET bio=EXCLUDED.bio, birth=EXCLUDED.birth, "
                                "aliases=EXCLUDED.aliases, updated_at=EXCLUDED.updated_at",
                                (canon, raw_bio, raw_birth, raw_aliases, raw_ts),
                            )
                            bio_rows[canon] = (raw_bio, raw_birth, raw_aliases, raw_ts)
                        conn.execute("DELETE FROM performer_bios WHERE lower(name) = lower(%s)", (orig_name,))

    return broken, flattened

def migrate_redundant_performers(conn: db.Connection, media_dir: Path, *,
                                 services: Optional[Any] = None,
                                 redundant_performers_version: int = REDUNDANT_PERFORMERS_VERSION) -> int:
    """Clean up redundant single-word sub-names and nationality-prefixed duplicates from DB and NFOs."""
    svc = _resolve_services(services)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'redundant_performers_version'").fetchone()
        if row and row[0] == str(redundant_performers_version):
            svc.logger.debug("migrate_redundant_performers: v%s already applied, skipping.", redundant_performers_version)
            return 0
    except Exception as _exc:
        logger.debug("Ignored exception in %s", "migrate_redundant_performers", exc_info=True)

    # Purge invalid stopwords/verbs and media files from metadata tables if present
    try:
        tables = db.table_names(conn)
        with conn:
            if "performer_aliases" in tables:
                alias_rows = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
                for a, c in alias_rows:
                    if not is_safe_alias(a, c):
                        conn.execute("DELETE FROM performer_aliases WHERE alias = %s", (a,))
            if "performer_bios" in tables:
                bio_rows = conn.execute("SELECT name FROM performer_bios").fetchall()
                for (n,) in bio_rows:
                    if not is_valid_performer_name(n):
                        conn.execute("DELETE FROM performer_bios WHERE name = %s", (n,))
            if "performer_photos" in tables:
                photo_rows = conn.execute("SELECT name FROM performer_photos").fetchall()
                for (n,) in photo_rows:
                    if not is_valid_performer_name(n):
                        conn.execute("DELETE FROM performer_photos WHERE name = %s", (n,))
            if "jellyfin_person_sync" in tables:
                sync_rows = conn.execute("SELECT name FROM jellyfin_person_sync").fetchall()
                for (n,) in sync_rows:
                    if not is_valid_performer_name(n):
                        conn.execute("DELETE FROM jellyfin_person_sync WHERE name = %s", (n,))
    except Exception as _exc:
        logger.debug("Ignored exception in %s", "migrate_redundant_performers", exc_info=True)

    # Correct known false performers for Cock Sure Men
    try:
        c_row = conn.execute("SELECT id, title, performers FROM videos WHERE title ILIKE %s LIMIT 1", ("%Cock Sure Men%",)).fetchone()
        if c_row:
            c_vid = c_row[0]
            target_c_perfs = ["Morgan Black", "Heath Jordan", "Conner Habib"]
            try:
                curr_perfs = json.loads(c_row[2] or "[]")
            except Exception:
                curr_perfs = []
            if curr_perfs != target_c_perfs:
                with conn:
                    conn.execute("UPDATE videos SET performers = %s WHERE id = %s", (json.dumps(target_c_perfs), c_vid))
    except Exception as e:
        svc.logger.warning("Failed to fix performers for Cock Sure Men: %s", e)

    # Correct known false performers for The Straight Man Part 4
    try:
        s_row = conn.execute("SELECT id, title, performers FROM videos WHERE title ILIKE %s LIMIT 1", ("%The Straight Man Part 4%",)).fetchone()
        if s_row:
            s_vid = s_row[0]
            target_s_perfs = ["Connor Halstead", "Eli Hunter", "Jared Summers"]
            try:
                curr_s_perfs = json.loads(s_row[2] or "[]")
            except Exception:
                curr_s_perfs = []
            if curr_s_perfs != target_s_perfs:
                with conn:
                    conn.execute("UPDATE videos SET performers = %s WHERE id = %s", (json.dumps(target_s_perfs), s_vid))
    except Exception as e:
        svc.logger.warning("Failed to fix performers for The Straight Man: %s", e)

    break_alias_cycles_and_flatten(conn)
    alias_rows = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall() if "performer_aliases" in tables else []
    alias_map = {str(a).strip().lower(): str(c).strip() for a, c in alias_rows if a and c}

    svc.logger.info("Checking database and NFO files for redundant performer cleanup...")
    rows = conn.execute(
        """SELECT id, provider, title, slug, duration, thumbnail,
                  performers, categories, tags, studio, first_seen
           FROM videos
           WHERE performers <> '[]'::jsonb"""
    ).fetchall()
    updated = 0
    for row in rows:
        vid_id, provider, title, slug, duration, thumb, perfs_raw, cats_raw, tags_raw, studio, first_seen = row
        try:
            perfs = json.loads(perfs_raw) if perfs_raw else []
        except Exception:
            perfs = []
        if not perfs:
            continue

        valid_perfs = [p for p in perfs if is_valid_performer_name(str(p))]
        aliased_perfs = [alias_map.get(str(p).strip().lower(), p) for p in valid_perfs]
        pruned = svc.prune_redundant_performers(aliased_perfs)
        if pruned == perfs:
            continue

        try:
            with conn:
                conn.execute("UPDATE videos SET performers = %s WHERE id = %s", (json.dumps(pruned), vid_id))

            nfo_path = media_dir / f"{svc.stable_media_basename(vid_id)}.nfo"
            if nfo_path.exists():
                try:
                    genres = set(json.loads(cats_raw)) if cats_raw else set()
                except Exception:
                    genres = set()
                try:
                    tags = set(json.loads(tags_raw)) if tags_raw else set()
                except Exception:
                    tags = set()
                pid = vid_id.split(":", 1)[-1] if ":" in vid_id else vid_id
                video = {
                    "id": vid_id,
                    "provider": provider,
                    "provider_video_id": pid,
                    "title": title or pid,
                    "slug": slug or "",
                    "duration": duration or 0,
                    "thumbnail": thumb or "",
                    "performers": pruned,
                    "performer_thumbs": svc.load_performer_thumbs(conn, pruned),
                    "studio": studio or "",
                    "dateadded": svc.format_dateadded(first_seen) if first_seen else "",
                }
                svc.create_nfo_file(video, genres, tags, nfo_path)
            updated += 1
        except Exception as e:
            svc.logger.warning("Failed to update performers for %s: %s", vid_id, e)

    try:
        with conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('redundant_performers_version', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (str(redundant_performers_version),),
            )
    except Exception as e:
        svc.logger.warning("Could not update redundant_performers_version in meta: %s", e)

    if updated > 0:
        svc.logger.info(f"Cleaned up redundant performers in {updated} videos and NFO files.")
    return updated
