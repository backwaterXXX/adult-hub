"""Performer alias and metadata data migrations."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import logging

from common import db
from common.performers import is_safe_alias, is_valid_performer_name

logger = logging.getLogger(__name__)


REDUNDANT_PERFORMERS_VERSION = 7

# (table, select sql, delete sql) for name-keyed performer tables purged of invalid names.
_NAME_PURGE_QUERIES = (
    ("performer_bios", "SELECT name FROM performer_bios", "DELETE FROM performer_bios WHERE name = %s"),
    ("performer_photos", "SELECT name FROM performer_photos", "DELETE FROM performer_photos WHERE name = %s"),
    ("jellyfin_person_sync", "SELECT name FROM jellyfin_person_sync", "DELETE FROM jellyfin_person_sync WHERE name = %s"),
)


def _resolve_services(services: Optional[Any]) -> Any:
    if services is not None:
        return services
    if __package__:
        from .data_migrations import _resolve_default_services
    else:
        from data_migrations import _resolve_default_services
    return _resolve_default_services(None)


def _video_performer_counts(conn: db.Connection, tables) -> Dict[str, int]:
    video_counts: Dict[str, int] = defaultdict(int)
    if "videos" in tables:
        for (raw,) in conn.execute("SELECT performers FROM videos WHERE performers <> '[]'::jsonb").fetchall():
            try:
                for p in json.loads(raw):
                    video_counts[str(p).strip().lower()] += 1
            except Exception:
                logger.debug("Ignored exception in break_alias_cycles_and_flatten", exc_info=True)
    return video_counts


def _lowered_names(conn: db.Connection, sql: str) -> set:
    return {str(n).strip().lower() for (n,) in conn.execute(sql).fetchall()}


def _score_alias_name(name: str, video_counts, photos, bios) -> int:
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


def _break_two_cycles(conn: db.Connection, alias_map: Dict[str, str], score_name) -> int:
    broken = 0
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
    return broken


def _flatten_alias_chains(conn: db.Connection, alias_map: Dict[str, str]) -> int:
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
    return flattened


def _migrate_photo_rows(conn: db.Connection, alias_map: Dict[str, str]) -> None:
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


def _migrate_bio_rows(conn: db.Connection, alias_map: Dict[str, str]) -> None:
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


def break_alias_cycles_and_flatten(conn: db.Connection) -> Tuple[int, int]:
    """Break 2-cycles and flatten alias chains in performer_aliases table."""
    tables = db.table_names(conn)
    if "performer_aliases" not in tables:
        return 0, 0

    with conn:
        conn.execute("DELETE FROM performer_aliases WHERE lower(alias) = lower(canonical)")

    rows = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
    alias_map = {str(a).strip().lower(): str(c).strip() for a, c in rows if a and c}

    video_counts = _video_performer_counts(conn, tables)
    photos = _lowered_names(conn, "SELECT name FROM performer_photos") if "performer_photos" in tables else set()
    bios = _lowered_names(conn, "SELECT name FROM performer_bios") if "performer_bios" in tables else set()

    with conn:
        broken = _break_two_cycles(
            conn, alias_map,
            lambda name: _score_alias_name(name, video_counts, photos, bios),
        )
        flattened = _flatten_alias_chains(conn, alias_map)
        if "performer_photos" in tables:
            _migrate_photo_rows(conn, alias_map)
        if "performer_bios" in tables:
            _migrate_bio_rows(conn, alias_map)

    return broken, flattened


def _redundant_version_applied(conn: db.Connection, redundant_performers_version: int, svc: Any) -> bool:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'redundant_performers_version'").fetchone()
        if row and row[0] == str(redundant_performers_version):
            svc.logger.debug("migrate_redundant_performers: v%s already applied, skipping.", redundant_performers_version)
            return True
    except Exception:
        logger.debug("Ignored exception in migrate_redundant_performers", exc_info=True)
    return False


def _purge_invalid_aliases(conn: db.Connection) -> None:
    alias_rows = conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
    for a, c in alias_rows:
        if not is_safe_alias(a, c):
            conn.execute("DELETE FROM performer_aliases WHERE alias = %s", (a,))


def _purge_invalid_names(conn: db.Connection, select_sql: str, delete_sql: str) -> None:
    for (n,) in conn.execute(select_sql).fetchall():
        if not is_valid_performer_name(n):
            conn.execute(delete_sql, (n,))


def _purge_invalid_metadata(conn: db.Connection, tables) -> None:
    """Purge invalid stopwords/verbs and media files from metadata tables if present."""
    with conn:
        if "performer_aliases" in tables:
            _purge_invalid_aliases(conn)
        for table, select_sql, delete_sql in _NAME_PURGE_QUERIES:
            if table in tables:
                _purge_invalid_names(conn, select_sql, delete_sql)


def _restore_known_performers(conn: db.Connection, svc: Any, title_pattern: str,
                              target_perfs: list, label: str) -> None:
    """Set the performer list of one known-bad video title to its corrected list."""
    try:
        row = conn.execute("SELECT id, title, performers FROM videos WHERE title ILIKE %s LIMIT 1", (title_pattern,)).fetchone()
        if row:
            vid = row[0]
            try:
                curr_perfs = json.loads(row[2] or "[]")
            except Exception:
                curr_perfs = []
            if curr_perfs != target_perfs:
                with conn:
                    conn.execute("UPDATE videos SET performers = %s WHERE id = %s", (json.dumps(target_perfs), vid))
    except Exception as e:
        svc.logger.warning("Failed to fix performers for %s: %s", label, e)


def _json_set(raw) -> set:
    try:
        return set(json.loads(raw)) if raw else set()
    except Exception:
        return set()


def _nfo_genres_and_tags(cats_raw, tags_raw) -> Tuple[set, set]:
    genres = _json_set(cats_raw)
    tags = _json_set(tags_raw)
    return genres, tags


def _write_pruned_nfo(conn: db.Connection, svc: Any, media_dir: Path, row, pruned) -> None:
    """Rewrite the NFO of one video with its pruned performer list, when the NFO exists."""
    vid_id, provider, title, slug, duration, thumb, _perfs_raw, cats_raw, tags_raw, studio, first_seen = row
    nfo_path = media_dir / f"{svc.stable_media_basename(vid_id)}.nfo"
    if not nfo_path.exists():
        return
    genres, tags = _nfo_genres_and_tags(cats_raw, tags_raw)
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


def _apply_pruned_performers(conn: db.Connection, svc: Any, media_dir: Path, row, pruned) -> None:
    vid_id = row[0]
    with conn:
        conn.execute("UPDATE videos SET performers = %s WHERE id = %s", (json.dumps(pruned), vid_id))
    _write_pruned_nfo(conn, svc, media_dir, row, pruned)


def _record_redundant_version(conn: db.Connection, redundant_performers_version: int, svc: Any) -> None:
    try:
        with conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('redundant_performers_version', %s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
                (str(redundant_performers_version),),
            )
    except Exception as e:
        svc.logger.warning("Could not update redundant_performers_version in meta: %s", e)


def _performer_list(perfs_raw) -> list:
    try:
        return json.loads(perfs_raw) if perfs_raw else []
    except Exception:
        return []


def migrate_redundant_performers(conn: db.Connection, media_dir: Path, *,
                                 services: Optional[Any] = None,
                                 redundant_performers_version: int = REDUNDANT_PERFORMERS_VERSION) -> int:
    """Clean up redundant single-word sub-names and nationality-prefixed duplicates from DB and NFOs."""
    svc = _resolve_services(services)
    if _redundant_version_applied(conn, redundant_performers_version, svc):
        return 0

    try:
        tables = db.table_names(conn)
        _purge_invalid_metadata(conn, tables)
    except Exception:
        logger.debug("Ignored exception in migrate_redundant_performers", exc_info=True)

    # Correct known false performers
    _restore_known_performers(conn, svc, "%Cock Sure Men%",
                              ["Morgan Black", "Heath Jordan", "Conner Habib"], "Cock Sure Men")
    _restore_known_performers(conn, svc, "%The Straight Man Part 4%",
                              ["Connor Halstead", "Eli Hunter", "Jared Summers"], "The Straight Man")

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
        perfs = _performer_list(row[6])
        if not perfs:
            continue

        valid_perfs = [p for p in perfs if is_valid_performer_name(str(p))]
        aliased_perfs = [alias_map.get(str(p).strip().lower(), p) for p in valid_perfs]
        pruned = svc.prune_redundant_performers(aliased_perfs)
        if pruned == perfs:
            continue

        try:
            _apply_pruned_performers(conn, svc, media_dir, row, pruned)
            updated += 1
        except Exception as e:
            svc.logger.warning("Failed to update performers for %s: %s", row[0], e)

    _record_redundant_version(conn, redundant_performers_version, svc)

    if updated > 0:
        svc.logger.info(f"Cleaned up redundant performers in {updated} videos and NFO files.")
    return updated
