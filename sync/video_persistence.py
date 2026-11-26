from __future__ import annotations

import json
import logging
from common import db
from common.duration import passes_minimum_video_duration
from common.performer_catalog import normalize_name
from sync.studio_tags import looks_like_channel_label
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


@dataclass
class VideoPersistenceServices:
    stable_media_basename: Callable[[str], str]
    map_categories: Callable[..., Set[str]]
    normalize_performers: Callable[[List[str]], List[str]]
    thumbnail_urls: Callable[[Optional[str]], Tuple[str, str]]
    download_image: Callable[[str, Path], None]
    write_stream_file: Callable[[Path, str], bool]
    sync_unique_video: Callable[..., Any]
    persist_prepared_video: Callable[[db.Connection, Dict[str, Any]], None]
    logger: logging.Logger
    time_fn: Callable[[], float] = time.time


def _merge_list_field(existing: Dict[str, Any], new: Dict[str, Any], key: str) -> None:
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


def _merge_performer_proposals(existing: Dict[str, Any], new: Dict[str, Any]) -> None:
    old_proposals = existing.get("performer_proposals") or []
    proposal_keys = {(p.get("matched_text"), p.get("reason")) for p in old_proposals}
    for proposal in new.get("performer_proposals") or []:
        key = (proposal.get("matched_text"), proposal.get("reason"))
        if key not in proposal_keys:
            old_proposals.append(proposal)
            proposal_keys.add(key)
    if old_proposals:
        existing["performer_proposals"] = old_proposals


def _merge_performer_resolution(existing: Dict[str, Any], new: Dict[str, Any]) -> None:
    new_resolution = new.get("performer_resolution") or {}
    old_resolution = existing.get("performer_resolution") or {}
    if new_resolution and int(new_resolution.get("priority") or 0) >= int(old_resolution.get("priority") or -1):
        existing["performer_resolution"] = new_resolution
        if new.get("performers"):
            existing["performers"] = new["performers"]


def _merge_video_fields(existing: Dict[str, Any], new: Dict[str, Any]) -> None:
    """Union performers/categories from a duplicate sighting (order-preserving).

    The same video can surface in several sections/categories with partial
    metadata each time; merging avoids losing performers found elsewhere.
    """
    for key in ("performers", "categories"):
        _merge_list_field(existing, new, key)
    for key in ("source_text", "source_filename"):
        if not existing.get(key) and new.get(key):
            existing[key] = new[key]
    _merge_performer_proposals(existing, new)
    _merge_performer_resolution(existing, new)


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
    _record_primary_video_source(conn, comp_id, provider, video, time_fn=time_fn)


def _record_primary_video_source(
    conn: db.Connection,
    comp_id: str,
    provider: str,
    video: Dict[str, Any],
    time_fn: Callable[[], float] = time.time,
) -> None:
    """Record primary playable source in video_sources for Telegram videos in the same transaction."""
    if provider != "telegram":
        return

    channel_id = str(video.get("channel_id") or "").strip()
    message_id = str(video.get("message_id") or "").strip()
    parts = comp_id.split(":")
    if not channel_id and len(parts) >= 2:
        channel_id = parts[1]
    if not message_id and len(parts) >= 3:
        message_id = parts[2]
    channel_id = channel_id or None

    source_url = str(video.get("source_url") or "").strip()
    if not source_url:
        if (
            channel_id
            and channel_id.startswith("-100")
            and channel_id[4:].isdigit()
            and message_id
            and message_id.isdigit()
        ):
            source_url = f"https://t.me/c/{channel_id[4:]}/{message_id}"
        else:
            source_url = ""

    provider_ref = str(video.get("provider_ref") or comp_id)
    now = int(time_fn())

    cur = conn.execute("""
        INSERT INTO video_sources (
            video_id, source_id, provider, provider_ref, source_url, channel_id, priority, is_active, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, 1, 1, %s)
        ON CONFLICT (source_id) DO UPDATE SET
            provider = EXCLUDED.provider,
            provider_ref = EXCLUDED.provider_ref,
            source_url = COALESCE(NULLIF(EXCLUDED.source_url, ''), video_sources.source_url),
            channel_id = COALESCE(NULLIF(EXCLUDED.channel_id, ''), video_sources.channel_id)
        WHERE video_sources.video_id = EXCLUDED.video_id
        RETURNING video_sources.video_id
    """, (comp_id, comp_id, provider, provider_ref, source_url, channel_id, now))

    row = cur.fetchone()
    if row is None:
        raise RuntimeError(
            f"video_sources conflict: source_id '{comp_id}' is already owned by another video"
        )


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


def _load_existing_video_row(conn: db.Connection, comp_id: str):
    try:
        return conn.execute(
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
            return conn.execute(
                "SELECT first_seen, studio, performers FROM videos WHERE id = %s", (comp_id,)).fetchone()
        except Exception:
            return None


def _backfill_from_row(video: Dict[str, Any], _row, studio_val: str) -> str:
    """Fill studio, plot and release date from the stored row when the listing lacks them."""
    if not studio_val and _row and len(_row) > 1 and _row[1]:
        studio_val = _row[1]
    if _row and len(_row) > 3 and _row[3] and not video.get("plot"):
        video["plot"] = _row[3]
    if _row and len(_row) > 4 and _row[4] and not video.get("date_released"):
        video["date_released"] = _row[4]
    return studio_val


def _apply_scene_match_provenance(video: Dict[str, Any], _row) -> None:
    # Zachowaj proweniencje dopasowania sceny, aby przebieg zapisu nie
    # usuwal identyfikatora katalogowego.
    if _row and len(_row) > 9 and _row[9]:
        video["scene_match_source"] = _row[8]
        video["scene_match_external_id"] = _row[9]
        if len(_row) > 10 and _row[10]:
            video["scene_match_url"] = _row[10]


def _apply_catalog_title(video: Dict[str, Any], title: str, provider: str, _row) -> str:
    # Tytul: potwierdzone dopasowanie sceny StashDB/ThePornDB jest kanonicznym
    # zrodlem tytulu (jak w _apply_matched_scene). Swiezy listing z Telegrama
    # nie moze go cofnac do podpisu kanalu — inaczej klient traci tytul
    # katalogowy przy kazdym przebiegu.
    # Jawne nadpisanie operatora ma pierwszenstwo.
    provider_title = title
    catalog_title = ""
    if _row and len(_row) > 7 and _row[7]:
        catalog_title = str(_row[7]).strip()
    scene_source = str(_row[8] or "").casefold() if _row and len(_row) > 8 else ""
    has_catalog_match = any(
        provider in scene_source for provider in ("stashdb", "fansdb", "theporndb", "tpdb")
    )
    title_overridden = bool(_row and len(_row) > 11 and _row[11])
    if has_catalog_match:
        _apply_scene_match_provenance(video, _row)
    if has_catalog_match and catalog_title and not title_overridden:
        title = catalog_title
        # Zachowaj podpis dostawcy w <originaltitle> jako proweniencje.
        video["original_title"] = provider_title
        video["title"] = catalog_title
    return title


def _listing_performers(video: Dict[str, Any], services: VideoPersistenceServices) -> list:
    fresh_perfs = services.normalize_performers(video.get("performers", []))
    if video.get("performer_resolution"):
        fresh_perfs = list(dict.fromkeys(
            str(value).strip() for value in video.get("performers", []) if str(value).strip()
        ))
    return fresh_perfs


def _apply_stored_resolution(_row, fresh_perfs: list, effective_resolution):
    """A stored resolution with higher priority than the listing's replaces it, with its performers."""
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
                    fresh_perfs = list(dict.fromkeys(
                        str(value).strip() for value in _db_perfs if str(value).strip()
                    ))
                except Exception:
                    logger.debug("Ignored exception in sync_unique_video", exc_info=True)
    return fresh_perfs, effective_resolution


def _apply_stored_performers(video: Dict[str, Any], _row, fresh_perfs: list,
                             services: VideoPersistenceServices) -> list:
    if _row and len(_row) > 6 and _row[6] and _row[2]:
        try:
            _db_perfs = json.loads(_row[2]) if isinstance(_row[2], str) else _row[2]
            fresh_perfs = services.normalize_performers(_db_perfs)
        except Exception:
            logger.debug("Ignored exception in sync_unique_video", exc_info=True)
    elif not video.get("performer_resolution") and not fresh_perfs and _row and len(_row) > 2 and _row[2]:
        try:
            _db_perfs = json.loads(_row[2]) if isinstance(_row[2], str) else []
            fresh_perfs = services.normalize_performers(_db_perfs)
        except Exception:
            logger.debug("Ignored exception in sync_unique_video", exc_info=True)
    return fresh_perfs


def _resolve_fresh_performers(video: Dict[str, Any], _row, services: VideoPersistenceServices):
    """Listing performers win, but an empty listing must not erase performers already stored (backfill/matcher)."""
    fresh_perfs = _listing_performers(video, services)
    effective_resolution = video.get("performer_resolution")
    fresh_perfs, effective_resolution = _apply_stored_resolution(_row, fresh_perfs, effective_resolution)
    fresh_perfs = _apply_stored_performers(video, _row, fresh_perfs, services)
    return fresh_perfs, effective_resolution


def _detect_studio_names(video: Dict[str, Any], title: str, tags) -> Tuple[Any, list]:
    prod_names = []
    try:
        from sync.studio_tags import detect_production_studios, detect_production_studio_names
        detected_studios = detect_production_studios(title, tags)
        if detected_studios:
            tags = set(tags) | set(detected_studios)
        prod_names = detect_production_studio_names(title, tags)
        if prod_names:
            video["production_studios"] = prod_names
    except Exception:
        logger.debug("Ignored exception in sync_unique_video", exc_info=True)
    return tags, prod_names


def _is_telegram_channel_studio(studio_val: str) -> bool:
    # Telegram items used to carry the source channel in ``studio``. The channel
    # has its own column now, so only the legacy markers are recognised here and
    # every other value is a production studio.
    return bool(studio_val and (
        studio_val.lower() in ("telegram", "t.me", "telegram (t.me)")
        or studio_val.startswith("-100")
        or (studio_val.startswith("-") and studio_val[1:].isdigit())
    ))


def _is_own_channel_label(studio_val: str, video: Dict[str, Any]) -> bool:
    """True when the studio value is just this item's channel name.

    Channels removed from the configuration no longer contribute a hint, so the
    only trace of the old copy is the value matching ``channel_name``. Known
    production studios are exempt: a channel may legitimately be named after the
    studio it publishes.
    """
    channel_name = str(video.get("channel_name") or "").strip()
    if not channel_name or studio_val.strip().casefold() != channel_name.casefold():
        return False
    return looks_like_channel_label(studio_val)


def _apply_studio_fields(video: Dict[str, Any], title: str, tags, provider: str, studio_val: str):
    """Set studio/channel on the video dict. Returns (tags, studio value stored in the DB)."""
    tags, prod_names = _detect_studio_names(video, title, tags)
    is_tg_chan = _is_telegram_channel_studio(studio_val) or _is_own_channel_label(studio_val, video)
    if is_tg_chan:
        video["channel"] = studio_val
        video["studio"] = prod_names[0] if prod_names else ""
    elif prod_names:
        video["studio"] = prod_names[0]

    db_studio = prod_names[0] if prod_names else (None if (provider == "telegram" and is_tg_chan) else (studio_val or None))
    return tags, db_studio


def _clean_sync_tags(tags) -> set:
    clean_tags = set()
    for t in tags:
        st = str(t).strip()
        if st and st.lower() not in ("telegram", "t.me") and not st.startswith("-100"):
            clean_tags.add(st)
    return clean_tags


def _persist_prepared_with_commit(services: VideoPersistenceServices, prepared: Dict[str, Any],
                                  conn: db.Connection, commit: bool) -> None:
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
    jpg_file = target_dir / f"{base_name}.jpg"

    mapped_genres = services.map_categories(genres, title=title)

    # Studio (per-source override); starsze wiersze bez studia są backfillowane.
    _row = _load_existing_video_row(conn, comp_id)
    studio_val = (video.get("studio") or "").strip()
    studio_val = _backfill_from_row(video, _row, studio_val)
    title = _apply_catalog_title(video, title, provider, _row)

    # Obsada: swiezy listing wygrywa, ale pusty NIE kasuje tego, co juz mamy
    # w DB (backfill/matcher) — inaczej obsada ginalaby do kolejnego backfillu.
    _fresh_perfs, effective_resolution = _resolve_fresh_performers(video, _row, services)

    # Normalize performers once so the published metadata stays consistent (deduped order).
    _perfs = _fresh_perfs
    public_thumb_url, download_thumb_url = services.thumbnail_urls(video.get("thumbnail"))
    video = {**video, "_video_id": comp_id, "performers": _perfs, "thumbnail": public_thumb_url,
             "studio": studio_val, "performer_resolution": effective_resolution}

    # Detect production studios and add as tags and production_studios
    tags, db_studio = _apply_studio_fields(video, title, tags, provider, studio_val)
    tags = _clean_sync_tags(tags)

    prepared = {
        "comp_id": comp_id, "provider": provider, "title": title,
        "video": video, "genres": mapped_genres, "tags": tags,
        "strm_file": strm_file, "studio": db_studio,
        "jpg_file": jpg_file, "thumb_url": download_thumb_url,
    }

    if _prepare_only:
        return prepared

    # Record into DB with relative file path and composite video_id.
    _persist_prepared_with_commit(services, prepared, conn, commit)
    # The catalog row is durable; publish the stream pointer and artwork.
    write_media_files(services, prepared, conn, commit=commit)
    return None


def write_media_files(services: VideoPersistenceServices, prepared: Dict[str, Any],
                      conn: db.Connection, *, commit: bool = True) -> None:
    """Publish the thumbnail and STRM, then register the stream that exists."""
    thumb_url = prepared.get("thumb_url")
    if thumb_url and not prepared["jpg_file"].exists():
        services.download_image(thumb_url, prepared["jpg_file"])
    services.write_stream_file(prepared["strm_file"], prepared["comp_id"])
    register_stream_file(conn, prepared, time_fn=services.time_fn)
    if commit:
        conn.commit()


from sync.video_flush import flush_collected, publication_pending_ids, republish_pending_videos  # noqa: E402, F401
