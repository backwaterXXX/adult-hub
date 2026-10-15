"""Versioned schema migrations and database management for Adult Hub."""

from __future__ import annotations

from common.migrations.base import Migration, MigrationRecord, MigrationStatus
from common.migrations.runner import (
    APPLICATION_TABLES,
    MIGRATIONS,
    SCHEMA_MIGRATIONS_TABLE,
    apply_migrations,
    get_applied_migrations,
    get_migration_status,
    recreate_database,
)

__all__ = [
    "Migration",
    "MigrationRecord",
    "MigrationStatus",
    "MIGRATIONS",
    "APPLICATION_TABLES",
    "SCHEMA_MIGRATIONS_TABLE",
    "apply_migrations",
    "get_applied_migrations",
    "get_migration_status",
    "recreate_database",
]
