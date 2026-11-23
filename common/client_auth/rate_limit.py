"""PostgreSQL-backed distributed rate limiter for authentication and TV pairing."""

from __future__ import annotations

import time


def check_and_increment_rate_limit(
    conn,
    rate_key: str,
    max_attempts: int,
    window_seconds: int,
) -> bool:
    """Atomically record an attempt and return whether the request is within limit.

    Commits atomically so that rate limits persist even when the calling
    operation later fails or raises an exception.
    Prunes globally expired windows older than 1 hour.
    Returns True if allowed (attempts <= max_attempts), False if rate-limited.
    """
    now_epoch = int(time.time())
    window_epoch = (now_epoch // window_seconds) * window_seconds

    # Global prune of expired windows older than 1 hour (bounded garbage cleanup)
    global_cutoff = now_epoch - 3600
    conn.execute(
        "DELETE FROM client_auth_rate_limits WHERE window_epoch < %s",
        (global_cutoff,),
    )

    # Atomic upsert attempt count
    cursor = conn.execute(
        "INSERT INTO client_auth_rate_limits (rate_key, window_epoch, attempts) "
        "VALUES (%s, %s, 1) "
        "ON CONFLICT (rate_key, window_epoch) "
        "DO UPDATE SET attempts = client_auth_rate_limits.attempts + 1 "
        "RETURNING attempts",
        (rate_key, window_epoch),
    )
    row = cursor.fetchone()
    current_attempts = row[0] if row else 1
    # Commit immediately so counter persists regardless of downstream exceptions
    conn.commit()
    return current_attempts <= max_attempts
