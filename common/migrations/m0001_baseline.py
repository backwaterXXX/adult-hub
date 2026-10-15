"""Migration 0001: canonical baseline schema for Adult Hub."""

from __future__ import annotations

from typing import Any

from common.migrations.base import Migration
from common.migrations.legacy import LEGACY_MIGRATIONS


_REQUIRED_TABLES = (
    "videos",
    "video_performers",
    "video_metadata_migration_issues",
    "video_performer_migration_issues",
    "performer_bio_migration_issues",
    "performer_bios",
    "video_categories",
    "video_sources",
    "synced_files",
    "dead_streams",
    "stream_cache",
    "diagnostic_reports",
)
_REQUIRED_COLUMNS = (
    ("videos", "performers", "jsonb"),
    ("videos", "categories", "jsonb"),
    ("videos", "tags", "jsonb"),
    ("videos", "first_seen", "timestamp with time zone"),
    ("performer_bios", "aliases", "jsonb"),
    ("stream_cache", "expire_at", "timestamp with time zone"),
)


def _validate_existing_schema(conn: Any) -> None:
    missing_tables = [
        name for name in _REQUIRED_TABLES
        if conn.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is None
    ]
    if missing_tables:
        raise RuntimeError(
            "Cannot mark the baseline on an incomplete existing schema; missing tables: "
            + ", ".join(missing_tables)
        )

    actual = {
        (row[0], row[1]): row[2]
        for row in conn.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        ).fetchall()
    }
    wrong_types = [
        f"{table}.{column} (expected {expected}, got {actual.get((table, column), 'missing')})"
        for table, column, expected in _REQUIRED_COLUMNS
        if actual.get((table, column)) != expected
    ]
    if wrong_types:
        raise RuntimeError(
            "Cannot mark the baseline on a schema with incompatible column types: "
            + ", ".join(wrong_types)
        )


def apply(conn: Any, **kwargs) -> None:
    # A new database is initialized in one tracked migration. Legacy DDL
    # functions are reused here so the baseline stays identical to the schema
    # that existing databases reach through the upgrade path.
    has_videos = conn.execute("SELECT to_regclass('videos')").fetchone()[0] is not None
    if not has_videos:
        for migration in LEGACY_MIGRATIONS:
            migration.apply(conn, **kwargs)

    # Existing databases reach the baseline only after their legacy chain has
    # completed. Validate first; never rewrite application data here.
    _validate_existing_schema(conn)


migration = Migration(
    version=1,
    name="0001_baseline",
    description="Canonical baseline schema for new installs and upgraded databases",
    apply_fn=apply,
)
