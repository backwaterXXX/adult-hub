"""Fingerprint matching and catalog backfill for scene metadata."""
from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    from .scene_scraper_models import ScrapedScene
except ImportError:
    from scene_scraper_models import ScrapedScene

if __package__:
    from . import scene_scrapers as _core
else:
    import scene_scrapers as _core



if __package__:
    from .scene_scraper_apply import (
        _apply_matched_scene,
        _backfill_linked_scene_artwork,
        _is_generated_telegram_post_title,
        _scene_match_provenance,
    )
    from .scene_scraper_caption_matches import (
        _backfill_caption_title_matches,
        _backfill_stashdb_caption_matches,
        _load_official_stashdb_performers,
    )
    from .scene_scraper_split_parts import _backfill_split_part_matches
else:
    from scene_scraper_apply import (
        _apply_matched_scene,
        _backfill_linked_scene_artwork,
        _is_generated_telegram_post_title,
        _scene_match_provenance,
    )
    from scene_scraper_caption_matches import (
        _backfill_caption_title_matches,
        _backfill_stashdb_caption_matches,
        _load_official_stashdb_performers,
    )
    from scene_scraper_split_parts import _backfill_split_part_matches



__all__ = [
    "ScrapedScene",
    "_apply_matched_scene",
    "_backfill_caption_title_matches",
    "_backfill_linked_scene_artwork",
    "_backfill_split_part_matches",
    "_backfill_stashdb_caption_matches",
    "_cached_video_fingerprint",
    "_fingerprint_backfill_module",
    "_fingerprint_feature_enabled",
    "_is_generated_telegram_post_title",
    "_load_official_stashdb_performers",
    "_scene_match_provenance",
    "_store_video_fingerprint",
    "_telegram_oshash_batch_limit",
    "_update_media_files",
    "apply_pending_fingerprint_proposals",
    "backfill_missing_telegram_oshashes",
    "backfill_scenes",
    "backfill_telegram_fingerprints",
]


def _cached_video_fingerprint(conn: Any, video_id: str, algorithm: str) -> Optional[str]:
    row = conn.execute(
        "SELECT fingerprint FROM video_fingerprints WHERE video_id=%s AND algorithm=%s",
        (video_id, algorithm),
    ).fetchone()
    fingerprint = str(row[0] or "").strip().lower() if row else ""
    return fingerprint if _core.re.fullmatch(r"[0-9a-f]{16}", fingerprint) else None


def _store_video_fingerprint(
    conn: Any, video_id: str, algorithm: str, fingerprint: str,
    *, media_probe: Optional[dict] = None,
) -> None:
    value = str(fingerprint or "").strip().lower()
    if algorithm not in {"OSHASH", "PHASH"} or not _core.re.fullmatch(r"[0-9a-f]{16}", value):
        raise ValueError("Invalid video fingerprint")
    probe_status = media_probe.get("status") if isinstance(media_probe, dict) else None
    probe_bytes = media_probe.get("bytes") if isinstance(media_probe, dict) else None
    probe_data = media_probe.get("data", {}) if isinstance(media_probe, dict) else None
    probe_json = _core.json.dumps(probe_data, separators=(",", ":")) if probe_data is not None else None
    with conn:
        conn.execute(
            """INSERT INTO video_fingerprints(
                   video_id,algorithm,fingerprint,media_probe_status,media_probe_bytes,media_probe_data
               ) VALUES (%s,%s,%s,%s,%s,%s::jsonb)
               ON CONFLICT(video_id,algorithm) DO UPDATE SET
                 fingerprint=EXCLUDED.fingerprint,computed_at=CURRENT_TIMESTAMP,
                 media_probe_status=COALESCE(EXCLUDED.media_probe_status,video_fingerprints.media_probe_status),
                 media_probe_bytes=COALESCE(EXCLUDED.media_probe_bytes,video_fingerprints.media_probe_bytes),
                 media_probe_data=COALESCE(EXCLUDED.media_probe_data,video_fingerprints.media_probe_data),
                 media_probe_updated_at=CASE WHEN EXCLUDED.media_probe_status IS NOT NULL
                   THEN CURRENT_TIMESTAMP ELSE video_fingerprints.media_probe_updated_at END""",
            (video_id, algorithm, value, probe_status, probe_bytes, probe_json),
        )
        raw_size = probe_data.get("file_size_bytes") if isinstance(probe_data, dict) else None
        if isinstance(raw_size, bool) or not isinstance(raw_size, (int, float)) or raw_size < 0:
            return
        conn.execute(
            "UPDATE videos SET file_size_bytes=%s WHERE id=%s AND file_size_bytes IS DISTINCT FROM %s",
            (int(raw_size), video_id, int(raw_size)),
        )




def apply_pending_fingerprint_proposals(conn: Any, media_dir: Path, *, limit: int = 500,
                                        http_client: Optional[Any] = None) -> Tuple[int, int]:
    """Apply existing trusted candidates, newest first, in resumable batches."""
    sources = [f"{provider}:{method}" for provider in ("stashdb", "theporndb")
               for method in ("phash", "oshash", "title_exact", "linked_id", "split_title_duration_v1")]
    rows = conn.execute(
        "SELECT id FROM metadata_review_proposals WHERE status='pending' "
        "AND next_attempt_at<=CURRENT_TIMESTAMP AND lower(source)=ANY(%s) "
        "ORDER BY next_attempt_at,id LIMIT %s", (sources, max(1, min(int(limit), 1000))),
    ).fetchall()
    processed = applied = 0
    for (proposal_id,) in rows:
        try:
            did_process = False
            did_apply = False
            with conn:
                row = conn.execute(
                    "SELECT video_id,changes,source,external_id,source_url,confidence "
                    "FROM metadata_review_proposals WHERE id=%s AND status='pending' "
                    "AND next_attempt_at<=CURRENT_TIMESTAMP FOR UPDATE SKIP LOCKED",
                    (proposal_id,),
                ).fetchone()
                if not row:
                    continue
                video_id, changes, source, external_id, url, confidence = row
                current = conn.execute(
                    "SELECT scene_match_source,scene_match_external_id FROM videos WHERE id=%s FOR UPDATE",
                    (video_id,),
                ).fetchone()
                # A newer scene identity supersedes this historical suggestion.
                same_scene = (current and external_id and str(current[1] or '') == str(external_id)
                              and str(current[0] or '').split(':')[0].casefold() == str(source).split(':')[0].casefold())
                lower_priority = (str(source).casefold().split(':')[-1] not in {'phash', 'oshash'}
                                  and current and any(token in str(current[0] or '').casefold()
                                                      for token in ('phash', 'oshash', 'fingerprint')))
                if same_scene and not lower_priority:
                    changes = json.loads(changes) if isinstance(changes, str) else changes
                    changes = changes if isinstance(changes, dict) else {}
                    scene = ScrapedScene(title=changes.get('title', ''), plot=changes.get('plot', ''),
                        date=changes.get('date_released', ''), studio=changes.get('studio', ''),
                        performers=changes.get('performers', []), tags=changes.get('tags', []),
                        source=source, external_id=external_id, url=url, confidence=confidence)
                    _apply_matched_scene(conn, media_dir, str(video_id), scene, http_client=http_client,
                                         strict_media=True)
                    did_apply = True
                    conn.execute(
                        "UPDATE metadata_review_proposals SET status='rejected',reviewed_at=CURRENT_TIMESTAMP "
                        "WHERE video_id=%s AND status='pending' AND id<%s AND lower(source)=ANY(%s)",
                        (video_id, proposal_id, sources),
                    )
                conn.execute("""UPDATE metadata_review_proposals
                    SET status=%s,reviewed_at=CURRENT_TIMESTAMP,attempt_count=attempt_count+1,
                        last_attempt_at=CURRENT_TIMESTAMP,next_attempt_at=CURRENT_TIMESTAMP,last_error=''
                    WHERE id=%s""",
                             ('accepted' if same_scene and not lower_priority else 'rejected', proposal_id))
                did_process = True
            processed += int(did_process)
            applied += int(did_apply)
        except Exception as exc:
            conn.rollback()
            _core.logger.exception("Could not automatically apply metadata proposal %s", proposal_id)
            try:
                with conn:
                    conn.execute("""UPDATE metadata_review_proposals
                        SET attempt_count=attempt_count+1,last_attempt_at=CURRENT_TIMESTAMP,
                            next_attempt_at=CURRENT_TIMESTAMP + make_interval(secs =>
                                LEAST(604800::double precision,
                                      60::double precision * power(2::double precision,
                                          LEAST(attempt_count,13)::double precision))),
                            last_error=%s
                        WHERE id=%s AND status='pending'""", (type(exc).__name__, proposal_id))
            except Exception:
                conn.rollback()
                _core.logger.exception("Could not record retry state for metadata proposal %s", proposal_id)
    conn.commit()
    return processed, applied


def backfill_scenes(
    conn: Any,
    media_dir: Path,
    limit: int = 50,
    sleep_sec: float = 0.5,
    http_client: Optional[Any] = None,
) -> Tuple[int, int]:
    """Run fingerprint, split-sequence and caption matching for Telegram scenes."""
    if not conn or not media_dir:
        return 0, 0

    try:
        # Check if columns exist
        conn.execute("SELECT plot, date_released, scene_matched_at FROM videos LIMIT 1")
    except Exception:
        # Columns not yet migrated, skip gracefully
        logger.debug("Ignored exception in backfill_scenes", exc_info=True)
        return 0, 0

    checked, updated = apply_pending_fingerprint_proposals(conn, media_dir, limit=500, http_client=http_client)
    hash_checked, hash_updated = _core.backfill_telegram_fingerprints(
        conn, media_dir, limit=min(limit, 20), sleep_sec=sleep_sec, http_client=http_client,
    )
    checked += hash_checked
    updated += hash_updated
    missing_hash_limit = _core._telegram_oshash_batch_limit()
    oshash_checked, oshash_computed = _core.backfill_missing_telegram_oshashes(
        conn, limit=missing_hash_limit, sleep_sec=min(sleep_sec, 0.1), http_client=http_client,
    )
    checked += oshash_checked
    updated += oshash_computed

    split_checked, split_updated = _backfill_split_part_matches(
        conn, media_dir, limit=min(100, max(1, limit)), sleep_sec=sleep_sec,
        http_client=http_client,
    )
    checked += split_checked
    updated += split_updated

    title_checked, title_updated = _backfill_caption_title_matches(
        conn, media_dir, limit=min(100, max(1, limit)), sleep_sec=sleep_sec,
        http_client=http_client,
    )
    checked += title_checked
    updated += title_updated
    artwork_checked, artwork_updated = _backfill_linked_scene_artwork(
        conn, media_dir, limit=min(100, max(1, limit)), sleep_sec=sleep_sec,
        http_client=http_client,
    )
    checked += artwork_checked
    updated += artwork_updated
    text_checked, text_updated = _backfill_stashdb_caption_matches(
        conn, limit=min(512, max(1, limit)),
    )
    checked += text_checked
    updated += text_updated
    _core.logger.info(
        "Telegram metadata backfill complete: checked=%d, enriched=%d "
        "(fingerprint, split-title/runtime, linked-scene artwork, caption-title and StashDB-alias stages)",
        checked, updated,
    )
    return checked, updated



def _fingerprint_backfill_module():
    if __package__:
        from . import scene_scraper_fingerprint_backfill as implementation
    else:
        import scene_scraper_fingerprint_backfill as implementation
    return implementation


def _telegram_oshash_batch_limit() -> int:
    return _fingerprint_backfill_module()._telegram_oshash_batch_limit()


def backfill_missing_telegram_oshashes(
    conn: Any,
    limit: int = 200,
    sleep_sec: float = 0.1,
    http_client: Optional[Any] = None,
) -> Tuple[int, int]:
    return _fingerprint_backfill_module().backfill_missing_telegram_oshashes(
        conn, limit, sleep_sec, http_client
    )


def _fingerprint_feature_enabled(name: str, legacy_name: str, *, default: bool = False) -> bool:
    return _fingerprint_backfill_module()._fingerprint_feature_enabled(
        name, legacy_name, default=default
    )


def backfill_telegram_fingerprints(
    conn: Any,
    media_dir: Path,
    limit: int = 20,
    sleep_sec: float = 0.5,
    http_client: Optional[Any] = None,
    video_ids: Optional[List[str]] = None,
    channel_id: Optional[str] = None,
    progress_callback: Optional[Any] = None,
) -> Tuple[int, int]:
    implementation = _fingerprint_backfill_module().backfill_telegram_fingerprints
    kwargs = {
        "limit": limit,
        "sleep_sec": sleep_sec,
        "http_client": http_client,
        "video_ids": video_ids,
        "channel_id": channel_id,
    }
    if "progress_callback" in inspect.signature(implementation).parameters:
        kwargs["progress_callback"] = progress_callback
    return implementation(conn, media_dir, **kwargs)



def _update_media_files(
    media_dir: Path,
    video_id: str,
    matched: ScrapedScene,
    performers: List[str],
    http_client: Optional[Any] = None,
    conn: Optional[Any] = None,
    strict: bool = False,
) -> None:
    """Compatibility facade for matched NFO and artwork persistence."""
    if __package__:
        from .scene_scraper_media_files import _update_media_files as implementation
    else:
        from scene_scraper_media_files import _update_media_files as implementation
    return implementation(media_dir, video_id, matched, performers, http_client, conn, strict)

