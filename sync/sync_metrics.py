"""Low-cardinality metrics collected during one sync run."""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse


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
