from __future__ import annotations

import json
import logging
import os
from common import db
from common.duration import (
    MIN_VIDEO_DURATION_SECONDS,
    passes_minimum_video_duration,
)
from common.performer_catalog import normalize_name
from common.video_metadata import load_metadata_state
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


_WARNED_MISSING_CHANNEL_HINTS: bool = False


def reset_channel_hints_warning() -> None:
    """Reset the channel hints warning latch (primarily for test isolation)."""
    global _WARNED_MISSING_CHANNEL_HINTS
    _WARNED_MISSING_CHANNEL_HINTS = False


def _is_telegram_configured() -> bool:
    """Return True if Telegram is configured as an active or enabled source."""
    try:
        from common.config_loader import load_config
        cfg = load_config()
        if cfg:
            tg = cfg.get_source("telegram")
            if tg and getattr(tg, "enabled", True):
                channels = getattr(tg, "channels", [])
                if channels and any(getattr(ch, "enabled", True) for ch in channels):
                    return True
    except Exception:
        logger.debug("Ignored exception in _is_telegram_configured", exc_info=True)
    if os.getenv("TELEGRAM_API_ID") and (
        os.getenv("TELEGRAM_CHANNELS") or os.getenv("SYNC_TELEGRAM", "").lower() in ("true", "1", "yes")
    ):
        return True
    return False


def warn_if_channel_hints_missing(
    logger: Optional[logging.Logger] = None, *, is_configured: Optional[bool] = None
) -> bool:
    """Emit a once-per-startup warning if Telegram is configured but channel hints are empty.

    Returns True if a warning was emitted, False otherwise.
    Does not log private channel names or repeat per video.
    """
    global _WARNED_MISSING_CHANNEL_HINTS
    if _WARNED_MISSING_CHANNEL_HINTS:
        return False
    hints = (os.getenv("TELEGRAM_CHANNEL_NAME_HINTS") or "").strip()
    if hints:
        return False
    tg_configured = _is_telegram_configured() if is_configured is None else is_configured
    if not tg_configured:
        return False
    _WARNED_MISSING_CHANNEL_HINTS = True
    log = logger or logging.getLogger("sync.video_persistence")
    log.warning(
        "Telegram is configured but TELEGRAM_CHANNEL_NAME_HINTS is empty; "
        "channel titles may be misclassified as production studios."
    )
    return True


@dataclass
class VideoPersistenceServices:
    stable_media_basename: Callable[[str], str]
    map_categories: Callable[..., Set[str]]
    normalize_performers: Callable[[List[str]], List[str]]
    thumbnail_urls: Callable[[Optional[str]], Tuple[str, str]]
    load_performer_thumbs: Callable[[db.Connection, List[str]], Dict[str, str]]
    format_dateadded: Callable[[Any], str]
    create_nfo_file: Callable[[Dict[str, Any], Set[str], Set[str], Path], None]
    download_image: Callable[[str, Path], None]
    write_stream_file: Callable[[Path, str], bool]
    sync_unique_video: Callable[..., Any]
    persist_prepared_video: Callable[[db.Connection, Dict[str, Any]], None]
    logger: logging.Logger
    time_fn: Callable[[], float] = time.time
    load_performer_links: Optional[Callable[[db.Connection, List[str]], Dict[str, Dict[str, str]]]] = None

    def __post_init__(self) -> None:
        warn_if_channel_hints_missing(self.logger)


def _merge_video_fields(existing: Dict[str, Any], new: Dict[str, Any]) -> None:
    """Union performers/categories from a duplicate sighting (order-preserving).

    The same video can surface in several sections/categories with partial
    metadata each time; merging avoids losing performers found elsewhere.
    """
    for key in ("performers", "categories"):
        old = existing.get(key) or []
        if not isinstance(old, list):
            old = [old]
        seen = set(old)
        merged = list(old)
        new_vals = new.get(key) or []
        if not isinstance(new_vals, list):
            new_vals = [new_vals]
        for v in new_vals:
            if v and v not in seen:
                seen.add(v)
                merged.append(v)
        if len(merged) != len(old):
            existing[key] = merged
    for key in ("source_text", "source_filename"):
        if not existing.get(key) and new.get(key):
            existing[key] = new[key]
    old_proposals = existing.get("performer_proposals") or []
    proposal_keys = {(p.get("matched_text"), p.get("reason")) for p in old_proposals}
    for proposal in new.get("performer_proposals") or []:
        key = (proposal.get("matched_text"), proposal.get("reason"))
        if key not in proposal_keys:
            old_proposals.append(proposal)
            proposal_keys.add(key)
    if old_proposals:
        existing["performer_proposals"] = old_proposals
    new_resolution = new.get("performer_resolution") or {}
    old_resolution = existing.get("performer_resolution") or {}
    if new_resolution and int(new_resolution.get("priority") or 0) >= int(old_resolution.get("priority") or -1):
        existing["performer_resolution"] = new_resolution
        existing["performers"] = new.get("performers") or []


def persist_prepared_video(
    conn: db.Connection,
    prepared: Dict[str, Any],
    time_fn: Callable[[], float] = time.time,
) -> None:
    """Persist one media-prepared video inside the caller's DB transaction."""
    if not passes_minimum_video_duration((prepared.get("video") or {}).get("duration")):
        raise ValueError("Videos shorter than 420 seconds cannot be saved to the catalog")
    comp_id = prepared["comp_id"]
    provider = prepared["provider"]
    video = prepared["video"]
    mapped_genres = prepared["genres"]
    tags = prepared.get("tags") or []
    now = int(time_fn())
    clean_tags = []
    for t in tags:
        st = str(t).strip()
        if st and st.lower() not in ("telegram", "t.me") and not st.startswith("-100"):
            clean_tags.append(st)
    tags_json = json.dumps(sorted(set(clean_tags)))

    conn.execute("""
        INSERT INTO videos (id, provider, title, slug, duration, thumbnail, performers, categories, tags, embed_host, playable, studio, first_seen, last_seen, source_text, source_filename, performer_resolution)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title, duration=excluded.duration,
            thumbnail=excluded.thumbnail,
            performers=CASE WHEN excluded.performer_resolution IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM video_metadata_overrides o
                        WHERE o.video_id=videos.id AND o.fields ? 'performers'
                    )
                    AND COALESCE((excluded.performer_resolution->>'priority')::integer,0)
                        >= COALESCE((videos.performer_resolution->>'priority')::integer,-1)
                THEN excluded.performers
                WHEN excluded.performers IS NULL OR excluded.performers = '[]'::jsonb
                THEN videos.performers ELSE excluded.performers END,
            performer_resolution=CASE WHEN EXISTS (
                    SELECT 1 FROM video_metadata_overrides o
                    WHERE o.video_id=videos.id AND o.fields ? 'performers'
                ) THEN videos.performer_resolution
                WHEN excluded.performer_resolution IS NOT NULL
                     AND COALESCE((excluded.performer_resolution->>'priority')::integer,0)
                         >= COALESCE((videos.performer_resolution->>'priority')::integer,-1)
                THEN excluded.performer_resolution ELSE videos.performer_resolution END,
            categories=excluded.categories, tags=excluded.tags,
            embed_host=COALESCE(NULLIF(excluded.embed_host, ''), videos.embed_host),
            playable=COALESCE(excluded.playable, videos.playable),
            studio=COALESCE(NULLIF(excluded.studio, ''), videos.studio),
            last_seen=excluded.last_seen,
            source_text=CASE WHEN excluded.source_text <> '' THEN excluded.source_text ELSE videos.source_text END,
            source_filename=CASE WHEN excluded.source_filename <> '' THEN excluded.source_filename ELSE videos.source_filename END
    """, (comp_id, provider, prepared["title"], video.get("slug", ""),
          video.get("duration", 0), video.get("thumbnail"),
          json.dumps(video.get("performers", [])),
          json.dumps(sorted(mapped_genres)), tags_json,
          video.get("embed_host", ""), video.get("playable"),
          prepared["studio"] or None, now, now,
          video.get("source_text") or video.get("description") or "",
          video.get("source_filename") or "",
          json.dumps(video.get("performer_resolution"), ensure_ascii=False)
          if video.get("performer_resolution") is not None else None))
    for proposal in video.get("performer_proposals") or []:
        candidate = str(proposal.get("candidate") or "").strip()
        normalized = normalize_name(candidate)
        if not normalized:
            continue
        conn.execute("""
            INSERT INTO telegram_performer_proposals
                (video_id,candidate,normalized,matched_text,source_field,reason,performer_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(video_id,normalized) DO UPDATE SET
                matched_text=EXCLUDED.matched_text, source_field=EXCLUDED.source_field,
                reason=EXCLUDED.reason, performer_id=EXCLUDED.performer_id
            WHERE telegram_performer_proposals.status='pending'
        """, (comp_id, candidate, normalized, str(proposal.get("matched_text") or ""),
              str(proposal.get("source_field") or ""), str(proposal.get("reason") or ""),
              proposal.get("performer_id")))
    conn.execute("DELETE FROM video_categories WHERE video_id = %s", (comp_id,))
    for genre in mapped_genres:
        if genre and not genre.startswith("Browse"):
            conn.execute("INSERT INTO video_categories (video_id, category) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                         (comp_id, genre))


def register_stream_file(conn: db.Connection, prepared: Dict[str, Any],
                         time_fn: Callable[[], float] = time.time) -> None:
    """Record a stream that is already on disk, so cleanup can track its files.

    Registration happens after publication and never before: an entry whose
    sidecars could not be written stays unregistered, which keeps
    ``cleanup_orphaned_records`` from reading a publication failure as an
    ordinary missing file and deleting the catalog row along with the entry.
    """
    conn.execute("""INSERT INTO synced_files (file_path, video_id, category, created_at)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (file_path) DO UPDATE SET
            video_id = EXCLUDED.video_id,
            category = EXCLUDED.category,
            created_at = EXCLUDED.created_at""", (prepared["strm_file"].name,
                                 prepared["comp_id"],
                                 ", ".join(sorted(prepared.get("genres") or [])),
                                 int(time_fn())))


def sync_unique_video(
    video: Dict[str, Any],
    genres: Set[str],
    tags: Set[str],
    target_dir: Path,
    conn: db.Connection,
    commit: bool = True,
    *,
    _prepare_only: bool = False,
    services: VideoPersistenceServices,
) -> Optional[Dict[str, Any]]:
    target_dir.mkdir(parents=True, exist_ok=True)
    vid_id = video.get("provider_video_id") or video.get("id", "").split(":")[-1]
    provider = video.get("provider", "telegram")
    comp_id = f"{provider}:{vid_id}"

    title = video.get("title", vid_id)
    base_name = services.stable_media_basename(comp_id)

    strm_file = target_dir / f"{base_name}.strm"
    nfo_file = target_dir / f"{base_name}.nfo"
    jpg_file = target_dir / f"{base_name}.jpg"

    mapped_genres = services.map_categories(genres, title=title)

    # Studio (per-source override) i data dodania: first_seen z DB dla starych,
    # teraz dla nowych. Backfilluje tez kolumne studio.
    now = int(services.time_fn())
    try:
        _row = conn.execute(
            """SELECT v.first_seen, v.studio, v.performers, v.plot, v.date_released,
                      v.performer_resolution,
                      EXISTS(SELECT 1 FROM video_metadata_overrides o
                             WHERE o.video_id=v.id AND o.fields ? 'performers'),
                      v.title, v.scene_match_source, v.scene_match_external_id,
                      v.scene_match_url,
                      EXISTS(SELECT 1 FROM video_metadata_overrides o
                             WHERE o.video_id=v.id AND o.fields ? 'title')
               FROM videos v WHERE v.id = %s""", (comp_id,)).fetchone()
    except Exception:
        try:
            _row = conn.execute(
                "SELECT first_seen, studio, performers FROM videos WHERE id = %s", (comp_id,)).fetchone()
        except Exception:
            _row = None
    first_seen = _row[0] if _row and _row[0] else now
    studio_val = (video.get("studio") or "").strip()
    if not studio_val and _row and len(_row) > 1 and _row[1]:
        studio_val = _row[1]
    if _row and len(_row) > 3 and _row[3] and not video.get("plot"):
        video["plot"] = _row[3]
    if _row and len(_row) > 4 and _row[4] and not video.get("date_released"):
        video["date_released"] = _row[4]

    # Tytul: potwierdzone dopasowanie sceny StashDB/ThePornDB jest kanonicznym
    # zrodlem tytulu (jak w _apply_matched_scene). Swiezy listing z Telegrama
    # nie moze go cofnac do podpisu kanalu — inaczej NFO (a w nim nazwa
    # widoczna w Jellyfin) traci tytul katalogowy przy kazdym przebiegu.
    # Jawne nadpisanie operatora ma pierwszenstwo.
    provider_title = title
    catalog_title = ""
    if _row and len(_row) > 7 and _row[7]:
        catalog_title = str(_row[7]).strip()
    scene_source = str(_row[8] or "").casefold() if _row and len(_row) > 8 else ""
    has_catalog_match = any(
        provider in scene_source for provider in ("stashdb", "theporndb", "tpdb")
    )
    title_overridden = bool(_row and len(_row) > 11 and _row[11])
    if has_catalog_match and _row and len(_row) > 9 and _row[9]:
        # Przenies proweniencje dopasowania do NFO, aby przebieg zapisu nie
        # usuwal identyfikatora katalogowego z pliku.
        video["scene_match_source"] = _row[8]
        video["scene_match_external_id"] = _row[9]
        if len(_row) > 10 and _row[10]:
            video["scene_match_url"] = _row[10]
    if has_catalog_match and catalog_title and not title_overridden:
        title = catalog_title
        # Zachowaj podpis dostawcy w <originaltitle> jako proweniencje.
        video["original_title"] = provider_title
        video["title"] = catalog_title

    # Obsada: swiezy listing wygrywa, ale pusty NIE kasuje tego, co juz mamy
    # w DB (backfill/matcher) — inaczej NFO traciloby obsade do kolejnego backfillu.
    _fresh_perfs = services.normalize_performers(video.get("performers", []))
    effective_resolution = video.get("performer_resolution")
    if video.get("performer_resolution"):
        _fresh_perfs = list(dict.fromkeys(
            str(value).strip() for value in video.get("performers", []) if str(value).strip()
        ))
    if _row and len(_row) > 6 and _row[5]:
        db_resolution = _row[5]
        if isinstance(db_resolution, str):
            try:
                db_resolution = json.loads(db_resolution)
            except (TypeError, ValueError):
                db_resolution = {}
        incoming_priority = int((effective_resolution or {}).get("priority") or -1)
        stored_priority = int(db_resolution.get("priority") or -1) if isinstance(db_resolution, dict) else -1
        if stored_priority > incoming_priority:
            effective_resolution = db_resolution
            if _row[2]:
                try:
                    _db_perfs = json.loads(_row[2]) if isinstance(_row[2], str) else _row[2]
                    _fresh_perfs = list(dict.fromkeys(
                        str(value).strip() for value in _db_perfs if str(value).strip()
                    ))
                except Exception:
                    logger.debug("Ignored exception in sync_unique_video", exc_info=True)
    if _row and len(_row) > 6 and _row[6] and _row[2]:
        try:
            _db_perfs = json.loads(_row[2]) if isinstance(_row[2], str) else _row[2]
            _fresh_perfs = services.normalize_performers(_db_perfs)
        except Exception:
            logger.debug("Ignored exception in sync_unique_video", exc_info=True)
    elif not video.get("performer_resolution") and not _fresh_perfs and _row and len(_row) > 2 and _row[2]:
        try:
            _db_perfs = json.loads(_row[2]) if isinstance(_row[2], str) else []
            _fresh_perfs = services.normalize_performers(_db_perfs)
        except Exception:
            logger.debug("Ignored exception in sync_unique_video", exc_info=True)

    # Normalize performers once so NFO and DB stay consistent (deduped order).
    _perfs = _fresh_perfs
    public_thumb_url, download_thumb_url = services.thumbnail_urls(video.get("thumbnail"))
    perf_links = {}
    if getattr(services, "load_performer_links", None):
        try:
            perf_links = services.load_performer_links(conn, _perfs)
        except Exception:
            logger.debug("Ignored exception in sync_unique_video", exc_info=True)
    video = {**video, "_video_id": comp_id, "performers": _perfs, "thumbnail": public_thumb_url,
             "performer_thumbs": services.load_performer_thumbs(conn, _perfs),
             "performer_links": perf_links,
             "studio": studio_val, "dateadded": services.format_dateadded(first_seen),
             "performer_resolution": effective_resolution}

    # Detect production studios and add as tags and production_studios
    prod_names = []
    try:
        from studio_tags import detect_production_studios, detect_production_studio_names
        detected_studios = detect_production_studios(title, tags)
        if detected_studios:
            tags = set(tags) | set(detected_studios)
        prod_names = detect_production_studio_names(title, tags)
        if prod_names:
            video["production_studios"] = prod_names
    except Exception:
        logger.debug("Ignored exception in sync_unique_video", exc_info=True)
    # For Telegram, distinguish actual production studios from source channel names.
    # Deployment-specific channel titles can be supplied through the environment
    # instead of being hardcoded in the public repository.
    if provider == "telegram":
        warn_if_channel_hints_missing(services.logger if services else None, is_configured=True)
    extra_channel_hints = tuple(
        hint.strip().casefold()
        for hint in (os.getenv("TELEGRAM_CHANNEL_NAME_HINTS") or "").split(",")
        if hint.strip()
    )
    is_tg_chan = bool(studio_val and (
        studio_val.lower() in ("telegram", "t.me", "telegram (t.me)")
        or studio_val.startswith("-100")
        or (studio_val.startswith("-") and studio_val[1:].isdigit())
        or any(k in studio_val.lower() for k in extra_channel_hints)
    ))
    if is_tg_chan:
        video["channel"] = studio_val
        video["studio"] = prod_names[0] if prod_names else ""
    elif prod_names:
        video["studio"] = prod_names[0]

    db_studio = prod_names[0] if prod_names else (None if (provider == "telegram" and is_tg_chan) else (studio_val or None))

    clean_tags = set()
    for t in tags:
        st = str(t).strip()
        if st and st.lower() not in ("telegram", "t.me") and not st.startswith("-100"):
            clean_tags.add(st)
    tags = clean_tags

    prepared = {
        "comp_id": comp_id, "provider": provider, "title": title,
        "video": video, "genres": mapped_genres, "tags": tags,
        "strm_file": strm_file, "studio": db_studio,
        "nfo_file": nfo_file, "jpg_file": jpg_file, "thumb_url": download_thumb_url,
    }

    if _prepare_only:
        return prepared

    # Record into DB with relative file path and composite video_id.
    try:
        services.persist_prepared_video(conn, prepared)
        if commit:
            conn.commit()
    except Exception:
        if commit:
            try:
                conn.rollback()
            except Exception:
                logger.debug("Ignored exception in sync_unique_video", exc_info=True)
        raise
    # The catalog row is durable; publish sidecars (NFO first so the library
    # scanner sees metadata before a new stream file becomes visible).
    write_media_files(services, prepared, conn, commit=commit)
    return None


def write_media_files(services: VideoPersistenceServices, prepared: Dict[str, Any],
                      conn: db.Connection, *, commit: bool = True) -> None:
    """Publish NFO, thumbnail and STRM, then register the stream that exists."""
    services.create_nfo_file(prepared["video"], prepared["genres"], prepared["tags"], prepared["nfo_file"])
    thumb_url = prepared.get("thumb_url")
    if thumb_url and not prepared["jpg_file"].exists():
        services.download_image(thumb_url, prepared["jpg_file"])
    services.write_stream_file(prepared["strm_file"], prepared["comp_id"])
    register_stream_file(conn, prepared, time_fn=services.time_fn)
    if commit:
        conn.commit()


def flush_collected(
    collected: Dict[str, Dict[str, Any]],
    conn: db.Connection,
    media_dir: Path,
    source_name: str = "",
    *,
    raise_on_error: bool = False,
    services: VideoPersistenceServices,
) -> int:
    """Flush collected videos to DB and media_dir, merging previously known genres and tags from DB.
    Clears the dictionary after writing to free memory and prevent re-writing."""
    if not collected:
        return 0
    short_ids = [
        video_id for video_id, item in collected.items()
        if not passes_minimum_video_duration((item.get("video") or {}).get("duration"))
    ]
    for video_id in short_ids:
        collected.pop(video_id, None)
    if short_ids:
        services.logger.info("Skipped %d video(s) shorter than 420 seconds%s",
                             len(short_ids), f" ({source_name})" if source_name else "")
    if not collected:
        return 0
    try:
        hidden = {r[0] for r in conn.execute(
            "SELECT loser_id FROM video_duplicate_decisions WHERE state='active'")}
        if hidden:
            deliberately_skipped = [vid for vid in collected if vid in hidden]
            for vid in deliberately_skipped:
                collected.pop(vid, None)
            if deliberately_skipped:
                services.logger.info("Skipped %d intentionally hidden duplicate reference(s)%s",
                                    len(deliberately_skipped), f" ({source_name})" if source_name else "")
    except db.OperationalError:
        # Compatible with old catalogs until init_db has run its migration.
        pass
    if not collected:
        return 0
    count = len(collected)
    label = f" ({source_name})" if source_name else ""
    services.logger.info(f"Writing {count} videos{label} to {media_dir} and DB...")

    errors = []
    # Merge previously known genres and tags from DB so we don't lose tags across syncs
    for comp_id, item in collected.items():
        try:
            cur = conn.execute("SELECT categories, tags FROM videos WHERE id = %s", (comp_id,))
            row = cur.fetchone()
            if row:
                if row[0]:
                    item["genres"].update(services.map_categories(json.loads(row[0])))
                if row[1]:
                    db_tags = [t for t in json.loads(row[1]) if str(t).lower().strip() not in ("telegram", "t.me") and not str(t).strip().startswith("-100")]
                    item["tags"].update(db_tags)
        except Exception as e:
            errors.append(f"{comp_id} metadata read: {type(e).__name__}: {e}")
            services.logger.error("Failed to read existing metadata for %s: %s", comp_id, e)

    prepared_items = []
    for comp_id, item in collected.items():
        try:
            prepared = services.sync_unique_video(
                video=item["video"],
                genres=item["genres"],
                tags=item["tags"],
                target_dir=media_dir,
                conn=conn,
                commit=False,
                _prepare_only=True,
            )
            if prepared is not None:
                prepared_items.append(prepared)
        except Exception as e:
            errors.append(f"{comp_id}: {type(e).__name__}: {e}")
            services.logger.error(f"Failed to write video {comp_id}: {e}")

    written = 0
    committed_items: List[Dict[str, Any]] = []
    try:
        # Finish any caller-owned setup writes before opening the media batch.
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN")
        for prepared in prepared_items:
            services.persist_prepared_video(conn, prepared)
            written += 1
        conn.commit()
        committed_items = prepared_items
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            logger.debug("Ignored exception in flush_collected", exc_info=True)
        errors.append(f"database batch: {type(e).__name__}: {e}")
        services.logger.warning(f"Database batch failed: {e}")
        written = 0

    # Publish sidecars only after the catalog batch is durable, so a rolled-back
    # batch cannot leave unmanaged .strm/.nfo files in the library. A failed
    # publication leaves the entry registered as nothing at all: its catalog row
    # survives, the library never sees a half-written item, and
    # republish_pending_videos() retries it from the database on the next pass.
    for prepared in committed_items:
        try:
            write_media_files(services, prepared, conn)
        except Exception as e:
            errors.append(f"{prepared['comp_id']} file write: {type(e).__name__}: {e}")
            services.logger.error("Failed to publish files for %s: %s", prepared["comp_id"], e)

    if errors:
        services.logger.warning("Saved %d/%d videos%s; there were %d write/commit errors.", written, count, label, len(errors))
        if raise_on_error:
            raise RuntimeError("Catalog persistence incomplete: " + "; ".join(errors[:5]))
    else:
        services.logger.info(f"Successfully saved {written}/{count} videos{label} to {media_dir} and DB.")
    collected.clear()
    return written


def publication_pending_ids(conn: db.Connection, limit: int) -> List[str]:
    """Catalog rows that were persisted but never published to the library.

    A ``videos`` row without a ``synced_files`` registration is only produced by
    a failed or interrupted publication: every successful pass registers its
    stream right after writing it. Deliberately hidden duplicates are excluded
    so a retry can never resurrect a merged-away entry.
    """
    rows = conn.execute("""
        SELECT v.id FROM videos v
        WHERE v.duration >= %s
          AND NOT EXISTS (SELECT 1 FROM synced_files sf WHERE sf.video_id = v.id)
          AND NOT EXISTS (SELECT 1 FROM video_duplicate_decisions d
                          WHERE d.loser_id = v.id AND d.state = 'active')
        ORDER BY v.id
        LIMIT %s""", (MIN_VIDEO_DURATION_SECONDS, max(1, int(limit)))).fetchall()
    return [str(row[0]) for row in rows]


def prepared_from_catalog(comp_id: str, media_dir: Path, conn: db.Connection,
                          services: VideoPersistenceServices) -> Optional[Dict[str, Any]]:
    """Rebuild a publication record from the catalog row for one video."""
    state = load_metadata_state(conn, comp_id)
    if state is None:
        return None
    source = state["source"]
    if not passes_minimum_video_duration(source.get("duration")):
        return None
    provider, _, raw_id = str(comp_id).partition(":")
    genres = {str(row[0]) for row in conn.execute(
        "SELECT category FROM video_categories WHERE video_id = %s", (comp_id,)).fetchall()
        if row[0]}
    tags = {str(t).strip() for t in (state["metadata"].get("tags") or []) if str(t).strip()}
    performers = services.normalize_performers(state["metadata"].get("performers") or [])
    public_thumb_url, download_thumb_url = services.thumbnail_urls(source.get("thumbnail"))
    base_name = services.stable_media_basename(comp_id)
    video = {**source, **state["metadata"],
             "id": comp_id, "_video_id": comp_id, "provider": provider,
             "provider_video_id": raw_id,
             "performers": performers, "thumbnail": public_thumb_url,
             "performer_thumbs": services.load_performer_thumbs(conn, performers),
             "dateadded": services.format_dateadded(source.get("first_seen"))}
    return {
        "comp_id": comp_id, "provider": provider,
        "title": video.get("title") or comp_id, "video": video,
        "genres": genres, "tags": tags,
        "strm_file": media_dir / f"{base_name}.strm",
        "nfo_file": media_dir / f"{base_name}.nfo",
        "jpg_file": media_dir / f"{base_name}.jpg",
        "thumb_url": download_thumb_url, "studio": source.get("studio"),
    }


def republish_pending_videos(
    conn: db.Connection,
    media_dir: Path,
    *,
    services: VideoPersistenceServices,
    limit: int = 50,
) -> Dict[str, int]:
    """Retry sidecar publication for catalog rows still waiting on their files.

    Publication is the only step that can fail after the catalog batch commits,
    so this is the durable recovery path: it reads the pending state from the
    database and therefore also repairs entries left by a crashed run.
    """
    pending = publication_pending_ids(conn, limit)
    if not pending:
        return {"pending": 0, "published": 0, "failed": 0}
    media_dir.mkdir(parents=True, exist_ok=True)
    published = 0
    failed = 0
    for comp_id in pending:
        try:
            prepared = prepared_from_catalog(comp_id, media_dir, conn, services)
            if prepared is None:
                continue
            write_media_files(services, prepared, conn)
            published += 1
        except Exception as exc:
            failed += 1
            try:
                conn.rollback()
            except Exception:
                logger.debug("Ignored exception in republish_pending_videos", exc_info=True)
            services.logger.error("Republish failed for %s: %s", comp_id, exc)
    services.logger.info(
        "Republished %d/%d catalog row(s) awaiting library publication (%d failed, %d still pending)",
        published, len(pending), failed, max(0, len(pending) - published - failed),
    )
    return {"pending": len(pending), "published": published, "failed": failed}
