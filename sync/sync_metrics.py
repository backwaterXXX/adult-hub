"""Low-cardinality metrics collected during one sync run."""
from __future__ import annotations

import threading
from typing import Any, Optional
from urllib.parse import urlparse

_LOCK = threading.Lock()
_FANSDB_SCENE_ATTEMPTS: dict[str, int] = {
    "matched": 0,
    "no_match": 0,
    "error": 0,
}
_ACTIVE_METRICS: Optional[dict[str, Any]] = None


def bind_active_metrics(metrics: Optional[dict[str, Any]]) -> None:
    """Explicitly bind the per-run metrics dictionary for the active sync pass."""
    global _ACTIVE_METRICS
    with _LOCK:
        _ACTIVE_METRICS = metrics


def get_active_metrics() -> Optional[dict[str, Any]]:
    """Return the currently bound active per-run metrics dictionary, if any."""
    with _LOCK:
        return _ACTIVE_METRICS


def record_fansdb_scene_attempt(
    outcome: str,
    metrics: Optional[dict[str, Any]] = None,
) -> None:
    """Record an aggregate FansDB scene lookup attempt outcome.

    Outcome must be one of 'matched', 'no_match', 'error'.
    Maintains bounded, low-cardinality aggregate counters without any
    personalized labels or identifiers.
    """
    if outcome not in ("matched", "no_match", "error"):
        raise ValueError(f"Invalid FansDB scene outcome: {outcome!r}")
    with _LOCK:
        _FANSDB_SCENE_ATTEMPTS[outcome] += 1
        target = metrics if metrics is not None else _ACTIVE_METRICS
        if target is not None:
            target_attempts = target.setdefault(
                "fansdb_scene_attempts", {"matched": 0, "no_match": 0, "error": 0}
            )
            target_attempts[outcome] = target_attempts.get(outcome, 0) + 1


def get_fansdb_scene_metrics(*, reset: bool = False) -> dict[str, int]:
    """Return a snapshot of aggregate FansDB scene attempt counters."""
    with _LOCK:
        result = dict(_FANSDB_SCENE_ATTEMPTS)
        if reset:
            _FANSDB_SCENE_ATTEMPTS["matched"] = 0
            _FANSDB_SCENE_ATTEMPTS["no_match"] = 0
            _FANSDB_SCENE_ATTEMPTS["error"] = 0
        return result


def reset_fansdb_scene_metrics() -> None:
    """Clear aggregate FansDB scene attempt counters."""
    get_fansdb_scene_metrics(reset=True)


observe_fansdb_scene_attempt = record_fansdb_scene_attempt


def observe_sync_stage(
    metrics: Optional[dict[str, Any]], name: str, elapsed: float, failed: bool = False,
) -> None:
    if metrics is None:
        return
    item = metrics["stages"].setdefault(name, {
        "calls": 0, "failures": 0, "total_seconds": 0.0, "max_seconds": 0.0,
    })
    item["calls"] += 1
    item["failures"] += int(bool(failed))
    item["total_seconds"] = round(item["total_seconds"] + max(0.0, elapsed), 3)
    item["max_seconds"] = round(max(item["max_seconds"], elapsed), 3)


def observe_http_error(
    metrics: Optional[dict[str, Any]], url: str, error: Exception, retrying: bool,
) -> None:
    if metrics is None:
        return
    host = urlparse(url).hostname or "unknown"
    status = getattr(getattr(error, "response", None), "status_code", None)
    limited = status == 429 or "429" in str(error) or "Too Many Requests" in str(error)
    item = metrics["http_hosts"].setdefault(
        host, {"failures": 0, "retries": 0, "rate_limited": 0},
    )
    item["failures"] += 1
    item["retries"] += int(bool(retrying))
    item["rate_limited"] += int(limited)
    metrics["http"]["failed_attempts"] += 1
    metrics["http"]["retries"] += int(bool(retrying))
    metrics["http"]["rate_limited"] += int(limited)
