"""Expiry for the byte-range cache the fingerprint workers fill.

Every cache directory is named after a hash of the job and the video, and the
client that creates it writes a ``usage.json`` ledger with the charge of the day.
A directory with a readable ledger expires on the short TTL. A directory without
one used to live forever, which is how the cache reached gigabytes of ranges for
jobs whose ledger was written by an older build; the fallback is the directory
mtime with a much longer grace, so a job that is still filling its directory is
safe and abandoned directories still go away.
"""

from __future__ import annotations

import json
import os
import shutil
import time

DEFAULT_TTL_SECONDS = 24 * 60 * 60
LEGACY_GRACE_SECONDS = 7 * 24 * 60 * 60
IDENTITY_LENGTH = 64


def _ledger_timestamp(path: str) -> float | None:
    """Return the ledger's ``updated_at``, or None when there is no usable ledger."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        updated_at = data["updated_at"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return updated_at if type(updated_at) in (int, float) else None


def _directory_age(entry: os.DirEntry, now: float) -> float:
    """Age of a directory by its mtime; a stat failure counts as freshly written."""
    try:
        return now - entry.stat(follow_symlinks=False).st_mtime
    except OSError:
        return 0.0


def purge_expired(
    cache_dir: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    legacy_grace_seconds: int = LEGACY_GRACE_SECONDS,
    now: float | None = None,
) -> int:
    """Remove the range caches past their TTL and return how many went away."""
    now = time.time() if now is None else now
    try:
        entries = os.scandir(cache_dir)
    except FileNotFoundError:
        return 0
    removed = 0
    with entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False) or len(entry.name) != IDENTITY_LENGTH:
                continue
            updated_at = _ledger_timestamp(os.path.join(entry.path, "usage.json"))
            if updated_at is None:
                if _directory_age(entry, now) < legacy_grace_seconds:
                    continue
            elif now - updated_at < ttl_seconds:
                continue
            try:
                shutil.rmtree(entry.path)
            except OSError:
                continue
            removed += 1
    return removed
