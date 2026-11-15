"""Shared primitives for PostgreSQL-backed durable job queues.

Both ``job_queue`` (video_duplicate_jobs) and ``phash_jobs`` (video_phash_jobs)
implement the same claim-heartbeat-finish state machine.  Code that is identical
or structurally equivalent in both modules lives here so a fix or improvement
reaches both queues at once (M-5).

Rules for adding to this module:
- Functions must be table-agnostic; callers supply the table name.
- Use ``psycopg.sql`` for identifiers to prevent SQL injection.
- Keep functions small and independently testable.
"""

from __future__ import annotations

import json
import uuid


def generate_lease_token() -> str:
    """Return a fresh opaque lease token (UUID hex, 32 chars)."""
    return uuid.uuid4().hex


def generate_job_id(prefix: str = "") -> str:
    """Return a fresh job identifier, optionally prefixed."""
    return prefix + uuid.uuid4().hex


def advisory_xact_lock(conn, key: str) -> None:
    """Acquire a transaction-scoped advisory lock by hashing *key*.

    The lock is released automatically when the surrounding transaction
    commits or rolls back — no explicit unlock needed.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))


def serialize_result(result: dict, *, max_bytes: int) -> str:
    """JSON-encode *result*, replacing it with a truncation notice if too large.

    This prevents result payloads from growing without bound and blocking
    terminal writes.  The job is still marked terminal; diagnostic context
    is preserved in the replacement payload.
    """
    serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized.encode("utf-8")) <= max_bytes:
        return serialized
    return json.dumps(
        {
            "result_truncated": True,
            "original_size_bytes": len(serialized.encode("utf-8")),
            "message": "Job result exceeded the storage limit; detailed result was omitted.",
        },
        ensure_ascii=False,
    )
