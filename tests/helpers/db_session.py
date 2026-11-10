"""Scoped PostgreSQL sessions for tests.

``common.db.Connection`` only commits or rolls back in ``__exit__``; it does not
close the socket. ``db_session`` keeps the same commit/rollback semantics and
closes the connection in a ``finally`` block, so tests do not leak server slots.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from common import db


@contextmanager
def db_session(url: str | None = None) -> Iterator[db.Connection]:
    """Yield a connection that commits on success, rolls back on error, and is always closed."""
    conn = db.connect(url)
    try:
        with conn:
            yield conn
    finally:
        conn.close()
