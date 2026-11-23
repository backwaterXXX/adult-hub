"""Database schema definitions for Direct Play Client API authentication."""

from __future__ import annotations

from common.migrations.m0070_client_auth import apply as apply_auth_schema


def ensure_auth_schema(conn) -> None:
    """Ensure auth tables, constraints, and indexes exist in PostgreSQL.

    Delegates to versioned migration m0070_client_auth.
    Note: runtime request handlers must never invoke this function.
    """
    apply_auth_schema(conn)
