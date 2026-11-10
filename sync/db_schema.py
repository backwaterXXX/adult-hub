"""PostgreSQL schema creation and versioned migrations for the hub database.

Backend is a PostgreSQL 17 server.
Delegates to the versioned migration runner in ``common.migrations``.
"""

from __future__ import annotations

from typing import Callable

from common.db import Connection, connect
from common.migrations import apply_migrations


def init_db(conn_or_url: Connection | str | None = None, *,
            map_category_fn: Callable[[str], str | None] | None = None) -> Connection:
    """Create/upgrade all hub tables via versioned migrations; return connection."""
    if isinstance(conn_or_url, Connection):
        conn = conn_or_url
    else:
        conn = connect(conn_or_url or None)

    apply_migrations(conn, map_category_fn=map_category_fn)
    return conn


# Backwards-compatible alias for older imports.
initdb = init_db
