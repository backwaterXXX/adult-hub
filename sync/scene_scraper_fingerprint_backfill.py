"""OSHASH and pHash backfill flows for Telegram videos."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple
from common.telegram_transfer_budget import (
    DailyTelegramTransferBudgetExceeded,
    DailyTelegramTransferBudgetUnavailable,
)

if __package__:
    from . import scene_scrapers as _core
else:
    import scene_scrapers as _core


def _telegram_oshash_batch_limit() -> int:
    try:
        return max(1, min(500, int(_core.os.environ.get("TELEGRAM_OSHASH_BACKFILL_PER_RUN", "200"))))
    except (TypeError, ValueError):
        return 200


def backfill_missing_telegram_oshashes(
    conn: Any,
    limit: int = 200,
    sleep_sec: float = 0.1,
    http_client: Optional[Any] = None,
) -> Tuple[int, int]:
    """Calculate OSHASH only for Telegram videos that do not have one yet."""
    if not conn:
        return 0, 0
    try:
        if not _core.db.table_exists(conn, "video_fingerprints"):
            return 0, 0
        rows = conn.execute(
            """SELECT v.id
               FROM videos v
               LEFT JOIN video_fingerprints f
                 ON f.video_id=v.id AND f.algorithm='OSHASH'
               LEFT JOIN external_metadata_attempts a
                 ON a.entity_type='video' AND a.entity_key=v.id AND a.source='telegram_oshash'
               WHERE v.provider='telegram' AND f.video_id IS NULL
                 AND (a.entity_key IS NULL OR a.outcome <> 'error'
                      OR a.next_attempt_at <= CURRENT_TIMESTAMP)
               ORDER BY a.last_attempt_at ASC NULLS FIRST,v.first_seen ASC NULLS LAST,v.id ASC
               LIMIT %s""",
            (max(1, min(500, int(limit))),),
        ).fetchall()
    except Exception as exc:
        _core.logger.warning("Telegram OSHASH backfill query failed (%s)", type(exc).__name__)
        return 0, 0

    checked = computed = 0
    for index, (video_id,) in enumerate(rows):
        if index and sleep_sec > 0:
            _core.time.sleep(sleep_sec)
        checked += 1
        try:
            oshash_result = _core._telegram_oshash(str(video_id), http_client=http_client)
            fingerprint, _bytes_read = oshash_result
            _core._store_video_fingerprint(
                conn, str(video_id), "OSHASH", fingerprint,
                media_probe=getattr(oshash_result, "media_probe", None),
            )
            if _core.record_attempt:
                _core.record_attempt(conn, "video", str(video_id), "telegram_oshash", "found")
            computed += 1
        except (DailyTelegramTransferBudgetExceeded, DailyTelegramTransferBudgetUnavailable) as exc:
            if _core.record_attempt:
                try:
                    _core.record_attempt(conn, "video", str(video_id), "telegram_oshash", "error", error=exc)
                except Exception:
                    _core.logger.debug("Could not save daily Telegram transfer cap state for %s", video_id)
            _core.logger.info(
                "Telegram fingerprint pass paused because the shared transfer budget is exhausted or unavailable (%s)",
                video_id,
            )
            break
        except Exception as exc:
            if _core.record_attempt:
                try:
                    _core.record_attempt(conn, "video", str(video_id), "telegram_oshash", "error", error=exc)
                except Exception:
                    _core.logger.debug("Could not save Telegram OSHASH retry state for %s", video_id)
            _core.logger.warning("Telegram OSHASH calculation failed for %s (%s)", video_id, type(exc).__name__)
    if rows:
        _core.logger.info("Telegram OSHASH backfill: checked=%d computed=%d", checked, computed)
    return checked, computed


def _fingerprint_feature_enabled(name: str, legacy_name: str, *, default: bool = False) -> bool:
    value = _core.os.environ.get(name)
    if value is None:
        value = _core.os.environ.get(legacy_name, "true" if default else "")
    return str(value).strip().lower() in {"1", "true", "yes"}


def backfill_telegram_fingerprints(
    conn: Any,
    media_dir: Path,
    limit: int = 20,
    sleep_sec: float = 0.5,
    http_client: Optional[Any] = None,
    video_ids: Optional[List[str]] = None,
    channel_id: Optional[str] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> Tuple[int, int]:
    """Enrich Telegram uploads through OSHASH, then duration-gated pHash.

    Numeric filenames and promotional captions have no useful text-search
    signal. OSHASH lookups require an exact hash and a duration match. When no
    catalogue match is found, pHash can match visually similar scenes within
    the configured Hamming-distance and duration limits. Fingerprints are
    cached in the database, so retries can query catalogues without rereading
    the Telegram file. Scheduled callers process every Telegram channel in
    bounded batches; ``channel_id`` only narrows explicit runs.
    """
    if not conn or not media_dir:
        return 0, 0
    if not _core._fingerprint_feature_enabled(
        "TELEGRAM_FINGERPRINTS_ENABLED", "LUKAYAN_FINGERPRINTS_ENABLED", default=True,
    ):
        return 0, 0
    channel_pattern = f"telegram:{channel_id}:%" if channel_id else "telegram:%"
    channel_label = "Telegram"
    attempt_source = "telegram_fingerprint"
    if video_ids is not None:
        rows = conn.execute(
            """SELECT v.id,v.title,v.duration,v.studio
               FROM videos v WHERE v.id = ANY(%s) AND v.provider='telegram'
                 AND v.id LIKE %s ORDER BY v.id""",
            (video_ids, channel_pattern),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT v.id,v.title,v.duration,v.studio
               FROM videos v
               LEFT JOIN external_metadata_attempts a
                 ON a.entity_type='video' AND a.entity_key=v.id AND a.source=%s
               WHERE v.provider='telegram' AND v.id LIKE %s
                 AND (a.entity_key IS NULL OR a.next_attempt_at <= CURRENT_TIMESTAMP)
               ORDER BY a.last_attempt_at ASC NULLS FIRST,v.first_seen ASC,v.id ASC
               LIMIT %s""",
            (attempt_source, channel_pattern, max(1, int(limit))),
        ).fetchall()

    checked = 0
    updated = 0
    if progress_callback:
        progress_callback(0, len(rows))
    for idx, (video_id, old_title, duration, old_studio) in enumerate(rows):
        if idx and sleep_sec > 0:
            _core.time.sleep(sleep_sec)
        checked += 1
        try:
            started = _core.time.monotonic()
            fingerprint = _core._cached_video_fingerprint(conn, video_id, "OSHASH")
            if fingerprint:
                bytes_read = 0
                _core.logger.info("Using cached %s OSHASH for %s", channel_label, video_id)
            else:
                oshash_result = _core._telegram_oshash(video_id, http_client=http_client)
                fingerprint, bytes_read = oshash_result
                _core._store_video_fingerprint(
                    conn, video_id, "OSHASH", fingerprint,
                    media_probe=getattr(oshash_result, "media_probe", None),
                )
            # OSHASH is a useful lookup/candidate signal, not cryptographic proof
            # of identical media. Keep duplicate decisions in the operator's
            # preview/execute flow instead of hiding files during metadata sync.
            matched = _core.search_scene_by_oshash(
                fingerprint, http_client=http_client, duration=int(duration or 0),
                duration_tolerance=2,
            )
            method = "oshash"
            total_bytes = bytes_read
            if (matched is None and _core._fingerprint_feature_enabled(
                    "TELEGRAM_PHASH_ENABLED", "LUKAYAN_PHASH_ENABLED", default=True)):
                from video_fingerprints import DEFAULT_TIMEOUT_SECONDS, telegram_phash
                configured_budget = int(_core.os.environ.get(
                    "TELEGRAM_PHASH_BYTE_BUDGET",
                    _core.os.environ.get("LUKAYAN_PHASH_BYTE_BUDGET", str(_core.TELEGRAM_PHASH_BYTE_BUDGET)),
                ))
                configured_timeout = int(_core.os.environ.get(
                    "TELEGRAM_PHASH_TIMEOUT_SECONDS",
                    _core.os.environ.get("LUKAYAN_PHASH_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)),
                ))
                configured_rate = int(_core.os.environ.get(
                    "TELEGRAM_FINGERPRINT_MAX_BYTES_PER_SECOND", str(256 * 1024),
                ))
                phash = _core._cached_video_fingerprint(conn, video_id, "PHASH")
                if phash:
                    phash_bytes = 0
                    _core.logger.info("Using cached Telegram pHash for %s", video_id)
                else:
                    remaining_budget = max(0, configured_budget - bytes_read)
                    remaining_time = max(1, configured_timeout - int(_core.time.monotonic() - started))
                    phash, phash_bytes = telegram_phash(
                        video_id, int(duration or 0), byte_budget=remaining_budget,
                        timeout_seconds=remaining_time, max_bytes_per_second=configured_rate,
                    )
                    _core._store_video_fingerprint(conn, video_id, "PHASH", phash)
                total_bytes += phash_bytes
                matched = _core.search_scene_by_phash(
                    phash, http_client=http_client, duration=int(duration or 0),
                    max_distance=10, duration_tolerance=2,
                )
                method = "phash"
            if matched:
                with conn:
                    updated += int(_core._apply_matched_scene(
                        conn, media_dir, str(video_id), matched, http_client=http_client,
                    ))
                outcome = "found"
                _core.logger.info("%s %s matched %s as %r (%d bytes read)", channel_label, method, video_id, matched.title, total_bytes)
            else:
                outcome = "no_match"
                _core.logger.info("%s fingerprints found no catalogue entry for %s (%d bytes read)", channel_label, video_id, total_bytes)
            if _core.record_attempt:
                _core.record_attempt(conn, "video", video_id, attempt_source, outcome)
        except (DailyTelegramTransferBudgetExceeded, DailyTelegramTransferBudgetUnavailable) as exc:
            if _core.record_attempt:
                try:
                    _core.record_attempt(conn, "video", video_id, attempt_source, "error", error=exc)
                except Exception:
                    _core.logger.debug("Could not save daily Telegram transfer cap state for %s", video_id)
            _core.logger.info(
                "Telegram fingerprint pass paused because the shared transfer budget is exhausted or unavailable (%s)",
                video_id,
            )
            if progress_callback:
                progress_callback(checked, len(rows))
            break
        except Exception as exc:
            if _core.record_attempt:
                try:
                    _core.record_attempt(conn, "video", video_id, attempt_source, "error", error=exc)
                except Exception:
                    _core.logger.debug("Could not save OSHASH retry state for %s", video_id)
            _core.logger.warning("%s OSHASH enrichment failed for %s (%s)", channel_label, video_id, type(exc).__name__)
        if progress_callback:
            progress_callback(checked, len(rows))
    return checked, updated
