"""Migration runner and schema recreation engine for Adult Hub."""

from __future__ import annotations

import logging
from typing import Any

from common import db
from common.migrations.base import Migration, MigrationRecord, MigrationStatus
from common.migrations.legacy import (
    LEGACY_MIGRATIONS,
    POST_BASELINE_MIGRATIONS,
    admin_operation_plans,
    video_metadata_overrides,
)
from common.migrations.m0001_baseline import migration as baseline

logger = logging.getLogger(__name__)

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"
MIGRATION_LOCK_ID = 0xAD01780B

# A fresh database is built by this one tracked migration. The 38 changes that
# used to follow the baseline were squashed into it in release 0.4.0 and now live
# in POST_BASELINE_MIGRATIONS, which only databases with recorded history from
# that chain still walk.
MIGRATIONS: tuple[Migration, ...] = (baseline,)


def _validate_active_migrations(migrations: tuple[Migration, ...]) -> None:
    """Reject duplicate or out-of-order versions in a chain."""
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError("Active migration versions must be unique")
    if versions != sorted(versions):
        raise ValueError("Active migrations must be ordered by version")


_validate_active_migrations(MIGRATIONS)
# Ordering carries meaning here (m0039 drops the table m0035 creates).
_validate_active_migrations(POST_BASELINE_MIGRATIONS)
INTERIM_BASELINE_VERSION = 9

# Highest version of the pre-squash chain; also the ceiling for partial upgrades.
POST_BASELINE_CEILING = max(migration.version for migration in POST_BASELINE_MIGRATIONS)

# Objects introduced by the last two migrations of the squashed chain. Their
# presence proves a schema is at least as new as the baseline it was compared
# against; their absence means the database never finished that chain.
SQUASH_MARKERS: tuple[tuple[str, str], ...] = (
    (
        "video_phash_jobs",
        "SELECT to_regclass('video_phash_jobs') IS NOT NULL",
    ),
    (
        "videos.file_size_bytes",
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'videos' "
        "AND column_name = 'file_size_bytes')",
    ),
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
    "videos",
    "dead_streams",
    "meta",
    "video_duplicate_jobs",
    "video_duplicate_decisions",
    "video_duplicate_dismissals",
    "telegram_channel_state",
    "performer_aliases",
    "performer_bios",
    "performer_photos",
    "performer_catalog",
    "performer_catalog_aliases",
    "performer_profile_overrides",
    "telegram_performer_proposals",
    "telegram_performer_rebuild_backup",
    "jellyfin_person_sync",
    "stream_cache",
    "diagnostic_settings",
    "diagnostic_reports",
    "admin_operation_plans",
    "video_metadata_overrides",
    "video_availability_checks",
    "telegram_unavailable_quarantine",
    "admin_audit_items",
    "admin_schedules",
    "admin_action_events",
    "admin_audit_events",
    "admin_orphan_quarantine",
    "admin_quality_acknowledgements",
    "external_metadata_attempts",
    "metadata_review_proposals",
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
    """True once the baseline era left any application schema behind."""
    return conn.execute("SELECT to_regclass('videos')").fetchone()[0] is not None


def _migration_plan(
    applied: list[MigrationRecord],
    target_version: int | None = None,
    *,
    pre_squash_schema: bool = False,
) -> list[Migration]:
    """Migrations this database still needs.

    `pre_squash_schema` comes from the caller's introspection and only matters
    when the recorded history cannot say which schema generation a database
    holds on its own.
    """
    applied_versions = {record.version for record in applied}
    names_by_version = {record.version: record.name for record in applied}
    baseline_is_applied = (
        names_by_version.get(baseline.version) == baseline.name
        and INTERIM_BASELINE_VERSION not in applied_versions
    )
    has_interim_baseline = (
        INTERIM_BASELINE_VERSION in applied_versions
        and names_by_version[INTERIM_BASELINE_VERSION] == "0009_baseline"
    )
    has_legacy_history = any(
        version < INTERIM_BASELINE_VERSION
        and names_by_version[version] not in {
            baseline.name, admin_operation_plans.name, video_metadata_overrides.name,
            "0002_admin_operation_plans"
        }
        for version in applied_versions
    )
    # Any recorded version above the interim baseline proves this database
    # walked the pre-squash chain, so the rest of that chain still has to run.
    # Databases created by the current baseline record version 1 alone and never
    # see POST_BASELINE_MIGRATIONS.
    follows_pre_squash_chain = (
        has_legacy_history
        or has_interim_baseline
        or pre_squash_schema
        or any(version > INTERIM_BASELINE_VERSION for version in applied_versions)
    )

    # Explicit requests for an old target are retained for migration fixtures
    # and recovery of databases that began before the baseline existed.
    if target_version is not None and target_version < INTERIM_BASELINE_VERSION:
        if baseline_is_applied or has_interim_baseline:
            return []
        return [
            migration for migration in LEGACY_MIGRATIONS
            if migration.version <= target_version and migration.version not in applied_versions
        ]

    # The order of these three blocks is the execution order: the pre-baseline
    # chain, then the baseline stamp, then the squashed chain. Sorting by version
    # would place the baseline next to the legacy migration that also claims
    # version 1 and collide on the schema_migrations primary key.
    plan: list[Migration] = []
    if has_legacy_history:
        plan.extend(
            migration for migration in LEGACY_MIGRATIONS
            if migration.version not in applied_versions
        )
    if not baseline_is_applied:
        plan.append(baseline)
    if follows_pre_squash_chain:
        plan.extend(
            migration for migration in POST_BASELINE_MIGRATIONS
            if migration.version not in applied_versions
        )
    if target_version is not None:
        plan = [m for m in plan if m.version <= target_version]
    return plan


def _schema_predates_squash(conn: db.Connection) -> bool:
    """True when objects introduced late in the squashed chain are absent."""
    return any(
        not conn.execute(probe).fetchone()[0]
        for _, probe in SQUASH_MARKERS
    )


def _has_application_data(conn: db.Connection) -> bool:
    return bool(conn.execute("SELECT EXISTS (SELECT 1 FROM videos)").fetchone()[0])


def _untrusted_history(applied: list[MigrationRecord]) -> RuntimeError:
    recorded = ", ".join(str(record.version) for record in applied) or "nothing"
    return RuntimeError(
        f"Schema history cannot be trusted: the database holds tables but records {recorded}, "
        "and its schema predates the 0.4.0 squash. Refusing to replay the squashed chain over "
        "existing data or to stamp it as current. Insert the rows for the versions this schema "
        "actually has into schema_migrations, or restore a dump that includes that table."
    )


def _plan_for_connection(
    conn: db.Connection,
    applied: list[MigrationRecord],
    target_version: int | None = None,
) -> list[Migration]:
    """Resolve pending migrations, trusting the schema over lost bookkeeping.

    A database recording only the baseline is normally the flat shape created by
    the current baseline. If it is missing objects from the end of the squashed
    chain instead, it is an interrupted pre-squash upgrade: harmless to finish
    while the tables are still empty, dangerous once they hold data, because the
    squashed chain is not idempotent and two of its steps rewrite rows.
    """
    pre_squash_schema = False
    if _has_tables(conn):
        versions = {record.version for record in applied}
        if versions in (set(), {baseline.version}):
            pre_squash_schema = _schema_predates_squash(conn)
            if pre_squash_schema and (not applied or _has_application_data(conn)):
                raise _untrusted_history(applied)
    return _migration_plan(applied, target_version, pre_squash_schema=pre_squash_schema)


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
    target_version: int | None = None,
    **kwargs: Any,
) -> list[MigrationRecord]:
    """Apply any unapplied migrations up to target_version (or latest if None)."""
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
            plan = _plan_for_connection(conn, applied, target_version)
            if (
                target_version is not None
                and target_version < POST_BASELINE_CEILING
                and not _has_tables(conn)
                and any(migration is baseline for migration in plan)
            ):
                raise ValueError(
                    f"target_version {target_version} cannot be honoured: the baseline creates "
                    "the whole schema in one step, so a fresh database cannot stop partway"
                )
            for migration in plan:

                logger.info("Applying migration %04d: %s...", migration.version, migration.name)
                with conn:
                    migration.apply(conn, **kwargs)
                    if migration is baseline:
                        # Squash legacy/interim history only after schema
                        # validation succeeds. This changes metadata, not data.
                        conn.execute(f"DELETE FROM {SCHEMA_MIGRATIONS_TABLE}")
                    conn.execute(
                        f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE} (version, name, applied_at) "
                        "VALUES (%s, %s, CURRENT_TIMESTAMP)",
                        (migration.version, migration.name),
                    )

                # Fetch the recorded record
                rec_row = conn.execute(
                    f"SELECT version, name, applied_at FROM {SCHEMA_MIGRATIONS_TABLE} WHERE version = %s",
                    (migration.version,),
                ).fetchone()
                conn.commit()
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
    **kwargs: Any,
) -> list[MigrationRecord]:
    """Drop all hub tables and run all versioned migrations from scratch."""
    conn, owned = _resolve_connection(conn_or_url)
    try:
        all_to_drop = (*APPLICATION_TABLES, SCHEMA_MIGRATIONS_TABLE)
        with conn:
            for table in all_to_drop:
                conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

        logger.info("Recreating database: dropped %d tables.", len(all_to_drop))
        return apply_migrations(conn, **kwargs)
    finally:
        if owned:
            conn.close()
