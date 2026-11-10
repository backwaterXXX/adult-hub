"""Application of matched scene metadata and linked artwork backfill."""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

from sync.scene_scraper_models import ScrapedScene  # noqa: E402

from sync import scene_scrapers as _core  # noqa: E402

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


def _fetch_video_row(conn: Any, video_id: str) -> Optional[dict]:
    cursor = conn.execute("SELECT * FROM videos WHERE id=%s FOR UPDATE", (video_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip([column.name for column in cursor.description], row))


def _load_metadata_overrides(conn: Any, video_id: str) -> dict:
    override_row = conn.execute(
        "SELECT fields FROM video_metadata_overrides WHERE video_id=%s", (video_id,)
    ).fetchone()
    overrides = override_row[0] if override_row and isinstance(override_row[0], dict) else {}
    if override_row and isinstance(override_row[0], str):
        try:
            overrides = _core.json.loads(override_row[0])
        except (TypeError, ValueError):
            overrides = {}
    return overrides


def _drop_generated_telegram_title(conn: Any, video_id: str, overrides: dict) -> dict:
    """Remove a generated post-number title override; returns the overrides that remain."""
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
    return overrides


def _list_value(value):
    if isinstance(value, str):
        try:
            value = _core.json.loads(value or "[]")
        except (TypeError, ValueError):
            value = []
    return value if isinstance(value, list) else []


def _has_value(value) -> bool:
    return bool(value) if isinstance(value, list) else bool(str(value or "").strip())


def _same_value(left, right, field) -> bool:
    if isinstance(left, list) and isinstance(right, list):
        def normalize(items):
            return sorted(str(item).strip().casefold() for item in items)

        return normalize(left) == normalize(right)
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _plan_one_field(field: str, candidate, overrides: dict, current_value, *, trusted: bool,
                    has_scene_catalog_match: bool, matched_priority: int, current_priority: int):
    """Decide what to do with one candidate field: "auto" to apply it, "review" to propose it, or None."""
    if trusted:
        return "auto" if field not in overrides else None
    # A confirmed StashDB/ThePornDB scene match is the canonical title
    # source. Keep an explicit operator override, but replace Telegram's
    # supplied title and avoid creating a redundant review proposal.
    if field == "title" and has_scene_catalog_match and field not in overrides:
        return "auto"
    if field == "performers" and field in overrides:
        return None
    effective = overrides.get(field, current_value)
    performers_outrank = field == "performers" and matched_priority > current_priority
    if _has_value(effective):
        if not _same_value(effective, candidate, field):
            return "auto" if performers_outrank else "review"
        return "auto" if performers_outrank else None
    return "auto" if field not in overrides else None


def _plan_metadata_changes(candidates: dict, current_values: dict, overrides: dict, *, trusted: bool,
                           has_scene_catalog_match: bool, matched_priority: int, current_priority: int):
    """Split matched scene fields into automatic updates and review proposals."""
    auto_values = {}
    review_changes = {}
    for field, candidate in candidates.items():
        if not _has_value(candidate):
            continue
        kind = _plan_one_field(
            field, candidate, overrides, current_values[field], trusted=trusted,
            has_scene_catalog_match=has_scene_catalog_match, matched_priority=matched_priority,
            current_priority=current_priority,
        )
        if kind == "auto":
            auto_values[field] = candidate
        elif kind == "review":
            review_changes[field] = candidate
    return auto_values, review_changes


def _scene_update_assignments(auto_values: dict, timestamp: int, provenance: tuple, matched_source: str,
                              matched_priority: int) -> Tuple[list, list]:
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
    return assignments, params


def _update_media_for_scene(media_dir: Path, video_id: str, matched: ScrapedScene, auto_values: dict,
                            http_client: Optional[Any], conn: Any, strict: bool) -> None:
    auto_scene = replace(
        matched, title=auto_values.get("title", ""), plot=auto_values.get("plot", ""),
        studio=auto_values.get("studio", ""), date=auto_values.get("date_released", ""),
        performers=auto_values.get("performers", []), tags=auto_values.get("tags", []),
        poster_url=matched.poster_url,
    )
    _core._update_media_files(media_dir=media_dir, video_id=video_id, matched=auto_scene,
                              performers=auto_values.get("performers", []),
                              http_client=http_client, conn=conn,
                              strict=strict)


def _store_review_changes(conn: Any, video_id: str, review_changes: dict, provenance: tuple) -> None:
    from common.metadata_review import store_metadata_review_proposal
    store_metadata_review_proposal(
        conn, video_id, review_changes, source=provenance[0], external_id=provenance[1],
        source_url=provenance[2], confidence=provenance[3],
    )


def _apply_matched_scene(conn: Any, media_dir: Path, video_id: str, matched: ScrapedScene,
                        http_client: Optional[Any] = None, now: Optional[int] = None,
                        strict_media: bool = False) -> bool:
    """Apply trusted scene metadata and persist other differences as proposals."""
    timestamp = int(_core.time.time()) if now is None else int(now)
    current = _fetch_video_row(conn, video_id)
    if current is None:
        return False
    overrides = _load_metadata_overrides(conn, video_id)
    overrides = _drop_generated_telegram_title(conn, video_id, overrides)

    candidates = {
        "title": matched.title or "", "plot": matched.plot or "", "studio": matched.studio or "",
        "date_released": matched.date or "", "performers": _list_value(matched.performers),
        "tags": _list_value(matched.tags),
    }
    current_values = {
        "title": current.get("title") or "", "plot": current.get("plot") or "",
        "studio": current.get("studio") or "", "date_released": current.get("date_released") or "",
        "performers": _list_value(current.get("performers")), "tags": _list_value(current.get("tags")),
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
    auto_values, review_changes = _plan_metadata_changes(
        candidates, current_values, overrides, trusted=trusted_fingerprint or trusted_text,
        has_scene_catalog_match=has_scene_catalog_match, matched_priority=matched_priority,
        current_priority=current_priority,
    )
    if matched.poster_url and str(current.get("thumbnail") or "").strip() != matched.poster_url.strip():
        auto_values["thumbnail"] = matched.poster_url

    provenance = _core._scene_match_provenance(matched)
    assignments, params = _scene_update_assignments(auto_values, timestamp, provenance, matched_source,
                                                    matched_priority)
    params.append(video_id)
    conn.execute(f"UPDATE videos SET {','.join(assignments)} WHERE id=%s", params)
    if auto_values or matched.poster_url:
        _update_media_for_scene(media_dir, video_id, matched, auto_values, http_client, conn,
                                strict=strict_media or trusted_fingerprint or trusted_text)
    if review_changes:
        _store_review_changes(conn, video_id, review_changes, provenance)
    return bool(auto_values)


# ==============================================================================
# 4. Backfill Routine for Hub Database
# ==============================================================================

def _fetch_linked_artwork_rows(conn: Any, attempt_source: str, limit: int):
    """Linked scenes that still need artwork. None when the query is unavailable."""
    try:
        return conn.execute(
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
        return None


def _import_linked_scene_lookups():
    """(stashdb lookup, tpdb lookup), or None when the scene text sources are unavailable."""
    try:
        from sync.scene_scraper_text_sources import (
            fetch_stashdb_scene_by_id, fetch_tpdb_scene_by_id,
        )
    except ImportError as exc:
        _core.logger.warning("Linked scene artwork lookup unavailable (%s)", type(exc).__name__)
        return None
    return fetch_stashdb_scene_by_id, fetch_tpdb_scene_by_id


def _link_artwork_for_video(conn: Any, media_dir: Path, video_id: Any, external_id: Any, match_source: Any,
                            http_client: Optional[Any], attempt_source: str, lookups) -> int:
    """Fetch and store the poster for one linked scene. Returns 1 when artwork was saved."""
    fetch_stashdb, fetch_tpdb = lookups
    updated = 0
    try:
        lookup = (fetch_tpdb
                  if "theporndb" in str(match_source or "").casefold()
                  or "tpdb" in str(match_source or "").casefold()
                  else fetch_stashdb)
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
                return updated
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
    return updated


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
    rows = _fetch_linked_artwork_rows(conn, attempt_source, limit)
    if not rows:
        return 0, 0

    lookups = _import_linked_scene_lookups()
    if lookups is None:
        return 0, 0

    checked = updated = 0
    for index, (video_id, _duration, external_id, match_source) in enumerate(rows):
        if index and sleep_sec > 0:
            _core.time.sleep(sleep_sec)
        checked += 1
        updated += _link_artwork_for_video(conn, media_dir, video_id, external_id, match_source,
                                           http_client, attempt_source, lookups)
    return checked, updated
