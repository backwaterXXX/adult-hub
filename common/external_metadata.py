"""Durable retry and refresh state for automatic external metadata lookups."""

from __future__ import annotations

import re
import time


FOUND_REFRESH_SECONDS = 180 * 86400
NO_MATCH_REFRESH_SECONDS = 30 * 86400
ERROR_RETRY_BASE_SECONDS = 3600
ERROR_RETRY_MAX_SECONDS = 7 * 86400
ERROR_DETAIL_MAX_LENGTH = 800


_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:authorization|api[_-]?key|token|password|secret)\b"
    r"([\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)"
)
_BEARER_VALUE_RE = re.compile(r"(?i)\bBearer\s+[^\s,;\"']+")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _safe_error_detail(error: Exception | None) -> str:
    """Keep a short actionable exception message without credential material."""
    if error is None:
        return ""
    detail = str(error).strip()
    if not detail:
        return ""
    detail = _BEARER_VALUE_RE.sub("Bearer [redacted]", detail)
    detail = _SECRET_VALUE_RE.sub(lambda match: match.group(1) + "[redacted]", detail)

    def strip_url_query(match: re.Match[str]) -> str:
        url = match.group(0).rstrip(".,)")
        suffix = match.group(0)[len(url):]
        return url.split("?", 1)[0].split("#", 1)[0] + suffix

    detail = _URL_RE.sub(strip_url_query, detail)
    detail = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[redacted]@", detail)
    detail = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", detail)
    detail = " ".join(detail.split())
    return detail[:ERROR_DETAIL_MAX_LENGTH]


def record_attempt(conn, entity_type: str, entity_key: str, source: str,
                   outcome: str, *, error: Exception | None = None,
                   now: int | None = None) -> None:
    """Persist an outcome and a short sanitized exception message.

    Empty results are retried monthly because source catalogues change. Errors
    use bounded exponential backoff; successful records are refreshed twice a
    year. Error details are sanitized and length-limited; upstream response
    bodies are never stored. The sync runner is single-writer, while the
    primary key also makes this safe to call repeatedly after a crash/retry.
    """
    if outcome not in {"found", "no_match", "error"}:
        raise ValueError("unsupported external metadata outcome")
    key = str(entity_key or "").strip()
    if not key:
        raise ValueError("entity key is required")
    timestamp = int(time.time()) if now is None else int(now)
    previous = conn.execute(
        "SELECT attempt_count,outcome,error_streak FROM external_metadata_attempts "
        "WHERE entity_type=%s AND entity_key=%s AND source=%s",
        (entity_type, key, source),
    ).fetchone()
    attempts = int(previous[0] or 0) + 1 if previous else 1
    error_streak = int(previous[2] or 0) + 1 if previous and previous[1] == "error" else int(outcome == "error")
    if outcome == "found":
        delay = FOUND_REFRESH_SECONDS
    elif outcome == "no_match":
        delay = NO_MATCH_REFRESH_SECONDS
    else:
        delay = min(ERROR_RETRY_BASE_SECONDS * (2 ** min(error_streak - 1, 16)), ERROR_RETRY_MAX_SECONDS)
    error_code = type(error).__name__[:80] if error is not None else ""
    error_detail = _safe_error_detail(error)
    with conn:
        conn.execute(
            """INSERT INTO external_metadata_attempts
                   (entity_type,entity_key,source,outcome,attempt_count,error_streak,
                    last_attempt_at,next_attempt_at,last_error_code,last_error_detail,updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,to_timestamp(%s),to_timestamp(%s),%s,%s,to_timestamp(%s))
               ON CONFLICT(entity_type,entity_key,source) DO UPDATE SET
                   outcome=EXCLUDED.outcome,attempt_count=EXCLUDED.attempt_count,
                   error_streak=EXCLUDED.error_streak,
                   last_attempt_at=EXCLUDED.last_attempt_at,
                   next_attempt_at=EXCLUDED.next_attempt_at,
                   last_error_code=EXCLUDED.last_error_code,
                   last_error_detail=EXCLUDED.last_error_detail,
                   updated_at=EXCLUDED.updated_at""",
            (entity_type, key, source, outcome, attempts, error_streak, timestamp,
             timestamp + delay, error_code, error_detail, timestamp),
        )


def entity_key(name: str) -> str:
    """Normalize performer identity keys for cross-source attempt joins."""
    return str(name or "").strip().casefold()
