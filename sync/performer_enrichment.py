"""Title-based performer enrichment for the sync database."""

import json
from common import db
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PerformerEnrichmentServices:
    parse_title_performers: Callable
    load_performer_thumbs: Callable
    format_dateadded: Callable
    stable_media_basename: Callable
    create_nfo_file: Callable
    logger: Any


def _regenerate_performer_nfo(
    conn, media_dir, vid_id, provider, title, slug, duration, thumbnail,
    categories, tags, performers, services,
):
    try:
        genres = set(json.loads(categories)) if categories else set()
    except Exception:
        genres = set()
    try:
        tag_set = set(json.loads(tags)) if tags else set()
    except Exception:
        tag_set = set()

    provider_video_id = vid_id.split(":", 1)[-1]
    video = {
        "id": vid_id,
        "provider": provider,
        "provider_video_id": provider_video_id,
        "title": title or provider_video_id,
        "slug": slug or "",
        "duration": duration or 0,
        "thumbnail": thumbnail or "",
        "performers": performers,
        "performer_thumbs": services.load_performer_thumbs(conn, performers),
        "studio": "",
        "dateadded": "",
    }
    try:
        extra = conn.execute(
            "SELECT studio, first_seen FROM videos WHERE id = %s", (vid_id,)
        ).fetchone()
    except Exception:
        extra = None
    if extra:
        if extra[0]:
            video["studio"] = extra[0]
        if extra[1]:
            video["dateadded"] = services.format_dateadded(extra[1])

    nfo_path = media_dir / f"{services.stable_media_basename(vid_id)}.nfo"
    services.create_nfo_file(video, genres, tag_set, nfo_path, conn=conn)


def backfill_performers_from_titles(conn: db.Connection, media_dir: Path, *, services) -> int:
    """Instant title-based performer enrichment for stored videos lacking cast.

    Runs across stored videos with empty performers (zero network overhead).
    Extracts performers from titles adhering to studio naming conventions
    ('P1, P2 & P3 – Title', 'Title – P1 & P2', 'P1 fucks P2') and cross-references
    single names against the DB's known performers dictionary.
    Updates DB and regenerates corresponding .nfo files. Returns count updated.
    """
    try:
        known_dict = _known_performer_names(conn)
        rows = conn.execute(
            """SELECT id, provider, title, slug, duration, thumbnail,
                      performers, categories, tags
               FROM videos
               WHERE performers IS NULL OR performers = '[]'::jsonb"""
        ).fetchall()
    except Exception as e:
        services.logger.warning("Title performer backfill query failed: %s", e)
        return 0

    updated = 0
    for row in rows:
        if _apply_title_performers(conn, media_dir, row, known_dict, services):
            updated += 1

    if updated:
        services.logger.info("Title performer backfill: %s videos enriched from titles", updated)
    return updated


def _known_performer_names(conn) -> dict:
    """Map lower-cased performer names and aliases to their display spelling."""
    known_rows = conn.execute(
        "SELECT performers FROM videos WHERE performers <> '[]'::jsonb"
    ).fetchall()
    known_dict = {}
    for (raw,) in known_rows:
        try:
            items = json.loads(raw) if isinstance(raw, str) else []
            for n in items:
                name_str = str(n or "").strip()
                if name_str:
                    k = name_str.lower()
                    if k not in known_dict:
                        known_dict[k] = name_str
        except Exception:
            logger.debug("Ignored exception in _known_performer_names", exc_info=True)
    try:
        for alias, canonical in conn.execute(
            "SELECT alias, canonical FROM performer_aliases"
        ).fetchall():
            alias_key = str(alias or "").strip().lower()
            canonical_name = str(canonical or "").strip()
            if alias_key and canonical_name:
                known_dict[alias_key] = canonical_name
    except db.Error:
        pass
    return known_dict


def _apply_title_performers(conn, media_dir: Path, row, known_dict: dict, services) -> bool:
    """Store performers parsed from one video title. Returns True when the row was stored."""
    vid_id, provider, title, slug, duration, thumb, _perf, cats, tags = row
    names = services.parse_title_performers(title or "", known_performers=known_dict)
    if not names:
        return False
    stored = False
    try:
        conn.execute("UPDATE videos SET performers = %s WHERE id = %s",
                     (json.dumps(names), vid_id))
        conn.commit()
        _regenerate_performer_nfo(
            conn, media_dir, vid_id, provider, title, slug, duration,
            thumb, cats, tags, names, services,
        )
        stored = True
        for n in names:
            k = n.strip().lower()
            if k not in known_dict:
                known_dict[k] = n.strip()
    except Exception as e:
        services.logger.warning("Title performer backfill store failed for %s: %s", vid_id, e)
    return stored


