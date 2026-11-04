"""Application of matched scene metadata and linked artwork backfill."""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional, Tuple

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


