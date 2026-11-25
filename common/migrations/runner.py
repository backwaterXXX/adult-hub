"""Migration runner and schema recreation engine for Adult Hub.

The schema has a single tracked migration: the squashed baseline (version 1),
which runs ``schema_baseline.sql`` in one transaction. A 0.6.0 database
therefore records exactly one row in ``schema_migrations``.

Databases that still carry the numbered pre-0.6.0 history cannot be upgraded in
place: that chain was removed with the squash. They are refused with a clear
error instead of being silently mis-stamped, because the only safe paths are a
dump restore or ``recreate_database``. Future schema changes are new numbered
migrations appended to ``MIGRATIONS`` and mirrored in ``schema_baseline.sql``.
"""

from __future__ import annotations

import logging
from typing import Any

from common import db
from common.migrations.base import Migration, MigrationRecord, MigrationStatus
from common.migrations.m0001_baseline import migration as baseline

logger = logging.getLogger(__name__)

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"
MIGRATION_LOCK_ID = 0xAD01780B

MIGRATIONS: tuple[Migration, ...] = (baseline,)


def _validate_active_migrations(migrations: tuple[Migration, ...]) -> None:
    """Reject duplicate or out-of-order versions in a chain."""
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError("Active migration versions must be unique")
    if versions != sorted(versions):
        raise ValueError("Active migrations must be ordered by version")


_validate_active_migrations(MIGRATIONS)

USER_STATE_TABLES: tuple[str, ...] = (
    "client_users",
    "client_user_watch_progress",
    "client_user_favorites",
)

AUTH_TABLES: tuple[str, ...] = (
    "client_session_spent_tokens",
    "client_sessions",
    "client_pairing_sessions",
    "client_auth_rate_limits",
)

PRESERVED_RECREATE_TABLES: tuple[str, ...] = (
    *USER_STATE_TABLES,
    "client_sessions",
    "client_session_spent_tokens",
)

APPLICATION_TABLES: tuple[str, ...] = (
    "video_sources",
    "video_categories",
    "video_performers",
    "video_performer_migration_issues",
    "video_metadata_migration_issues",
    "performer_bio_migration_issues",
    "synced_files",
    "video_fingerprints",
    "video_phash_provenance",
    "video_phash_thumbnails",
    "video_phash_jobs",
    "video_visual_signatures",
    "video_visual_comparisons",
    "video_visual_jobs",
    "video_visual_state",
    "video_visual_history",
    "videos",
    "dead_streams",
    "meta",
    "video_duplicate_jobs",
    "video_duplicate_decisions",
    "video_duplicate_dismissals",
    "telegram_accounts",
    "telegram_account_channels",
    "telegram_channel_state",
    "performer_aliases",
    "performer_bios",
    "performer_photos",
    "performer_catalog",
    "performer_catalog_aliases",
    "performer_profile_overrides",
    "telegram_performer_proposals",
    "telegram_performer_rebuild_backup",
    "stream_cache",
    "diagnostic_settings",
    "diagnostic_reports",
    "admin_operation_plans",
    "video_metadata_overrides",
    "video_availability_checks",
    "telegram_unavailable_quarantine",
    "admin_audit_items",
    "admin_schedules",
    "system_schedules",
    "system_schedule_stages",
    "admin_action_events",
    "admin_audit_events",
    "admin_orphan_quarantine",
    "admin_quality_acknowledgements",
    "external_metadata_attempts",
    "metadata_review_proposals",
    *USER_STATE_TABLES,
    *AUTH_TABLES,
)


def _resolve_connection(conn_or_url: db.Connection | str | None) -> tuple[db.Connection, bool]:
    """Return (connection, owned). Caller must close owned connections."""
    if isinstance(conn_or_url, db.Connection):
        return conn_or_url, False
    return db.connect(conn_or_url), True


def ensure_schema_migrations_table(conn: db.Connection) -> None:
    """Ensure the version tracking table exists."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_MIGRATIONS_TABLE} (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def get_applied_migrations(conn: db.Connection) -> list[MigrationRecord]:
    """List all migrations applied in the target database."""
    ensure_schema_migrations_table(conn)
    rows = conn.execute(
        f"SELECT version, name, applied_at FROM {SCHEMA_MIGRATIONS_TABLE} ORDER BY version"
    ).fetchall()
    conn.commit()
    return [
        MigrationRecord(version=int(r[0]), name=str(r[1]), applied_at=r[2])
        for r in rows
    ]


def _has_tables(conn: db.Connection) -> bool:
    """True once any application schema is already present."""
    return conn.execute("SELECT to_regclass('videos')").fetchone()[0] is not None


def _baseline_is_recorded(applied: list[MigrationRecord]) -> bool:
    """True when history proves the database belongs to the squashed schema."""
    return any(
        record.version == baseline.version and record.name == baseline.name
        for record in applied
    )


def _migration_plan(applied: list[MigrationRecord]) -> list[Migration]:
    """Migrations this database still needs, by recorded history alone."""
    applied_versions = {record.version for record in applied}
    if _baseline_is_recorded(applied):
        return [
            migration for migration in MIGRATIONS
            if migration.version not in applied_versions
        ]
    return list(MIGRATIONS)


def _pre_squash_history(applied: list[MigrationRecord]) -> RuntimeError:
    recorded = ", ".join(str(record.version) for record in applied) or "nothing"
    return RuntimeError(
        "Schema history predates the 0.6.0 squash: the database holds hub tables but "
        f"records {recorded} and no 0001_baseline row. The numbered upgrade chain was "
        "removed, so this schema cannot be upgraded in place. Restore a dump of this "
        "database, or run 'python3 migrate_db.py recreate' if its contents are disposable."
    )


def _plan_for_connection(
    conn: db.Connection,
    applied: list[MigrationRecord],
) -> list[Migration]:
    """Resolve pending migrations, refusing schemas the squash cannot upgrade.

    Tables without a recorded baseline mean either a pre-0.6.0 database or a
    database whose ``schema_migrations`` was lost. Replaying the baseline over
    either one would fail halfway or stamp an unknown schema as current, so the
    runner says no and names the escape hatch.
    """
    plan = _migration_plan(applied)
    if plan and not _baseline_is_recorded(applied) and _has_tables(conn):
        raise _pre_squash_history(applied)
    return plan


def get_migration_status(conn_or_url: db.Connection | str | None = None) -> MigrationStatus:
    """Inspect current schema migration status."""
    conn, owned = _resolve_connection(conn_or_url)
    try:
        applied = get_applied_migrations(conn)
        pending = _plan_for_connection(conn, applied)
        curr_ver = max((rec.version for rec in applied), default=0)
        return MigrationStatus(current_version=curr_ver, applied=applied, pending=pending)
    finally:
        if owned:
            conn.close()


def apply_migrations(
    conn_or_url: db.Connection | str | None = None,
    **kwargs: Any,
) -> list[MigrationRecord]:
    """Apply every migration this database still needs."""
    conn, owned = _resolve_connection(conn_or_url)
    applied_now: list[MigrationRecord] = []
    try:
        # Serialize against concurrent containers that start together and both
        # call apply_migrations; without this the check-then-insert can race.
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_ID,))
        conn.commit()
        try:
            ensure_schema_migrations_table(conn)
            applied = get_applied_migrations(conn)
            plan = _plan_for_connection(conn, applied)
            for migration in plan:
                logger.info("Applying migration %04d: %s...", migration.version, migration.name)
                with conn:
                    migration.apply(conn, **kwargs)
                    if migration is baseline:
                        # Stale rows from a wiped database would otherwise make
                        # the plan look partially applied forever.
                        conn.execute(f"DELETE FROM {SCHEMA_MIGRATIONS_TABLE}")
                    conn.execute(
                        f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE} (version, name, applied_at) "
                        "VALUES (%s, %s, CURRENT_TIMESTAMP)",
                        (migration.version, migration.name),
                    )

                rec_row = conn.execute(
                    f"SELECT version, name, applied_at FROM {SCHEMA_MIGRATIONS_TABLE} WHERE version = %s",
                    (migration.version,),
                ).fetchone()
                conn.commit()
                # The migration may have changed columns; drop cached INSERT column lists.
                db.clear_table_columns_cache()
                if rec_row:
                    record = MigrationRecord(
                        version=int(rec_row[0]),
                        name=str(rec_row[1]),
                        applied_at=rec_row[2],
                    )
                    applied_now.append(record)
                    if migration is baseline:
                        applied = [record]
                    else:
                        applied.append(record)
                logger.info("Applied migration %04d: %s successfully.", migration.version, migration.name)

            return applied_now
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_ID,))
            conn.commit()
    finally:
        if owned:
            conn.close()


def recreate_database(
    conn_or_url: db.Connection | str | None = None,
    *,
    preserve_user_state: bool = True,
    **kwargs: Any,
) -> list[MigrationRecord]:
    """Drop hub tables and run all versioned migrations from scratch.

    By default, user state tables (`USER_STATE_TABLES`) are preserved across
    recreation while all prior application tables are dropped and rebuilt.
    Pass ``preserve_user_state=False`` to drop user state tables as well.
    """
    conn, owned = _resolve_connection(conn_or_url)
    try:
        if preserve_user_state:
            tables_to_drop = [
                table for table in APPLICATION_TABLES if table not in PRESERVED_RECREATE_TABLES
            ]
        else:
            tables_to_drop = list(APPLICATION_TABLES)
        all_to_drop = (*tables_to_drop, SCHEMA_MIGRATIONS_TABLE)
        with conn:
            for table in all_to_drop:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

        logger.info("Recreating database: dropped %d tables.", len(all_to_drop))
        return apply_migrations(conn, **kwargs)
    finally:
        if owned:
            conn.close()
