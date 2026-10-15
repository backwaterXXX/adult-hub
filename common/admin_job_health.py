"""Shared operator-facing job health thresholds."""

import time
from datetime import datetime, timezone

# The worker refreshes ``updated_at`` every 10 seconds while processing. Allow
# several missed heartbeats before surfacing a possible stall to operators.
PROCESSING_STALE_WARNING_SECONDS = 90


def timestamp_seconds(value):
    """Normalize persisted epoch, datetime, and ISO timestamp values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        instant = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(instant.timestamp())
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        if isinstance(value, str):
            try:
                instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
                instant = instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)
                return int(instant.timestamp())
            except ValueError:
                pass
    return None


def is_processing_stale(state, updated_at, *, now=None):
    """Report stale only for processing jobs with a known old heartbeat."""
    if str(state or "").lower() != "processing":
        return False
    stamp = timestamp_seconds(updated_at)
    current = time.time() if now is None else float(now)
    return stamp is not None and 0 <= current - stamp and current - stamp > PROCESSING_STALE_WARNING_SECONDS
