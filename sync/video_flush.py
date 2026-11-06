"""Catalog flush and library publication recovery for video persistence.

Moved verbatim from video_persistence.py. The public names are re-exported
from video_persistence, which imports this module at its end.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from common import db
from common.duration import MIN_VIDEO_DURATION_SECONDS, passes_minimum_video_duration
from common.video_metadata import load_metadata_state

if TYPE_CHECKING:
    if __package__:
        from .video_persistence import VideoPersistenceServices
    else:
        from video_persistence import VideoPersistenceServices


def _persistence_module():
    """Return video_persistence. It imports this module at its end, so the import stays local."""
    if __package__:
        from . import video_persistence
    else:
        import video_persistence
    return video_persistence


def _drop_short_videos(collected: Dict[str, Dict[str, Any]], source_name: str,
                       services: VideoPersistenceServices) -> None:
    short_ids = [
        video_id for video_id, item in collected.items()
        if not passes_minimum_video_duration((item.get("video") or {}).get("duration"))
    ]
    for video_id in short_ids:
        collected.pop(video_id, None)
    if short_ids:
        services.logger.info("Skipped %d video(s) shorter than 420 seconds%s",
                             len(short_ids), f" ({source_name})" if source_name else "")


def _drop_hidden_duplicates(collected: Dict[str, Dict[str, Any]], source_name: str,
                            conn: db.Connection, services: VideoPersistenceServices) -> None:
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


def _merge_stored_metadata(collected: Dict[str, Dict[str, Any]], conn: db.Connection,
                           services: VideoPersistenceServices, errors: List[str]) -> None:
    """Merge previously known genres and tags from DB so we don't lose tags across syncs."""
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


def _prepare_collected_items(collected: Dict[str, Dict[str, Any]], media_dir: Path, conn: db.Connection,
                             services: VideoPersistenceServices, errors: List[str]) -> List[Dict[str, Any]]:
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
    return prepared_items


def _persist_prepared_batch(prepared_items: List[Dict[str, Any]], conn: db.Connection,
                            services: VideoPersistenceServices, errors: List[str]) -> Tuple[int, List[Dict[str, Any]]]:
    """Write all catalog rows in one transaction. Returns (rows written, rows committed)."""
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
            _persistence_module().logger.debug("Ignored exception in flush_collected", exc_info=True)
        errors.append(f"database batch: {type(e).__name__}: {e}")
        services.logger.warning(f"Database batch failed: {e}")
        written = 0
    return written, committed_items


def _publish_committed_items(committed_items: List[Dict[str, Any]], conn: db.Connection,
                             services: VideoPersistenceServices, errors: List[str]) -> None:
    for prepared in committed_items:
        try:
            _persistence_module().write_media_files(services, prepared, conn)
        except Exception as e:
            errors.append(f"{prepared['comp_id']} file write: {type(e).__name__}: {e}")
            services.logger.error("Failed to publish files for %s: %s", prepared["comp_id"], e)


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
    _drop_short_videos(collected, source_name, services)
    if not collected:
        return 0
    _drop_hidden_duplicates(collected, source_name, conn, services)
    if not collected:
        return 0
    count = len(collected)
    label = f" ({source_name})" if source_name else ""
    services.logger.info(f"Writing {count} videos{label} to {media_dir} and DB...")

    errors: List[str] = []
    _merge_stored_metadata(collected, conn, services, errors)
    prepared_items = _prepare_collected_items(collected, media_dir, conn, services, errors)
    written, committed_items = _persist_prepared_batch(prepared_items, conn, services, errors)

    # Publish sidecars only after the catalog batch is durable, so a rolled-back
    # batch cannot leave unmanaged .strm/.nfo files in the library. A failed
    # publication leaves the entry registered as nothing at all: its catalog row
    # survives, the library never sees a half-written item, and
    # republish_pending_videos() retries it from the database on the next pass.
    _publish_committed_items(committed_items, conn, services, errors)

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
            _persistence_module().write_media_files(services, prepared, conn)
            published += 1
        except Exception as exc:
            failed += 1
            try:
                conn.rollback()
            except Exception:
                _persistence_module().logger.debug("Ignored exception in republish_pending_videos", exc_info=True)
            services.logger.error("Republish failed for %s: %s", comp_id, exc)
    services.logger.info(
        "Republished %d/%d catalog row(s) awaiting library publication (%d failed, %d still pending)",
        published, len(pending), failed, max(0, len(pending) - published - failed),
    )
    return {"pending": len(pending), "published": published, "failed": failed}
