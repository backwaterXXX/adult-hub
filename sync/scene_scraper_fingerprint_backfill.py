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


_TELEGRAM_CHANNEL_LABEL = "Telegram"


def _record_error_attempt(conn: Any, record_id: Any, source: str, exc: BaseException, debug_message: str) -> None:
    """Store a failed attempt; a failure to store it is only logged at debug level."""
    if _core.record_attempt:
        try:
            _core.record_attempt(conn, "video", record_id, source, "error", error=exc)
        except Exception:
            _core.logger.debug(debug_message, record_id)


def _fetch_missing_oshash_rows(conn: Any, limit: int):
    """Telegram videos that still need an OSHASH. None when the query is unavailable."""
    try:
        if not _core.db.table_exists(conn, "video_fingerprints"):
            return []
        return conn.execute(
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
        return None


def _store_telegram_oshash(conn: Any, video_id: Any, http_client: Optional[Any]) -> int:
    """Compute and store one OSHASH. Returns 1 once the attempt is recorded as found."""
    oshash_result = _core._telegram_oshash(str(video_id), http_client=http_client)
    fingerprint, _bytes_read = oshash_result
    _core._store_video_fingerprint(
        conn, str(video_id), "OSHASH", fingerprint,
        media_probe=getattr(oshash_result, "media_probe", None),
    )
    if _core.record_attempt:
        _core.record_attempt(conn, "video", str(video_id), "telegram_oshash", "found")
    return 1


def backfill_missing_telegram_oshashes(
    conn: Any,
    limit: int = 200,
    sleep_sec: float = 0.1,
    http_client: Optional[Any] = None,
) -> Tuple[int, int]:
    """Calculate OSHASH only for Telegram videos that do not have one yet."""
    if not conn:
        return 0, 0
    rows = _fetch_missing_oshash_rows(conn, limit)
    if rows is None:
        return 0, 0

    checked = computed = 0
    for index, (video_id,) in enumerate(rows):
        if index and sleep_sec > 0:
            _core.time.sleep(sleep_sec)
        checked += 1
        try:
            computed += _store_telegram_oshash(conn, video_id, http_client)
        except (DailyTelegramTransferBudgetExceeded, DailyTelegramTransferBudgetUnavailable) as exc:
            _record_error_attempt(conn, str(video_id), "telegram_oshash", exc,
                                  "Could not save daily Telegram transfer cap state for %s")
            _core.logger.info(
                "Telegram fingerprint pass paused because the shared transfer budget is exhausted or unavailable (%s)",
                video_id,
            )
            break
        except Exception as exc:
            _record_error_attempt(conn, str(video_id), "telegram_oshash", exc,
                                  "Could not save Telegram OSHASH retry state for %s")
            _core.logger.warning("Telegram OSHASH calculation failed for %s (%s)", video_id, type(exc).__name__)
    if rows:
        _core.logger.info("Telegram OSHASH backfill: checked=%d computed=%d", checked, computed)
    return checked, computed


def _fingerprint_feature_enabled(name: str, legacy_name: str, *, default: bool = False) -> bool:
    value = _core.os.environ.get(name)
    if value is None:
        value = _core.os.environ.get(legacy_name, "true" if default else "")
    return str(value).strip().lower() in {"1", "true", "yes"}


def _fetch_telegram_fingerprint_rows(conn: Any, video_ids: Optional[List[str]], channel_pattern: str,
                                     attempt_source: str, limit: int):
    if video_ids is not None:
        return conn.execute(
            """SELECT v.id,v.title,v.duration,v.studio
               FROM videos v WHERE v.id = ANY(%s) AND v.provider='telegram'
                 AND v.id LIKE %s ORDER BY v.id""",
            (video_ids, channel_pattern),
        ).fetchall()
    return conn.execute(
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


def _oshash_for_video(conn: Any, video_id: Any, http_client: Optional[Any]) -> Tuple[str, int]:
    """Return (fingerprint, bytes read now): the cached OSHASH, or a freshly computed and stored one."""
    fingerprint = _core._cached_video_fingerprint(conn, video_id, "OSHASH")
    if fingerprint:
        _core.logger.info("Using cached %s OSHASH for %s", _TELEGRAM_CHANNEL_LABEL, video_id)
        return fingerprint, 0
    oshash_result = _core._telegram_oshash(video_id, http_client=http_client)
    fingerprint, bytes_read = oshash_result
    _core._store_video_fingerprint(
        conn, video_id, "OSHASH", fingerprint,
        media_probe=getattr(oshash_result, "media_probe", None),
    )
    return fingerprint, bytes_read


def _phash_settings(default_timeout: int) -> Tuple[int, int, int]:
    """(byte budget, timeout seconds, bytes per second) for pHash reads from the environment."""
    configured_budget = int(_core.os.environ.get(
        "TELEGRAM_PHASH_BYTE_BUDGET",
        _core.os.environ.get("LUKAYAN_PHASH_BYTE_BUDGET", str(_core.TELEGRAM_PHASH_BYTE_BUDGET)),
    ))
    configured_timeout = int(_core.os.environ.get(
        "TELEGRAM_PHASH_TIMEOUT_SECONDS",
        _core.os.environ.get("LUKAYAN_PHASH_TIMEOUT_SECONDS", str(default_timeout)),
    ))
    configured_rate = int(_core.os.environ.get(
        "TELEGRAM_FINGERPRINT_MAX_BYTES_PER_SECOND", str(2 * 1024 * 1024),
    ))
    return configured_budget, configured_timeout, configured_rate


def _phash_size_sink(conn: Any, video_id: str) -> Callable[[int], None]:
    def _record_phash_size(size_bytes: int) -> None:
        # pHash learns the true total from Content-Length; use it
        # only when the catalog has no size yet.
        with conn:
            conn.execute(
                "UPDATE videos SET file_size_bytes=%s "
                "WHERE id=%s AND file_size_bytes IS NULL",
                (int(size_bytes), video_id),
            )
    return _record_phash_size


def _phash_match_for_video(conn: Any, video_id: Any, duration: Any, started: float, bytes_read: int,
                           http_client: Optional[Any]) -> Tuple[Any, int]:
    """Return (matched scene or None, bytes read for the pHash)."""
    from video_fingerprints import DEFAULT_TIMEOUT_SECONDS, telegram_phash
    configured_budget, configured_timeout, configured_rate = _phash_settings(DEFAULT_TIMEOUT_SECONDS)
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
            size_sink=_phash_size_sink(conn, str(video_id)),
        )
        _core._store_video_fingerprint(conn, video_id, "PHASH", phash)
    matched = _core.search_scene_by_phash(
        phash, http_client=http_client, duration=int(duration or 0),
        max_distance=10, duration_tolerance=2,
    )
    return matched, phash_bytes


def _find_telegram_match(conn: Any, video_id: Any, duration: Any,
                         http_client: Optional[Any]) -> Tuple[Any, str, int]:
    """Return (matched scene or None, method name, total bytes read) for one Telegram video."""
    started = _core.time.monotonic()
    fingerprint, bytes_read = _oshash_for_video(conn, video_id, http_client)
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
        matched, phash_bytes = _phash_match_for_video(conn, video_id, duration, started, bytes_read, http_client)
        total_bytes += phash_bytes
        method = "phash"
    return matched, method, total_bytes


def _backfill_telegram_video(conn: Any, media_dir: Path, video_id: Any, duration: Any,
                             http_client: Optional[Any], attempt_source: str) -> Tuple[int, bool]:
    """Enrich one Telegram video. Returns (scenes applied, True when the transfer budget paused the pass)."""
    updated = 0
    try:
        matched, method, total_bytes = _find_telegram_match(conn, video_id, duration, http_client)
        if matched:
            with conn:
                updated += int(_core._apply_matched_scene(
                    conn, media_dir, str(video_id), matched, http_client=http_client,
                ))
            outcome = "found"
            _core.logger.info("%s %s matched %s as %r (%d bytes read)", _TELEGRAM_CHANNEL_LABEL, method, video_id, matched.title, total_bytes)
        else:
            outcome = "no_match"
            _core.logger.info("%s fingerprints found no catalogue entry for %s (%d bytes read)", _TELEGRAM_CHANNEL_LABEL, video_id, total_bytes)
        if _core.record_attempt:
            _core.record_attempt(conn, "video", video_id, attempt_source, outcome)
    except (DailyTelegramTransferBudgetExceeded, DailyTelegramTransferBudgetUnavailable) as exc:
        _record_error_attempt(conn, video_id, attempt_source, exc,
                              "Could not save daily Telegram transfer cap state for %s")
        _core.logger.info(
            "Telegram fingerprint pass paused because the shared transfer budget is exhausted or unavailable (%s)",
            video_id,
        )
        return updated, True
    except Exception as exc:
        _record_error_attempt(conn, video_id, attempt_source, exc,
                              "Could not save OSHASH retry state for %s")
        _core.logger.warning("%s OSHASH enrichment failed for %s (%s)", _TELEGRAM_CHANNEL_LABEL, video_id, type(exc).__name__)
    return updated, False


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
    attempt_source = "telegram_fingerprint"
    rows = _fetch_telegram_fingerprint_rows(conn, video_ids, channel_pattern, attempt_source, limit)

    checked = 0
    updated = 0
    if progress_callback:
        progress_callback(0, len(rows))
    for idx, (video_id, _old_title, duration, _old_studio) in enumerate(rows):
        if idx and sleep_sec > 0:
            _core.time.sleep(sleep_sec)
        checked += 1
        applied, paused = _backfill_telegram_video(conn, media_dir, video_id, duration, http_client, attempt_source)
        updated += applied
        if progress_callback:
            progress_callback(checked, len(rows))
        if paused:
            break
    return checked, updated
