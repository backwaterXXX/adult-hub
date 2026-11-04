"""Fingerprint matching and catalog backfill for scene metadata."""
from __future__ import annotations

import json
import inspect
from pathlib import Path
from dataclasses import replace
from typing import Any, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)

try:
    from .scene_scraper_models import ScrapedScene
except ImportError:
    from scene_scraper_models import ScrapedScene

if __package__:
    from . import scene_scrapers as _core
else:
    import scene_scrapers as _core


def _scene_match_provenance(scene: ScrapedScene) -> Tuple[str, str, str, float]:
    """Return safe, bounded provenance fields for persistence on the video row."""
    parsed = _core.urlparse(str(scene.url or "").strip())
    safe_url = ""
    hostname = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    if parsed.scheme.lower() in ("http", "https") and hostname:
        # Query strings and fragments may carry tracking or access tokens.
        safe_host = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None:
            safe_host = f"{safe_host}:{port}"
        safe_url = parsed._replace(netloc=safe_host, query="", fragment="").geturl()[:2048]
    try:
        confidence = min(1.0, max(0.0, float(scene.confidence)))
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        str(scene.source or "unknown")[:80],
        str(scene.external_id or "")[:255],
        safe_url,
        confidence,
    )


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


def _is_generated_telegram_post_title(title: Any, video_id: str) -> bool:
    """Recognize channel labels that simply repeat the Telegram post number."""
    parts = str(video_id or "").split(":", 3)
    if len(parts) < 3 or parts[0] != "telegram" or not parts[2].isdigit():
        return False
    value = str(title or "").strip()
    marker = f" #{parts[2]}"
    return bool(value[: -len(marker)].strip()) and value.casefold().endswith(marker.casefold())


def _apply_matched_scene(conn: Any, media_dir: Path, video_id: str, matched: ScrapedScene,
                        http_client: Optional[Any] = None, now: Optional[int] = None,
                        strict_media: bool = False) -> bool:
    """Apply trusted scene metadata and persist other differences as proposals."""
    timestamp = int(_core.time.time()) if now is None else int(now)
    cursor = conn.execute("SELECT * FROM videos WHERE id=%s FOR UPDATE", (video_id,))
    row = cursor.fetchone()
    if row is None:
        return False
    current = dict(zip([column.name for column in cursor.description], row))
    override_row = conn.execute(
        "SELECT fields FROM video_metadata_overrides WHERE video_id=%s", (video_id,)
    ).fetchone()
    overrides = override_row[0] if override_row and isinstance(override_row[0], dict) else {}
    if override_row and isinstance(override_row[0], str):
        try:
            overrides = _core.json.loads(override_row[0])
        except (TypeError, ValueError):
            overrides = {}
    if (isinstance(overrides, dict) and "title" in overrides
            and _is_generated_telegram_post_title(overrides.get("title"), video_id)):
        # Channel labels ending in the exact Telegram post number identify the
        # promo post, not necessarily the video. Drop that generated override
        # so a matched catalog title can replace it while preserving any other
        # independent operator overrides.
        overrides = dict(overrides)
        overrides.pop("title", None)
        conn.execute(
            "UPDATE video_metadata_overrides SET fields=%s::jsonb,revision=revision+1,"
            "updated_at=CURRENT_TIMESTAMP WHERE video_id=%s",
            (_core.json.dumps(overrides, ensure_ascii=False), video_id),
        )

    def list_value(value):
        if isinstance(value, str):
            try:
                value = _core.json.loads(value or "[]")
            except (TypeError, ValueError):
                value = []
        return value if isinstance(value, list) else []

    candidates = {
        "title": matched.title or "", "plot": matched.plot or "", "studio": matched.studio or "",
        "date_released": matched.date or "", "performers": list_value(matched.performers),
        "tags": list_value(matched.tags),
    }
    current_values = {
        "title": current.get("title") or "", "plot": current.get("plot") or "",
        "studio": current.get("studio") or "", "date_released": current.get("date_released") or "",
        "performers": list_value(current.get("performers")), "tags": list_value(current.get("tags")),
    }

    current_resolution = current.get("performer_resolution") or {}
    if isinstance(current_resolution, str):
        try:
            current_resolution = _core.json.loads(current_resolution)
        except (TypeError, ValueError):
            current_resolution = {}
    if not isinstance(current_resolution, dict):
        current_resolution = {}
    matched_source = str(matched.source or "unknown")
    normalized_source = matched_source.casefold()
    has_scene_catalog_match = any(
        provider in normalized_source for provider in ("stashdb", "theporndb", "tpdb")
    )
    matched_priority = 100 if any(token in matched_source.casefold() for token in ("oshash", "phash", "fingerprint")) else 70
    current_priority = int(current_resolution.get("priority") or -1)

    def has_value(value):
        return bool(value) if isinstance(value, list) else bool(str(value or "").strip())

    def same_value(left, right, field):
        if isinstance(left, list) and isinstance(right, list):
            def normalize(items):
                return sorted(str(item).strip().casefold() for item in items)

            return normalize(left) == normalize(right)
        return str(left or "").strip().casefold() == str(right or "").strip().casefold()

    auto_values = {}
    review_changes = {}
    trusted_fingerprint = normalized_source in {
        "stashdb:phash", "stashdb:oshash", "theporndb:phash", "theporndb:oshash",
    }
    trusted_text = normalized_source in {
        f"{provider}:{method}" for provider in ("stashdb", "theporndb")
        for method in ("title_exact", "linked_id", "split_title_duration_v1")
    }
    current_source = str(current.get("scene_match_source") or "").casefold()
    if trusted_text and any(token in current_source for token in ("phash", "oshash", "fingerprint")):
        return False
    for field, candidate in candidates.items():
        if not has_value(candidate):
            continue
        if trusted_fingerprint or trusted_text:
            if field not in overrides:
                auto_values[field] = candidate
            continue
        # A confirmed StashDB/ThePornDB scene match is the canonical title
        # source. Keep an explicit operator override, but replace Telegram's
        # supplied title and avoid creating a redundant review proposal.
        if field == "title" and has_scene_catalog_match and field not in overrides:
            auto_values[field] = candidate
            continue
        if field == "performers" and field in overrides:
            continue
        effective = overrides.get(field, current_values[field])
        if has_value(effective):
            if not same_value(effective, candidate, field):
                if field == "performers" and matched_priority > current_priority:
                    auto_values[field] = candidate
                else:
                    review_changes[field] = candidate
            elif field == "performers" and matched_priority > current_priority:
                auto_values[field] = candidate
        elif field not in overrides:
            auto_values[field] = candidate
    if matched.poster_url and str(current.get("thumbnail") or "").strip() != matched.poster_url.strip():
        auto_values["thumbnail"] = matched.poster_url

    provenance = _core._scene_match_provenance(matched)
    assignments = []
    params = []
    for field in ("title", "plot", "studio", "date_released", "performers", "tags", "thumbnail"):
        if field not in auto_values:
            continue
        assignments.append(f"{field}=%s")
        value = auto_values[field]
        params.append(_core.json.dumps(value, ensure_ascii=False) if field in {"performers", "tags"} else value)
    assignments.extend(("scene_matched_at=%s", "scene_match_source=%s", "scene_match_external_id=%s",
                        "scene_match_url=%s", "scene_match_confidence=%s"))
    params.extend((timestamp, *provenance))
    if "performers" in auto_values:
        resolution = {
            "status": "resolved",
            "method": matched_source,
            "source": "fingerprint_scene" if matched_priority == 100 else "catalog_scene",
            "external_id": provenance[1],
            "priority": matched_priority,
            "version": 1,
        }
        assignments.append("performer_resolution=%s::jsonb")
        params.append(_core.json.dumps(resolution, ensure_ascii=False))
    params.append(video_id)
    conn.execute(f"UPDATE videos SET {','.join(assignments)} WHERE id=%s", params)
    if auto_values or matched.poster_url:
        auto_scene = replace(
            matched, title=auto_values.get("title", ""), plot=auto_values.get("plot", ""),
            studio=auto_values.get("studio", ""), date=auto_values.get("date_released", ""),
            performers=auto_values.get("performers", []), tags=auto_values.get("tags", []),
            poster_url=matched.poster_url,
        )
        _core._update_media_files(media_dir=media_dir, video_id=video_id, matched=auto_scene,
                                  performers=auto_values.get("performers", []),
                                  http_client=http_client, conn=conn,
                                  strict=strict_media or trusted_fingerprint or trusted_text)
    if review_changes:
        from common.metadata_review import store_metadata_review_proposal
        store_metadata_review_proposal(
            conn, video_id, review_changes, source=provenance[0], external_id=provenance[1],
            source_url=provenance[2], confidence=provenance[3],
        )
    return bool(auto_values)


# ==============================================================================
# 4. Backfill Routine for Hub Database
# ==============================================================================

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
    except Exception as _exc:
        # Columns not yet migrated, skip gracefully
        logger.debug("Ignored exception in %s", "backfill_scenes", exc_info=True)
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


def _backfill_linked_scene_artwork(
    conn: Any,
    media_dir: Path,
    *,
    limit: int,
    sleep_sec: float,
    http_client: Optional[Any],
) -> Tuple[int, int]:
    """Fetch the poster for already-linked scenes, including older matches."""
    attempt_source = "telegram_linked_scene_artwork_by_id_v1"
    try:
        rows = conn.execute(
            """SELECT v.id,v.duration,v.scene_match_external_id,v.scene_match_source
               FROM videos v LEFT JOIN external_metadata_attempts a
                 ON a.entity_type='video' AND a.entity_key=v.id AND a.source=%s
               WHERE v.provider='telegram' AND v.scene_match_external_id IS NOT NULL
                 AND (v.scene_match_source ILIKE '%%stashdb%%'
                      OR v.scene_match_source ILIKE '%%theporndb%%'
                      OR v.scene_match_source ILIKE '%%tpdb%%')
                 AND (a.entity_key IS NULL OR a.next_attempt_at<=CURRENT_TIMESTAMP)
               ORDER BY a.last_attempt_at ASC NULLS FIRST,v.first_seen ASC,v.id ASC LIMIT %s""",
            (attempt_source, max(1, int(limit))),
        ).fetchall()
    except Exception as exc:
        _core.logger.debug("Linked scene artwork query unavailable: %s", type(exc).__name__)
        return 0, 0
    if not rows:
        return 0, 0

    try:
        if __package__:
            from .scene_scraper_text_sources import (
                fetch_stashdb_scene_by_id, fetch_tpdb_scene_by_id,
            )
        else:
            from scene_scraper_text_sources import (
                fetch_stashdb_scene_by_id, fetch_tpdb_scene_by_id,
            )
    except ImportError as exc:
        _core.logger.warning("Linked scene artwork lookup unavailable (%s)", type(exc).__name__)
        return 0, 0

    checked = updated = 0
    for index, (video_id, duration, external_id, match_source) in enumerate(rows):
        if index and sleep_sec > 0:
            _core.time.sleep(sleep_sec)
        checked += 1
        try:
            lookup = (fetch_tpdb_scene_by_id
                      if "theporndb" in str(match_source or "").casefold()
                      or "tpdb" in str(match_source or "").casefold()
                      else fetch_stashdb_scene_by_id)
            scene = lookup(str(external_id or ""), http_client=http_client)
            if scene and str(scene.external_id or "") == str(external_id or "") and scene.poster_url:
                scene = replace(scene, title="", plot="", date="", performers=[], tags=[], studio="")
                with conn:
                    image_saved = _core._update_media_files(
                        media_dir=media_dir, video_id=str(video_id), matched=scene,
                        performers=[], http_client=http_client, conn=conn,
                    )
                    if image_saved:
                        conn.execute("UPDATE videos SET thumbnail=%s WHERE id=%s", (scene.poster_url, video_id))
                if image_saved:
                    updated += 1
                    outcome = "found"
                else:
                    outcome = "error"
                    if _core.record_attempt:
                        _core.record_attempt(
                            conn, "video", video_id, attempt_source, outcome,
                            error=RuntimeError("poster_download_failed"),
                        )
                    continue
            else:
                outcome = "no_match"
            if _core.record_attempt:
                _core.record_attempt(conn, "video", video_id, attempt_source, outcome)
        except Exception as exc:
            if _core.record_attempt:
                try:
                    _core.record_attempt(conn, "video", video_id, attempt_source, "error", error=exc)
                except Exception:
                    _core.logger.debug("Could not save linked artwork retry state for %s", video_id)
            _core.logger.warning("Linked scene artwork lookup failed for %s (%s)", video_id, type(exc).__name__)
    return checked, updated


def _backfill_split_part_matches(
    conn: Any,
    media_dir: Path,
    *,
    limit: int,
    sleep_sec: float,
    http_client: Optional[Any],
) -> Tuple[int, int]:
    """Search complete part sequences once using their combined runtime.

    Incomplete, duplicated, already matched, or higher-priority-resolved groups
    are left untouched. Attempts are tracked per member so normal retry policy
    also applies when the sync process is interrupted part way through a group.
    """
    attempt_source = "telegram_split_title_duration_v1"
    try:
        rows = conn.execute(
            """SELECT v.id,v.title,v.source_filename,v.source_text,v.duration,
                      v.performer_resolution,v.scene_match_external_id,
                      (a.next_attempt_at IS NULL OR a.next_attempt_at<=CURRENT_TIMESTAMP)
               FROM videos v
               LEFT JOIN external_metadata_attempts a
                 ON a.entity_type='video' AND a.entity_key=v.id AND a.source=%s
               WHERE v.provider='telegram'
                 AND (COALESCE(v.title,'') ~* '(part[[:space:]_.-]*|pt[.]?[[:space:]_.-]*|s[[:space:]_.-]*)[0-9]{1,2}'
                   OR COALESCE(v.source_filename,'') ~* '(part[[:space:]_.-]*|pt[.]?[[:space:]_.-]*|s[[:space:]_.-]*)[0-9]{1,2}'
                   OR COALESCE(v.source_text,'') ~* '(part[[:space:]_.-]*|pt[.]?[[:space:]_.-]*|s[[:space:]_.-]*)[0-9]{1,2}')
               ORDER BY v.first_seen ASC,v.id ASC""",
            (attempt_source,),
        ).fetchall()
    except Exception as exc:
        _core.logger.debug("Split-sequence query unavailable (%s)", type(exc).__name__)
        return 0, 0
    if not rows:
        return 0, 0

    try:
        from common.telegram_metadata import extract_telegram_metadata
        if __package__:
            from .scene_scraper_split_parts import parse_split_part_title, search_split_scene
        else:
            from scene_scraper_split_parts import parse_split_part_title, search_split_scene
    except ImportError as exc:
        _core.logger.warning("Split-sequence lookup unavailable (%s)", type(exc).__name__)
        return 0, 0

    groups: dict[tuple[str, str], dict[int, list[dict[str, Any]]]] = {}
    for row in rows:
        video_id, title, filename, source_text, duration, resolution, matched_id, attempt_due = row
        caption = str(source_text or "")
        extracted = extract_telegram_metadata(caption)
        candidates = (extracted.get("title"), filename, title, caption)
        parsed = next((result for value in candidates
                       if value and (result := parse_split_part_title(str(value))) is not None), None)
        if not parsed:
            continue
        part_number, base_title, base_key = parsed
        identifier_parts = str(video_id or "").split(":", 2)
        if len(identifier_parts) < 3 or identifier_parts[0] != "telegram":
            continue
        channel = identifier_parts[1]
        group = groups.setdefault((channel, base_key), {})
        try:
            part_duration = int(duration or 0)
        except (TypeError, ValueError):
            part_duration = 0
        if isinstance(resolution, str):
            try:
                resolution = _core.json.loads(resolution)
            except (TypeError, ValueError):
                resolution = {}
        resolution = resolution if isinstance(resolution, dict) else {}
        group.setdefault(part_number, []).append({
            "video_id": str(video_id), "title": base_title, "source_text": caption,
            "source_filename": str(filename or ""), "duration": part_duration,
            "resolution": resolution, "matched_id": str(matched_id or ""),
            "attempt_due": bool(attempt_due), "explicit_cast": (
                extracted.get("performer_names", [])
                if extracted.get("performer_source") == "labeled_cast" else []
            ),
        })

    checked = updated = searched_groups = 0
    now = int(_core.time.time())
    for (_channel, _base_key), parts_by_number in groups.items():
        part_count = max(parts_by_number)
        if part_count < 2 or part_count > 12:
            continue
        if set(parts_by_number) != set(range(1, part_count + 1)):
            # Do not put an incomplete group on cooldown; a later sync may add
            # the missing part and make the sequence searchable.
            continue
        if any(len(part_rows) != 1 for part_rows in parts_by_number.values()):
            continue
        parts = [parts_by_number[number][0] for number in range(1, part_count + 1)]
        if (not all(part["attempt_due"] for part in parts)
                or any(part["matched_id"] for part in parts)
                or any(int(part["resolution"].get("priority") or 0) >= 70 for part in parts)):
            continue
        durations = [part["duration"] for part in parts]
        if any(duration <= 0 or duration > 8 * 60 * 60 for duration in durations):
            continue
        total_duration = sum(durations)
        if total_duration > 12 * 60 * 60:
            continue
        if searched_groups >= max(1, int(limit)):
            break
        if searched_groups and sleep_sec > 0:
            _core.time.sleep(sleep_sec)
        searched_groups += 1
        checked += len(parts)
        query_title = parts[0]["title"]
        source_texts = [text for part in parts
                        for text in (part["source_text"], part["source_filename"])]
        explicit_cast = list(dict.fromkeys(
            name for part in parts for name in part["explicit_cast"] if str(name).strip()
        ))
        try:
            from .scene_scraper_split_parts import SPLIT_DURATION_TOLERANCE_SECONDS
        except ImportError:
            from scene_scraper_split_parts import SPLIT_DURATION_TOLERANCE_SECONDS
        try:
            scene = search_split_scene(
                query_title, duration=total_duration, source_texts=source_texts,
                caption_performers=explicit_cast, http_client=http_client,
                duration_tolerance=SPLIT_DURATION_TOLERANCE_SECONDS,
            )
            outcome = "found" if scene else "no_match"
            if scene:
                with conn:
                    for part in parts:
                        updated += int(_core._apply_matched_scene(
                            conn, media_dir, part["video_id"], scene,
                            http_client=http_client, now=now,
                        ))
            if _core.record_attempt:
                for part in parts:
                    _core.record_attempt(conn, "video", part["video_id"], attempt_source, outcome)
        except Exception as exc:
            if _core.record_attempt:
                for part in parts:
                    try:
                        _core.record_attempt(
                            conn, "video", part["video_id"], attempt_source, "error", error=exc,
                        )
                    except Exception:
                        _core.logger.debug("Could not save split-title retry state for %s", part["video_id"])
            _core.logger.warning("Split-sequence lookup failed for %s (%s)",
                                 parts[0]["video_id"], type(exc).__name__)
    return checked, updated


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

