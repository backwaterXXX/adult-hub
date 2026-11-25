"""Title-based performer enrichment for the sync database."""

import json
from common import db
from dataclasses import dataclass
from typing import Any, Callable
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PerformerEnrichmentServices:
    parse_title_performers: Callable
    stable_media_basename: Callable
    logger: Any


def backfill_performers_from_titles(conn: db.Connection, media_dir: Path, *, services) -> int:
    """Instant title-based performer enrichment for stored videos lacking cast.

    Runs across stored videos with empty performers (zero network overhead).
    Extracts performers from titles adhering to studio naming conventions
    ('P1, P2 & P3 – Title', 'Title – P1 & P2', 'P1 fucks P2') and cross-references
    single names against the DB's known performers dictionary. Returns count updated.
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
        if _apply_title_performers(conn, row, known_dict, services):
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


def _apply_title_performers(conn, row, known_dict: dict, services) -> bool:
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
        stored = True
        for n in names:
            k = n.strip().lower()
            if k not in known_dict:
                known_dict[k] = n.strip()
    except Exception as e:
        services.logger.warning("Title performer backfill store failed for %s: %s", vid_id, e)
    return stored


