"""Fingerprint matching and catalog backfill for scene metadata."""
from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from typing import Any, List, Optional, Tuple

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


def _apply_matched_scene(conn: Any, media_dir: Path, video_id: str, matched: ScrapedScene,
                        http_client: Optional[Any] = None, now: Optional[int] = None) -> bool:
    """Fill empty catalog fields and persist differences as review proposals."""
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
    for field, candidate in candidates.items():
        if not has_value(candidate):
            continue
        effective = overrides.get(field, current_values[field])
        if has_value(effective):
            if not same_value(effective, candidate, field):
                review_changes[field] = candidate
        elif field not in overrides:
            auto_values[field] = candidate
    if matched.poster_url and not str(current.get("thumbnail") or "").strip():
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
    params.extend((timestamp, *provenance, video_id))
    conn.execute(f"UPDATE videos SET {','.join(assignments)} WHERE id=%s", params)
    if auto_values:
        auto_scene = replace(
            matched, title=auto_values.get("title", ""), plot=auto_values.get("plot", ""),
            studio=auto_values.get("studio", ""), date=auto_values.get("date_released", ""),
            performers=auto_values.get("performers", []), tags=auto_values.get("tags", []),
            poster_url=matched.poster_url if "thumbnail" in auto_values else "",
        )
        _core._update_media_files(media_dir=media_dir, video_id=video_id, matched=auto_scene,
                                  performers=auto_values.get("performers", []),
                                  http_client=http_client, conn=conn)
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

def backfill_scenes(
    conn: Any,
    media_dir: Path,
    limit: int = 50,
    sleep_sec: float = 0.5,
    http_client: Optional[Any] = None,
) -> Tuple[int, int]:
    """Enrich unmatched videos and incrementally hash Telegram videos."""
    if not conn or not media_dir:
        return 0, 0

    try:
        # Check if columns exist
        conn.execute("SELECT plot, date_released, scene_matched_at FROM videos LIMIT 1")
    except Exception:
        # Columns not yet migrated, skip gracefully
        return 0, 0

    try:
        rows = conn.execute(
            """
            SELECT v.id, v.title, v.studio, v.duration, v.performers, v.thumbnail,
                   v.plot, v.date_released, v.tags
            FROM videos v
            LEFT JOIN external_metadata_attempts a
              ON a.entity_type='video' AND a.entity_key=v.id AND a.source='studio_scene'
            WHERE v.provider = 'telegram'
              AND ((a.entity_key IS NULL AND
                    (v.scene_matched_at IS NULL OR v.scene_matched_at < to_timestamp(%s)))
                   OR a.next_attempt_at <= CURRENT_TIMESTAMP)
            ORDER BY a.last_attempt_at ASC NULLS FIRST, v.first_seen ASC, v.id ASC
            LIMIT %s
            """,
            (int(_core.time.time()) - 180 * 86400, max(1, limit * 4)),
        ).fetchall()

        # Prioritize candidates from supported studios
        def _is_priority(r):
            st = str(r[2] or "").lower()
            ti = str(r[1] or "").lower()
            combined = f"{st} {ti}"
            return any(k in combined for k in (
                "falcon", "raging", "nextdoor", "next door", "sean cody",
                "seancody", "men.com", "hot house", "nakedsword", "naked sword",
                "helix", "belami", "bel ami", "lucas"
            ))

        prioritized = [r for r in rows if _is_priority(r)]
        remainder = [r for r in rows if not _is_priority(r)]
        candidates = (prioritized + remainder)[:limit]
    except Exception as e:
        _core.logger.warning("backfill_scenes query failed: %s", e)
        raise

    checked = 0
    updated = 0
    now = int(_core.time.time())

    for idx, (vid_id, title, studio, duration, perfs_json, thumb,
              plot, date_released, tags_json) in enumerate(candidates):
        if idx > 0 and sleep_sec > 0:
            _core.time.sleep(sleep_sec)

        checked += 1
        performers = []
        tags = []
        try:
            if perfs_json:
                performers = _core.json.loads(perfs_json)
        except Exception:
            pass
        try:
            if tags_json:
                tags = _core.json.loads(tags_json)
        except Exception:
            pass
        performers = performers if isinstance(performers, list) else []
        tags = tags if isinstance(tags, list) else []

        try:
            matched = _core.search_scene(
                title=title or "",
                studio=studio or "",
                duration=duration or 0,
                performers=performers,
                http_client=http_client,
            )

            with conn:
                if matched:
                    updated += int(_core._apply_matched_scene(conn, media_dir, str(vid_id), matched,
                                                             http_client=http_client, now=now))
                    outcome = "found"
                else:
                    # Negative cache: mark as checked so we don't hammer the API on every run
                    conn.execute(
                        "UPDATE videos SET scene_matched_at = %s WHERE id = %s",
                        (now, vid_id),
                    )
                    outcome = "no_match"
            if _core.record_attempt:
                _core.record_attempt(conn, "video", vid_id, "studio_scene", outcome)
        except Exception as e:
            if _core.record_attempt:
                try:
                    _core.record_attempt(conn, "video", vid_id, "studio_scene", "error", error=e)
                except Exception:
                    _core.logger.debug("Could not save scene retry state for %s", vid_id)
            _core.logger.warning("Failed enriching scene %s: %s", vid_id, e)

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
    _core.logger.info("Scene backfill complete: checked %d, enriched %d", checked, updated)
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
) -> Tuple[int, int]:
    return _fingerprint_backfill_module().backfill_telegram_fingerprints(
        conn, media_dir, limit, sleep_sec, http_client, video_ids, channel_id
    )



def _update_media_files(
    media_dir: Path,
    video_id: str,
    matched: ScrapedScene,
    performers: List[str],
    http_client: Optional[Any] = None,
    conn: Optional[Any] = None,
) -> None:
    """Compatibility facade for matched NFO and artwork persistence."""
    if __package__:
        from .scene_scraper_media_files import _update_media_files as implementation
    else:
        from scene_scraper_media_files import _update_media_files as implementation
    return implementation(media_dir, video_id, matched, performers, http_client, conn)

