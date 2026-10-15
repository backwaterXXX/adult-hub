"""Small process-wide spacing guard for outbound HTTP requests by host."""

from __future__ import annotations

import threading
import time
import fcntl
import json
import logging
import os
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urlsplit

HOST_REQUEST_MIN_INTERVAL_SECONDS = 0.25
_LOCK = threading.Lock()
_NEXT_REQUEST_AT: "OrderedDict[str, float]" = OrderedDict()
_MAX_TRACKED_HOSTS = 512
_LOGGER = logging.getLogger(__name__)
_DATA_DIR = Path(os.getenv("DATA_DIR") or ("/data" if Path("/data").exists() else "./data"))
_RATE_LIMIT_STATE_PATH = _DATA_DIR / "cache" / "http-rate-limit-state.json"
_RATE_LIMIT_LOCK_PATH = _RATE_LIMIT_STATE_PATH.with_suffix(_RATE_LIMIT_STATE_PATH.suffix + ".lock")
try:
    _BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
except OSError:
    _BOOT_ID = "unknown"
_COORDINATION_MODE = "shared_file"
_SHARED_STATE_WARNING_LOGGED = False
_STATS = {
    "requests": 0,
    "delayed_requests": 0,
    "total_wait_seconds": 0.0,
    "max_wait_seconds": 0.0,
}


def _reserve_shared_slot(host: str, min_interval: float) -> tuple[float, float]:
    """Reserve a host slot in the data volume shared by sync and resolver."""
    _RATE_LIMIT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _RATE_LIMIT_LOCK_PATH.open("a+", encoding="ascii") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            now = time.monotonic()
            next_by_host: "OrderedDict[str, float]" = OrderedDict()
            try:
                state = json.loads(_RATE_LIMIT_STATE_PATH.read_text(encoding="ascii"))
            except FileNotFoundError:
                state = {}
            except (OSError, ValueError):
                state = {}
            if isinstance(state, dict) and state.get("boot_id") == _BOOT_ID:
                stored = state.get("next_request_at")
                if isinstance(stored, dict):
                    for stored_host, scheduled in stored.items():
                        try:
                            value = float(scheduled)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(stored_host, str) and value > now:
                            next_by_host[stored_host] = value

            scheduled = max(now, next_by_host.get(host, now))
            next_by_host[host] = scheduled + min_interval
            next_by_host.move_to_end(host)
            while len(next_by_host) > _MAX_TRACKED_HOSTS:
                next_by_host.popitem(last=False)
            state = {"boot_id": _BOOT_ID, "next_request_at": next_by_host}
            with _RATE_LIMIT_STATE_PATH.open("w", encoding="ascii") as state_file:
                json.dump(state, state_file, separators=(",", ":"))
                state_file.flush()
            return scheduled, now
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def wait_for_host_slot(
    url: str,
    *,
    min_interval: float = HOST_REQUEST_MIN_INTERVAL_SECONDS,
) -> float:
    """Reserve a request slot for the URL host and return the wait duration.

    Reservations are shared by processes using the same DATA_DIR. Sleeping
    happens after releasing the file lock, so callers to other hosts proceed.
    If the shared state file is unavailable, the process-local limiter remains
    active and a warning is logged once.
    """
    host = urlsplit(str(url)).hostname
    if not host or min_interval <= 0:
        return 0.0
    host = host.lower()
    global _COORDINATION_MODE, _SHARED_STATE_WARNING_LOGGED
    try:
        scheduled, now = _reserve_shared_slot(host, min_interval)
        _COORDINATION_MODE = "shared_file"
    except OSError as exc:
        with _LOCK:
            _COORDINATION_MODE = "process_local"
            if not _SHARED_STATE_WARNING_LOGGED:
                _LOGGER.warning(
                    "Shared HTTP rate-limit state is unavailable (%s); using process-local coordination",
                    type(exc).__name__,
                )
                _SHARED_STATE_WARNING_LOGGED = True
        now = time.monotonic()
        with _LOCK:
            scheduled = max(now, _NEXT_REQUEST_AT.get(host, now))
            _NEXT_REQUEST_AT[host] = scheduled + min_interval
            _NEXT_REQUEST_AT.move_to_end(host)
            while len(_NEXT_REQUEST_AT) > _MAX_TRACKED_HOSTS:
                _NEXT_REQUEST_AT.popitem(last=False)
    with _LOCK:
        _STATS["requests"] += 1
        if scheduled > now:
            _STATS["delayed_requests"] += 1
            delay = scheduled - now
            _STATS["total_wait_seconds"] += delay
            _STATS["max_wait_seconds"] = max(_STATS["max_wait_seconds"], delay)
    delay = max(0.0, scheduled - now)
    if delay:
        time.sleep(delay)
    return delay


def request_with_host_limit(client, method: str, url: str, *args, **kwargs):
    """Apply the shared host spacing guard to an HTTPX-like client request."""
    wait_for_host_slot(url)
    return getattr(client, method)(url, *args, **kwargs)


def snapshot_host_rate_limit_stats(*, reset: bool = False) -> dict:
    """Return bounded aggregate limiter counters, optionally clearing them."""
    with _LOCK:
        result = {
            **_STATS,
            "total_wait_seconds": round(_STATS["total_wait_seconds"], 3),
            "max_wait_seconds": round(_STATS["max_wait_seconds"], 3),
            "min_interval_seconds": HOST_REQUEST_MIN_INTERVAL_SECONDS,
            "coordination": _COORDINATION_MODE,
        }
        if reset:
            for key in _STATS:
                _STATS[key] = 0 if key in ("requests", "delayed_requests") else 0.0
        return result
