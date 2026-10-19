"""Migration runner and schema recreation engine for Adult Hub."""

from __future__ import annotations

import logging
from typing import Any

from common import db
from common.migrations.base import Migration, MigrationRecord, MigrationStatus
from common.migrations.legacy import LEGACY_MIGRATIONS
from common.migrations.m0001_baseline import migration as baseline
from common.migrations.m0010_admin_operation_plans import migration as admin_operation_plans
from common.migrations.m0011_video_metadata_overrides import migration as video_metadata_overrides
from common.migrations.m0012_video_availability_checks import migration as video_availability_checks
from common.migrations.m0013_admin_audit_items import migration as admin_audit_items
from common.migrations.m0014_admin_schedules import migration as admin_schedules
from common.migrations.m0015_admin_audit_nfo_findings import migration as admin_audit_nfo_findings
from common.migrations.m0016_admin_audit_jellyfin_state import migration as admin_audit_jellyfin_state
from common.migrations.m0017_admin_audit_artwork import migration as admin_audit_artwork
from common.migrations.m0018_admin_action_events import migration as admin_action_events
from common.migrations.m0019_admin_change_events import migration as admin_change_events
from common.migrations.m0020_admin_audit_operations_schedules import migration as admin_audit_operations_schedules
from common.migrations.m0021_performer_catalog import migration as performer_catalog
from common.migrations.m0022_telegram_performer_rebuild_backup import migration as performer_rebuild_backup
from common.migrations.m0023_admin_quality_center import migration as admin_quality_center
from common.migrations.m0024_admin_orphan_quarantine import migration as admin_orphan_quarantine
from common.migrations.m0025_admin_quality_acknowledgements import migration as admin_quality_acknowledgements
from common.migrations.m0026_external_metadata_attempts import migration as external_metadata_attempts
from common.migrations.m0027_video_fingerprints import migration as video_fingerprints
from common.migrations.m0028_phash_provenance import migration as phash_provenance
from common.migrations.m0029_oshash_media_probe import migration as oshash_media_probe
from common.migrations.m0030_video_duplicate_decision_operation_index import migration as video_duplicate_decision_operation_index
from common.migrations.m0031_video_scene_match_provenance import migration as video_scene_match_provenance
from common.migrations.m0032_history_retention_indexes import migration as history_retention_indexes
from common.migrations.m0033_video_duplicate_rules_provenance import migration as video_duplicate_rules_provenance
from common.migrations.m0034_video_phash_thumbnails import migration as video_phash_thumbnails
from common.migrations.m0035_fuzzy_performer_reviews import migration as fuzzy_performer_reviews
from common.migrations.m0036_metadata_review_proposals import migration as metadata_review_proposals
from common.migrations.m0037_video_phash_jobs import migration as video_phash_jobs
from common.migrations.m0038_video_phash_job_processing_time import migration as video_phash_job_processing_time
from common.migrations.m0039_drop_fuzzy_performer_reviews import migration as drop_fuzzy_performer_reviews
from common.migrations.m0040_performer_profile_overrides import migration as performer_profile_overrides
from common.migrations.m0041_performer_external_links import migration as performer_external_links
from common.migrations.m0042_telegram_performer_resolution import migration as telegram_performer_resolution
from common.migrations.m0043_telegram_unavailable_quarantine import migration as telegram_unavailable_quarantine
from common.migrations.m0044_minimum_video_duration import migration as minimum_video_duration
from common.migrations.m0045_external_metadata_error_detail import migration as external_metadata_error_detail

logger = logging.getLogger(__name__)

SCHEMA_MIGRATIONS_TABLE = "schema_migrations"
MIGRATION_LOCK_ID = 0xAD01780B

# Fresh databases use one baseline followed by additive post-baseline changes.
# Legacy migrations remain available only when the migration table proves that
# a database already started that historical chain.
MIGRATIONS: tuple[Migration, ...] = (
    baseline,
    admin_operation_plans,
    video_metadata_overrides,
    video_availability_checks,
    admin_audit_items,
    admin_schedules,
    admin_audit_nfo_findings,
    admin_audit_jellyfin_state,
    admin_audit_artwork,
    admin_action_events,
    admin_change_events,
    admin_audit_operations_schedules,
    performer_catalog,
    performer_rebuild_backup,
    admin_quality_center,
    admin_orphan_quarantine,
    admin_quality_acknowledgements,
    external_metadata_attempts,
    video_fingerprints,
    phash_provenance,
    oshash_media_probe,
    video_duplicate_decision_operation_index,
    video_scene_match_provenance,
    history_retention_indexes,
    video_duplicate_rules_provenance,
    video_phash_thumbnails,
    fuzzy_performer_reviews,
    metadata_review_proposals,
    video_phash_jobs,
    video_phash_job_processing_time,
    drop_fuzzy_performer_reviews,
    performer_profile_overrides,
    performer_external_links,
    telegram_performer_resolution,
    telegram_unavailable_quarantine,
    minimum_video_duration,
    external_metadata_error_detail,
)


def _validate_active_migrations(migrations: tuple[Migration, ...]) -> None:
    """Reject duplicate or out-of-order versions in the active chain only."""
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError("Active migration versions must be unique")
    if versions != sorted(versions):
        raise ValueError("Active migrations must be ordered by version")


_validate_active_migrations(MIGRATIONS)
INTERIM_BASELINE_VERSION = 9

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


def _migration_plan(
    applied: list[MigrationRecord], target_version: int | None = None,
) -> list[Migration]:
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

    # Explicit requests for an old target are retained for migration fixtures
    # and recovery of databases that began before the baseline existed.
    if target_version is not None and target_version < INTERIM_BASELINE_VERSION:
        if baseline_is_applied or has_interim_baseline:
            return []
        return [
            migration for migration in LEGACY_MIGRATIONS
            if migration.version <= target_version and migration.version not in applied_versions
        ]

    plan: list[Migration] = []
    if has_legacy_history:
        plan.extend(
            migration for migration in LEGACY_MIGRATIONS
            if migration.version not in applied_versions
        )
    if not baseline_is_applied:
        plan.append(baseline)
    plan.extend(
        migration for migration in MIGRATIONS
        if migration is not baseline
        and migration.version not in applied_versions
    )
    if target_version is not None:
        plan = [m for m in plan if m.version <= target_version]
    return plan


def get_migration_status(conn_or_url: db.Connection | str | None = None) -> MigrationStatus:
    """Inspect current schema migration status."""
    conn, owned = _resolve_connection(conn_or_url)
    try:
        applied = get_applied_migrations(conn)
        pending = _migration_plan(applied)
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
        ensure_schema_migrations_table(conn)
        applied = get_applied_migrations(conn)
        for migration in _migration_plan(applied, target_version):

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
