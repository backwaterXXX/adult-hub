"""Caption-based title and StashDB performer matching for Telegram scenes."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

if __package__:
    from . import scene_scrapers as _core
else:
    import scene_scrapers as _core

def _load_official_stashdb_performers(conn: Any) -> list[dict]:
    """Load only active StashDB identities and their approved official aliases."""
    try:
        people = conn.execute(
            """SELECT id,name FROM performer_catalog
               WHERE source='stashdb' AND active=TRUE ORDER BY name"""
        ).fetchall()
        aliases = conn.execute(
            """SELECT a.performer_id,a.alias FROM performer_catalog_aliases a
               JOIN performer_catalog p ON p.id=a.performer_id
               WHERE p.source='stashdb' AND p.active=TRUE
                 AND a.origin='stashdb' AND a.approved=TRUE
               ORDER BY length(a.alias) DESC"""
        ).fetchall()
    except Exception as exc:
        _core.logger.debug("Official StashDB performer catalogue unavailable: %s", type(exc).__name__)
        return []

    catalog = {
        str(performer_id): {
            "id": str(performer_id), "name": str(name), "aliases": [],
            "trusted_aliases": [], "_persisted": True,
        }
        for performer_id, name in people
    }
    for performer_id, alias in aliases:
        record = catalog.get(str(performer_id))
        if record and str(alias or "").strip():
            record["aliases"].append(str(alias).strip())
    return list(catalog.values())


def _backfill_caption_title_matches(
    conn: Any,
    media_dir: Path,
    *,
    limit: int,
    sleep_sec: float,
    http_client: Optional[Any],
) -> Tuple[int, int]:
    """Use deterministic caption title extraction plus exact title/runtime lookup."""
    attempt_source = "telegram_caption_title_exact_v1"
    try:
        rows = conn.execute(
            """SELECT v.id,v.source_text,v.duration
               FROM videos v LEFT JOIN external_metadata_attempts a
                 ON a.entity_type='video' AND a.entity_key=v.id AND a.source=%s
               WHERE v.provider='telegram' AND v.source_text IS NOT NULL AND v.source_text<>''
                 AND COALESCE((v.performer_resolution->>'priority')::integer,0)<70
                 AND v.scene_match_external_id IS NULL
                 AND NOT (COALESCE(v.title,'') ~* '(part[[:space:]_.-]*|pt[.]?[[:space:]_.-]*|s[[:space:]_.-]*)[0-9]{1,2}'
                   OR COALESCE(v.source_filename,'') ~* '(part[[:space:]_.-]*|pt[.]?[[:space:]_.-]*|s[[:space:]_.-]*)[0-9]{1,2}'
                   OR COALESCE(v.source_text,'') ~* '(part[[:space:]_.-]*|pt[.]?[[:space:]_.-]*|s[[:space:]_.-]*)[0-9]{1,2}')
                 AND (a.entity_key IS NULL OR a.next_attempt_at<=CURRENT_TIMESTAMP)
               ORDER BY a.last_attempt_at ASC NULLS FIRST,v.first_seen ASC,v.id ASC LIMIT %s""",
            (attempt_source, max(1, int(limit))),
        ).fetchall()
    except Exception as exc:
        _core.logger.debug("Caption title query unavailable: %s", type(exc).__name__)
        return 0, 0
    if not rows:
        return 0, 0

    try:
        from common.telegram_metadata import extract_telegram_metadata
        if __package__:
            from .scene_scraper_text_sources import search_scene_by_exact_title
        else:
            from scene_scraper_text_sources import search_scene_by_exact_title
    except ImportError as exc:
        _core.logger.warning("Caption title lookup unavailable (%s)", type(exc).__name__)
        return 0, 0

    checked = 0
    updated = 0
    now = int(_core.time.time())
    for index, (video_id, source_text, duration) in enumerate(rows):
        if index and sleep_sec > 0:
            _core.time.sleep(sleep_sec)
        checked += 1
        metadata = extract_telegram_metadata(str(source_text or ""))
        title = str(metadata.get("title") or "").strip()
        try:
            scene = search_scene_by_exact_title(
                title, duration=int(duration or 0), http_client=http_client,
                duration_tolerance=2,
            ) if title else None
            if scene:
                with conn:
                    updated += int(_core._apply_matched_scene(
                        conn, media_dir, str(video_id), scene,
                        http_client=http_client, now=now,
                    ))
                outcome = "found"
            else:
                outcome = "no_match"
            if _core.record_attempt:
                _core.record_attempt(conn, "video", video_id, attempt_source, outcome)
        except Exception as exc:
            if _core.record_attempt:
                try:
                    _core.record_attempt(conn, "video", video_id, attempt_source, "error", error=exc)
                except Exception:
                    _core.logger.debug("Could not save title retry state for %s", video_id)
            _core.logger.warning("Caption title lookup failed for %s (%s)", video_id, type(exc).__name__)
    return checked, updated


def _backfill_stashdb_caption_matches(conn: Any, *, limit: int) -> Tuple[int, int]:
    """Resolve caption mentions only through unique, conservative official aliases.

    Existing Telegram performer fields are deliberately not used as evidence. A
    higher-priority hash/catalog resolution always wins; legacy model results
    are not used as matching evidence by this deterministic pass.
    """
    try:
        rows = conn.execute(
            """SELECT id,source_text,performer_resolution
               FROM videos
               WHERE provider='telegram' AND source_text IS NOT NULL AND source_text<>''
                 AND COALESCE((performer_resolution->>'priority')::integer,0) < 70
                 AND COALESCE(performer_resolution->>'method','') <> 'stashdb_caption_alias_v1'
               ORDER BY first_seen ASC,id ASC LIMIT %s""",
            (max(1, int(limit)),),
        ).fetchall()
    except Exception as exc:
        _core.logger.debug("Caption performer matching query unavailable: %s", type(exc).__name__)
        return 0, 0
    if not rows:
        return 0, 0

    try:
        if __package__:
            from common.performer_catalog import match_catalog, prepare_catalog
        else:
            from common.performer_catalog import match_catalog, prepare_catalog
        catalog = _load_official_stashdb_performers(conn)
        if not catalog:
            return 0, 0
        prepared = prepare_catalog(catalog)
    except Exception as exc:
        _core.logger.warning("Could not prepare official StashDB performer aliases (%s)", type(exc).__name__)
        return 0, 0

    checked = 0
    updated = 0
    for video_id, source_text, old_resolution in rows:
        checked += 1
        matches, _proposals = match_catalog(
            [("telegram_caption", str(source_text or "")[:12000])], catalog, prepared=prepared,
        )
        if isinstance(old_resolution, str):
            try:
                old_resolution = json.loads(old_resolution)
            except (TypeError, ValueError):
                old_resolution = {}
        old_resolution = old_resolution if isinstance(old_resolution, dict) else {}
        names = [str(row.get("name") or "").strip() for row in matches]
        names = list(dict.fromkeys(name for name in names if name))
        evidence = [{
            "performer_id": str(row.get("id") or ""),
            "name": str(row.get("name") or ""),
            "matched_alias": str(row.get("alias") or row.get("name") or ""),
        } for row in matches]
        resolution = {
            "status": "resolved" if names else "unresolved",
            "method": "stashdb_caption_alias_v1",
            "source": "stashdb_official_alias",
            "priority": 60,
            "performer_ids": [item["performer_id"] for item in evidence],
            "performer_names": names,
            "evidence": evidence,
            "previous_resolution": {
                "method": str(old_resolution.get("method") or ""),
                "source": str(old_resolution.get("source") or ""),
            } if old_resolution else None,
            "version": 1,
        }
        with conn:
            cursor = conn.execute(
                """UPDATE videos SET performer_resolution=%s::jsonb,
                       performers=CASE WHEN %s::boolean AND NOT EXISTS (
                           SELECT 1 FROM video_metadata_overrides o
                           WHERE o.video_id=videos.id AND o.fields ? 'performers'
                       ) THEN %s::jsonb ELSE performers END
                   WHERE id=%s AND COALESCE((performer_resolution->>'priority')::integer,0)<70
                     AND COALESCE(performer_resolution->>'method','') <> 'stashdb_caption_alias_v1'""",
                (json.dumps(resolution, ensure_ascii=False), bool(names),
                 json.dumps(names, ensure_ascii=False), video_id),
            )
            updated += int(getattr(cursor, "rowcount", 0) == 1 and bool(names))
    return checked, updated


