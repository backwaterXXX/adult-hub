"""Tests for versioned migrations and schema recreation engine."""

from __future__ import annotations

import sys
import json
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import db
from common.migrations import (
    APPLICATION_TABLES,
    MIGRATIONS,
    SCHEMA_MIGRATIONS_TABLE,
    apply_migrations,
    get_applied_migrations,
    get_migration_status,
    recreate_database,
)
from common.migrations.base import Migration
from common.migrations.legacy import LEGACY_MIGRATIONS, POST_BASELINE_MIGRATIONS
from common.migrations.m0001_baseline import SCHEMA_BASELINE_SQL, migration as baseline
from common.migrations.runner import (
    POST_BASELINE_CEILING,
    _validate_active_migrations,
    ensure_schema_migrations_table,
)
from tests.pg_helper import PgTestCase


def _record(conn: db.Connection, version: int, name: str) -> None:
    conn.execute(
        f"INSERT INTO {SCHEMA_MIGRATIONS_TABLE} (version, name, applied_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP)",
        (version, name),
    )


def _drop_everything(conn: db.Connection) -> None:
    # Drop every table the schema actually holds, not just the managed list:
    # other test modules create ad-hoc tables in this database, and comparing
    # the baseline against the chain is only meaningful in a clean room.
    with conn:
        for table in sorted(db.table_names(conn)):
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def build_pre_squash_schema(conn: db.Connection, *, through_version: int) -> None:
    """Rebuild the schema a database had at `through_version` of the old chain.

    The current baseline creates the whole schema in one step, so an interim
    shape can only be produced by replaying the migrations that database would
    have walked, recording history the same way the runner does.
    """
    _drop_everything(conn)
    ensure_schema_migrations_table(conn)
    with conn:
        for migration in LEGACY_MIGRATIONS:
            migration.apply(conn)
            _record(conn, migration.version, migration.name)
        conn.execute(f"DELETE FROM {SCHEMA_MIGRATIONS_TABLE}")
        _record(conn, baseline.version, baseline.name)
        for migration in POST_BASELINE_MIGRATIONS:
            if migration.version > through_version:
                break
            migration.apply(conn)
            _record(conn, migration.version, migration.name)


def build_baseline_schema(conn: db.Connection) -> None:
    """Create the schema from the squashed script without touching history."""
    _drop_everything(conn)
    with conn:
        conn.execute(SCHEMA_BASELINE_SQL.read_text(encoding="utf-8"))


SNAPSHOT_SQL = """
SELECT 'column', table_name || '.' || column_name,
       data_type || '|' || is_nullable || '|' || coalesce(column_default, '')
FROM information_schema.columns
WHERE table_schema = current_schema() AND table_name <> 'schema_migrations'
UNION ALL
SELECT 'index', indexname, indexdef FROM pg_indexes
WHERE schemaname = current_schema() AND tablename <> 'schema_migrations'
UNION ALL
SELECT 'constraint', conname, pg_get_constraintdef(oid) FROM pg_constraint
WHERE connamespace = current_schema()::regnamespace
  AND conrelid::regclass::text <> 'schema_migrations'
UNION ALL
SELECT 'function', proname, pg_get_functiondef(oid) FROM pg_proc
WHERE pronamespace = current_schema()::regnamespace
UNION ALL
SELECT 'trigger', tgname, pg_get_triggerdef(oid) FROM pg_trigger WHERE NOT tgisinternal
"""


def schema_snapshot(conn: db.Connection) -> set[tuple[str, str, str]]:
    return {tuple(row) for row in conn.execute(SNAPSHOT_SQL).fetchall()}


class TestActiveMigrationValidation(unittest.TestCase):
    @staticmethod
    def _migration(version: int) -> Migration:
        return Migration(version, f"migration_{version}", "test fixture", lambda conn: None)

    def test_active_migration_versions_must_be_unique(self):
        with self.assertRaisesRegex(ValueError, "must be unique"):
            _validate_active_migrations((self._migration(1), self._migration(1)))

    def test_active_migrations_must_be_ordered(self):
        with self.assertRaisesRegex(ValueError, "ordered by version"):
            _validate_active_migrations((self._migration(2), self._migration(1)))


class TestMigrationsEngine(PgTestCase):
    # Snapshot of the squashed schema, taken once per class by setUp.
    baseline_reference: set[tuple[str, str, str]] | None = None

    def setUp(self):
        super().setUp()
        if type(self).baseline_reference is None:
            build_baseline_schema(self.conn)
            type(self).baseline_reference = schema_snapshot(self.conn)
        self.baseline_reference = type(self).baseline_reference

    def test_migration_status_and_full_application(self):
        # 1. Recreate clean test database: the baseline is the whole chain now.
        applied = recreate_database(self.conn)
        self.assertEqual([m.version for m in applied], [baseline.version])
        self.assertEqual(MIGRATIONS, (baseline,))

        # 2. Status should report up to date
        status = get_migration_status(self.conn)
        self.assertTrue(status.is_up_to_date)
        self.assertEqual(status.current_version, baseline.version)
        self.assertEqual(len(status.pending), 0)
        self.assertEqual(len(status.applied), 1)

        # 3. Check schema_migrations table contents
        applied_records = get_applied_migrations(self.conn)
        self.assertEqual([r.version for r in applied_records], [baseline.version])
        self.assertEqual(applied_records[0].name, "0001_baseline")

        # 4. Check that all expected application tables exist in PostgreSQL
        existing_tables = db.table_names(self.conn)
        for tbl in APPLICATION_TABLES:
            self.assertIn(tbl, existing_tables, f"Table {tbl} must exist")
        self.assertIn(SCHEMA_MIGRATIONS_TABLE, existing_tables)
        for table in (
            "video_performers", "video_performer_migration_issues",
            "video_metadata_migration_issues", "performer_bio_migration_issues",
        ):
            self.assertIn(table, existing_tables)

    def test_apply_migrations_is_idempotent(self):
        recreate_database(self.conn)
        # Calling apply_migrations again should be a no-op
        second_run = apply_migrations(self.conn)
        self.assertEqual(second_run, [])

        status = get_migration_status(self.conn)
        self.assertTrue(status.is_up_to_date)
        self.assertEqual(status.current_version, MIGRATIONS[-1].version)

    def test_concurrent_apply_migrations_is_serialized(self):
        # Containers starting together may both call apply_migrations on an
        # empty database; the advisory lock must keep the second writer from
        # hitting a duplicate-key insert in schema_migrations.
        with self.conn:
            for tbl in (*APPLICATION_TABLES, SCHEMA_MIGRATIONS_TABLE):
                self.conn.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")

        results: list[list] = []
        errors: list[BaseException] = []

        def worker():
            try:
                results.append(apply_migrations(self.db_url))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)

        self.assertEqual(errors, [])
        counts = sorted(len(r) for r in results)
        self.assertEqual(counts, [0, len(MIGRATIONS)])
        rows = self.conn.execute(
            f"SELECT version, COUNT(*) FROM {SCHEMA_MIGRATIONS_TABLE} GROUP BY version HAVING COUNT(*) > 1"
        ).fetchall()
        self.assertEqual(rows, [])

    def test_target_version_migration(self):
        # Drop all tables first
        with self.conn:
            for tbl in (*APPLICATION_TABLES, SCHEMA_MIGRATIONS_TABLE):
                self.conn.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")

        # Explicit old targets remain available for historical upgrade paths.
        applied_part1 = apply_migrations(self.conn, target_version=2)
        self.assertEqual([m.version for m in applied_part1], [1, 2])

        status_part1 = get_migration_status(self.conn)
        self.assertFalse(status_part1.is_up_to_date)
        self.assertEqual(status_part1.current_version, 2)
        self.assertEqual([m.version for m in status_part1.pending], list(range(3, 9)) + [1] + list(range(10, POST_BASELINE_CEILING + 1)))

        # Complete remaining migrations
        applied_part2 = apply_migrations(self.conn)
        self.assertEqual([m.version for m in applied_part2], list(range(3, 9)) + [1] + list(range(10, POST_BASELINE_CEILING + 1)))

        status_final = get_migration_status(self.conn)
        self.assertTrue(status_final.is_up_to_date)
        self.assertEqual(status_final.current_version, POST_BASELINE_CEILING)

    def test_partial_target_is_refused_when_baseline_builds_the_schema(self):
        # The squashed baseline has no intermediate shape to stop at, so a
        # partial target on a fresh database would record version 1 while the
        # schema is already current. Saying no beats pretending otherwise.
        _drop_everything(self.conn)
        self.addCleanup(recreate_database, self.conn)
        with self.assertRaisesRegex(ValueError, "cannot be honoured"):
            apply_migrations(self.conn, target_version=40)
        self.assertEqual(get_applied_migrations(self.conn), [])

    def test_recreate_database_clears_old_data_and_reapplies(self):
        recreate_database(self.conn)
        with self.conn:
            self.conn.execute("INSERT INTO videos (id, title, duration) VALUES ('v-test-1', 'Test Video', 600)")

        row = self.conn.execute("SELECT id FROM videos WHERE id='v-test-1'").fetchone()
        self.assertEqual(row[0], "v-test-1")
        self.conn.commit()

        # Recreate should wipe data and rebuild clean schema
        applied = recreate_database(self.conn)
        self.assertEqual(len(applied), len(MIGRATIONS))

        row_after = self.conn.execute("SELECT id FROM videos WHERE id='v-test-1'").fetchone()
        self.assertIsNone(row_after)

    def test_baseline_script_matches_the_squashed_migration_chain(self):
        """The two ways of reaching the current schema must agree exactly."""
        build_pre_squash_schema(self.conn, through_version=POST_BASELINE_CEILING)
        chain_schema = schema_snapshot(self.conn)
        build_baseline_schema(self.conn)
        baseline_schema = schema_snapshot(self.conn)

        only_chain = {f"{kind}:{name}" for kind, name, _ in chain_schema - baseline_schema}
        only_baseline = {f"{kind}:{name}" for kind, name, _ in baseline_schema - chain_schema}
        self.assertEqual(only_chain, set())
        self.assertEqual(only_baseline, set())

    def test_baseline_script_covers_every_application_table(self):
        build_baseline_schema(self.conn)
        created = set(db.table_names(self.conn)) - {SCHEMA_MIGRATIONS_TABLE}
        self.assertEqual(created, set(APPLICATION_TABLES))

    def test_recorded_old_chain_is_up_to_date_without_replaying_ddl(self):
        build_pre_squash_schema(self.conn, through_version=POST_BASELINE_CEILING)
        with self.conn:
            self.conn.execute("INSERT INTO videos (id, title, duration) VALUES ('v-old', 'Old', 600)")
        status = get_migration_status(self.conn)
        self.assertTrue(status.is_up_to_date)
        self.assertEqual(status.pending, [])
        self.assertEqual(apply_migrations(self.conn), [])
        self.assertEqual(
            self.conn.execute("SELECT title FROM videos WHERE id = 'v-old'").fetchone()[0], "Old"
        )

    def test_lagging_old_chain_upgrades_only_the_missing_versions(self):
        build_pre_squash_schema(self.conn, through_version=30)
        status = get_migration_status(self.conn)
        self.assertEqual(
            [migration.version for migration in status.pending],
            [migration.version for migration in POST_BASELINE_MIGRATIONS if migration.version > 30],
        )
        applied = apply_migrations(self.conn)
        self.assertEqual([migration.version for migration in applied], list(range(31, POST_BASELINE_CEILING + 1)))
        self.assertEqual(schema_snapshot(self.conn), self.baseline_reference)

    def test_existing_schema_without_history_is_refused(self):
        # Losing schema_migrations must not let the runner declare an unknown
        # schema current, because the squashed chain is not safe to replay.
        build_pre_squash_schema(self.conn, through_version=30)
        self.addCleanup(recreate_database, self.conn)
        with self.conn:
            self.conn.execute("INSERT INTO videos (id, title, duration) VALUES ('v-lost', 'Lost', 600)")
            self.conn.execute(f"DELETE FROM {SCHEMA_MIGRATIONS_TABLE}")
        with self.assertRaisesRegex(RuntimeError, "history cannot be trusted"):
            get_migration_status(self.conn)
        with self.assertRaisesRegex(RuntimeError, "history cannot be trusted"):
            apply_migrations(self.conn)

    def test_interrupted_pre_squash_install_resumes_the_chain(self):
        # An install that recorded the baseline and died before the chain ended
        # holds no data, so finishing it is safe rather than something to refuse.
        build_pre_squash_schema(self.conn, through_version=0)
        with self.conn:
            self.conn.execute(f"DELETE FROM {SCHEMA_MIGRATIONS_TABLE}")
            _record(self.conn, baseline.version, baseline.name)
        applied = apply_migrations(self.conn)
        self.assertEqual([migration.version for migration in applied], list(range(10, POST_BASELINE_CEILING + 1)))
        self.assertEqual(schema_snapshot(self.conn), self.baseline_reference)

    def test_current_schema_without_history_is_stamped_not_replayed(self):
        build_baseline_schema(self.conn)
        applied = apply_migrations(self.conn)
        self.assertEqual([migration.version for migration in applied], [baseline.version])
        self.assertTrue(get_migration_status(self.conn).is_up_to_date)

    def test_existing_v8_database_is_squashed_without_rewriting_data(self):
        recreate_database(self.conn, target_version=8)
        self.conn.execute(
            "INSERT INTO videos (id, title, performers, first_seen) VALUES (%s, %s, %s, %s)",
            ("existing:v8", "Keep me", "[]", 1_700_000_000),
        )
        self.conn.commit()

        applied = apply_migrations(self.conn)

        self.assertEqual([record.version for record in applied], [1] + list(range(10, POST_BASELINE_CEILING + 1)))
        row = self.conn.execute(
            "SELECT title, EXTRACT(EPOCH FROM first_seen) FROM videos WHERE id = %s",
            ("existing:v8",),
        ).fetchone()
        self.assertEqual(row[0], "Keep me")
        self.assertEqual(float(row[1]), 1_700_000_000)
        self.assertTrue(get_migration_status(self.conn).is_up_to_date)
        records = get_applied_migrations(self.conn)
        self.assertEqual([(record.version, record.name) for record in records], [
            (1, "0001_baseline"), (10, "0010_admin_operation_plans"), (11, "0011_video_metadata_overrides"),
            (12, "0012_video_availability_checks"), (13, "0013_admin_audit_items"),
            (14, "0014_admin_schedules"), (15, "0015_admin_audit_nfo_findings"),
            (16, "0016_admin_audit_jellyfin_state"), (17, "0017_admin_audit_artwork"),
            (18, "0018_admin_action_events"), (19, "0019_admin_change_events"),
            (20, "0020_admin_audit_operations_schedules"), (21, "0021_performer_catalog"),
            (22, "0022_telegram_performer_rebuild_backup"), (23, "0023_admin_quality_center"),
            (24, "0024_admin_orphan_quarantine"), (25, "0025_admin_quality_acknowledgements"),
            (26, "0026_external_metadata_attempts"), (27, "0027_video_fingerprints"),
            (28, "0028_phash_provenance"), (29, "0029_oshash_media_probe"),
            (30, "0030_video_duplicate_decision_operation_index"),
            (31, "0031_video_scene_match_provenance"),
            (32, "0032_history_retention_indexes"),
            (33, "0033_video_duplicate_rules_provenance"),
            (34, "0034_video_phash_thumbnails"),
            (35, "0035_fuzzy_performer_reviews"),
            (36, "0036_metadata_review_proposals"), (37, "0037_video_phash_jobs"),
            (38, "0038_video_phash_job_processing_time"),
            (39, "0039_drop_fuzzy_performer_reviews"),
            (40, "0040_performer_profile_overrides"),
            (41, "0041_performer_external_links"),
            (42, "0042_telegram_performer_resolution"),
            (43, "0043_telegram_unavailable_quarantine"),
            (44, "0044_minimum_video_duration"),
            (45, "0045_external_metadata_error_detail"),
            (46, "0046_drop_stream_probe_columns"),
            (47, "0047_video_file_size"),
            (48, "0048_video_phash_job_error_detail"),
            (49, "0049_catalog_automation"),
        ])

    def test_interim_v9_baseline_is_renumbered_to_v1(self):
        # A database from the interim-baseline era: version 9 recorded, then the
        # whole chain walked on top of it.
        build_pre_squash_schema(self.conn, through_version=POST_BASELINE_CEILING)
        self.conn.execute(
            "INSERT INTO videos (id, title, duration) VALUES (%s, %s, 600)",
            ("existing:v9", "Keep interim data"),
        )
        self.conn.execute(
            "UPDATE schema_migrations SET version = 9, name = '0009_baseline' WHERE version = 1"
        )
        self.conn.commit()

        applied = apply_migrations(self.conn)

        # The interim baseline only needs renumbering. Later migrations are
        # already recorded and must not be replayed over the current schema.
        self.assertEqual([record.version for record in applied], [1])
        row = self.conn.execute(
            "SELECT title FROM videos WHERE id = %s", ("existing:v9",)
        ).fetchone()
        self.assertEqual(row[0], "Keep interim data")
        records = get_applied_migrations(self.conn)
        self.assertEqual([(record.version, record.name) for record in records], [
            (1, "0001_baseline"),
        ])

    def test_admin_audit_extension_preserves_existing_rows_and_accepts_new_types(self):
        from psycopg.types.json import Jsonb

        build_pre_squash_schema(self.conn, through_version=19)
        self.conn.execute(
            "ALTER TABLE admin_audit_events RENAME CONSTRAINT admin_audit_events_event_type_check TO legacy_event_guard"
        )
        self.conn.execute(
            "ALTER TABLE admin_audit_events RENAME CONSTRAINT admin_audit_events_object_type_check TO legacy_object_guard"
        )
        self.conn.execute(
            "INSERT INTO admin_audit_events (event_key,event_type,object_type,object_id,changes) "
            "VALUES ('existing-source-event','source_settings_update','source_configuration','managed_sources',%s)",
            (Jsonb({"telegram.channels.-1001.studio": {"before": "Old", "after": "New"}}),),
        )
        self.conn.commit()
        apply_migrations(self.conn)

        existing = self.conn.execute(
            "SELECT event_type,changes FROM admin_audit_events WHERE event_key='existing-source-event'"
        ).fetchone()
        self.assertEqual(existing[0], "source_settings_update")
        existing_changes = existing[1] if isinstance(existing[1], dict) else json.loads(existing[1])
        self.assertEqual(existing_changes["telegram.channels.-1001.studio"]["after"], "New")
        with self.conn:
            self.conn.execute(
                "INSERT INTO admin_audit_events (event_key,event_type,object_type,object_id,changes) "
                "VALUES ('operation-event','admin_operation_queued','operation_plan','plan-1',%s)",
                (Jsonb({"state": {"before": "previewed", "after": "queued"}}),),
            )
            self.conn.execute(
                "INSERT INTO admin_audit_events (event_key,event_type,object_type,object_id,changes) "
                "VALUES ('schedule-event','schedule_created','schedule','schedule-1',%s)",
                (Jsonb({"enabled": {"before": None, "after": True}}),),
            )
        from psycopg.errors import CheckViolation
        with self.assertRaises(CheckViolation):
            self.conn.execute(
                "INSERT INTO admin_audit_events (event_key,event_type,object_type,object_id,changes) "
                "VALUES ('invalid-event','unsupported_type','schedule','schedule-2',%s)",
                (Jsonb({"enabled": {"before": None, "after": True}}),),
            )
        self.conn.rollback()

    def test_new_migrations_preserve_auditable_metadata_and_convert_times(self):
        recreate_database(self.conn, target_version=6)
        self.addCleanup(apply_migrations, self.conn)
        # Seed historical v6 data without the v8 epoch compatibility adapter.
        self.conn._raw.execute(
            "INSERT INTO videos (id, provider, title, performers, categories, tags, first_seen, last_seen, performers_audited_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ("test:ordered", "test", "Ordered", '["A", "a", "B"]',
             '["Drama", "Anal"]', '["tag"]', 1_700_000_000, 1_700_000_001,
             1_700_000_000_000_000_000),
        )
        self.conn._raw.execute(
            "INSERT INTO videos (id, provider, title, performers) VALUES (%s, %s, %s, %s)",
            ("test:malformed", "test", "Malformed", "legacy malformed performers"),
        )
        self.conn._raw.execute(
            "INSERT INTO performer_bios (name, aliases) VALUES (%s, %s)",
            ("A", "legacy malformed aliases"),
        )
        self.conn.commit()

        apply_migrations(self.conn)

        columns = {
            (row[0], row[1]): row[2]
            for row in self.conn.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name IN ('videos', 'performer_bios')"
            ).fetchall()
        }
        self.assertEqual(columns[("videos", "performers")], "jsonb")
        self.assertEqual(columns[("performer_bios", "aliases")], "jsonb")
        self.assertEqual(columns[("videos", "first_seen")], "timestamp with time zone")

        credits = self.conn.execute(
            "SELECT performer_name, position FROM video_performers "
            "WHERE video_id = %s ORDER BY position", ("test:ordered",)
        ).fetchall()
        self.assertEqual(credits, [("A", 0), ("a", 1), ("B", 2)])
        duplicate = self.conn.execute(
            "SELECT occurrences, source_names, source_positions "
            "FROM video_performer_migration_issues WHERE video_id = %s AND performer_key = %s",
            ("test:ordered", "a"),
        ).fetchone()
        self.assertEqual(duplicate[0], 2)
        self.assertEqual(json.loads(duplicate[1]), ["A", "a"])
        self.assertEqual(duplicate[2], [0, 1])
        self.assertEqual(
            self.conn.execute(
                "SELECT original_value FROM video_metadata_migration_issues "
                "WHERE video_id = %s AND field_name = 'performers'", ("test:malformed",)
            ).fetchone()[0],
            "legacy malformed performers",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT original_aliases FROM performer_bio_migration_issues WHERE name = %s", ("A",)
            ).fetchone()[0],
            "legacy malformed aliases",
        )
        epoch = self.conn.execute(
            "SELECT EXTRACT(EPOCH FROM first_seen) FROM videos WHERE id = %s", ("test:ordered",)
        ).fetchone()[0]
        self.assertEqual(float(epoch), 1_700_000_000)
        audited_epoch = self.conn.execute(
            "SELECT EXTRACT(EPOCH FROM performers_audited_at) FROM videos WHERE id = %s",
            ("test:ordered",),
        ).fetchone()[0]
        self.assertEqual(float(audited_epoch), 1_700_000_000)

    def test_m0041_performer_external_links_columns_and_backfill(self):
        build_pre_squash_schema(self.conn, through_version=40)
        self.conn.execute("INSERT INTO performer_bios (name, bio) VALUES ('Test Star', 'A bio')")
        self.conn.execute(
            "INSERT INTO performer_catalog (id, source, source_id, name) VALUES ('stashdb:uuid-123', 'stashdb', 'uuid-123', 'Test Star')"
        )
        self.conn.commit()

        apply_migrations(self.conn)

        row = self.conn.execute("SELECT stashdb_url, pbc_url FROM performer_bios WHERE name = 'Test Star'").fetchone()
        self.assertEqual(row[0], "https://stashdb.org/performers/uuid-123")
        self.assertEqual(row[1], "")


if __name__ == "__main__":
    unittest.main()
