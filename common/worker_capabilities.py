"""Atomic worker capability heartbeat shared with the resolver process."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


DEFAULT_MAX_AGE_SECONDS = 15


def publish_worker_capabilities(path: str | Path, *, version: str, actions, running: bool = True,
                                now: float | None = None) -> None:
    """Publish a small, secret-free snapshot of actions supported by this worker."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_actions = sorted({action for action in actions
                           if isinstance(action, str) and action and len(action) <= 80})
    payload = {
        "schema_version": 1,
        "worker_version": str(version)[:80],
        "updated_at": float(time.time() if now is None else now),
        "running": bool(running),
        "actions": safe_actions,
    }
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o666)
        os.replace(temporary, target)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_worker_capabilities(path: str | Path, *, now: float | None = None,
                             max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> dict:
    """Return only a fresh, validated worker snapshot; missing/stale means unavailable."""
    unavailable = {"available": False, "worker_version": None, "updated_at": None,
                   "age_seconds": None, "actions": []}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            return unavailable
        if payload.get("running") is not True:
            return unavailable
        version = payload.get("worker_version")
        updated_at = payload.get("updated_at")
        actions = payload.get("actions")
        if (not isinstance(version, str) or not version or len(version) > 80
                or isinstance(updated_at, bool) or not isinstance(updated_at, (int, float))
                or not isinstance(actions, list) or len(actions) > 100
                or any(not isinstance(action, str) or not action or len(action) > 80 for action in actions)):
            return unavailable
        current = float(time.time() if now is None else now)
        age = current - float(updated_at)
        if age < -5 or age > max_age_seconds:
            return unavailable
        return {"available": True, "worker_version": version, "updated_at": float(updated_at),
                "age_seconds": max(0, int(age)), "actions": sorted(set(actions))}
    except (OSError, ValueError, TypeError, OverflowError):
        return unavailable
