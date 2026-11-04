"""Cross-process leases for active Telegram playback streams.

The resolver and its pHash subprocesses share ``/data``. A short file lease
lets pHash throttle itself while a real Telegram playback request is open,
without relying on optional playback diagnostics.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = "/data/cache/active-telegram-playbacks.json"
LEASE_SECONDS = 90


def _paths() -> tuple[Path, Path]:
    path = Path(os.environ.get("TELEGRAM_PLAYBACK_ACTIVITY_PATH", DEFAULT_PATH))
    return path, path.with_suffix(path.suffix + ".lock")


def _read(path: Path, now: float) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("invalid playback activity file")
        return {str(key): float(expiry) for key, expiry in raw.items()
                if isinstance(expiry, (int, float)) and expiry > now}
    except FileNotFoundError:
        return {}


def _update(mutator):
    path, lock_path = _paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        now = time.time()
        entries = _read(path, now)
        result = mutator(entries, now)
        fd, temporary = tempfile.mkstemp(prefix="telegram-playback-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(entries, output, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return result


def begin_playback() -> str:
    """Register an active playback lease and return its opaque handle."""
    handle = uuid.uuid4().hex
    _update(lambda entries, now: entries.__setitem__(handle, now + LEASE_SECONDS))
    return handle


def refresh_playback(handle: str) -> None:
    """Renew an active playback lease."""
    if handle:
        _update(lambda entries, now: entries.__setitem__(handle, now + LEASE_SECONDS))


def end_playback(handle: str) -> None:
    """Remove an active playback lease."""
    if handle:
        _update(lambda entries, _now: entries.pop(handle, None))


def has_active_playback() -> bool:
    """Return whether a Telegram stream is active; fail closed on ledger errors."""
    path, lock_path = _paths()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            active = bool(_read(path, time.time()))
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return active
    except Exception as _exc:
        logger.debug("Ignored exception in %s", "has_active_playback", exc_info=True)
        # An unreadable lease file must keep background reads at the safe rate.
        return True
