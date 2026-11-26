"""Tests for the squashed migration baseline and schema recreation engine."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

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
from common.migrations.m0002_catalog_sort_indexes import migration as catalog_sort_indexes
from common.migrations.m0001_baseline import SCHEMA_BASELINE_SQL, migration as baseline
from common.migrations.runner import (
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
    # other test modules create ad-hoc tables in this database.
    with conn:
        for table in sorted(db.table_names(conn)):
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


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

    def test_chain_is_the_baseline_plus_appended_migrations(self):
        self.assertEqual([migration.version for migration in MIGRATIONS], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(MIGRATIONS[0].name, "0001_baseline")
        self.assertIs(MIGRATIONS[0], baseline)
        self.assertEqual(MIGRATIONS[1].name, "0002_catalog_sort_indexes")


class TestBitrateGeneratedColumn(PgTestCase):
    """The bitrate column is generated, so row snapshots must drop it."""

    def test_bitrate_is_generated_and_writable_snapshots_drop_it(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, duration, first_seen, playable, file_size_bytes) "
                "VALUES ('generated-1', 'telegram', 'Generated', 1800, '2026-10-01 12:00:00+00', 1, 2700000000)"
            )
        row = self.conn.execute("SELECT * FROM videos WHERE id = 'generated-1'").fetchone()
        columns = db.table_columns(self.conn, "videos")
        stored = dict(zip(columns, row))
        # 2 700 000 000 bytes over 1 800 seconds is 12 000 kbit/s.
        self.assertEqual(stored["bitrate_kbps"], 12000)
        self.assertEqual(
            db.generated_table_columns(self.conn, "videos"),
            {"bitrate_kbps", "quality_tier"},
        )

        writable = db.writable_row_dict(self.conn, "videos", row, columns)
        self.assertNotIn("bitrate_kbps", writable)
        names = list(writable)
        with self.conn:
            # Duplicate undo replays a whole snapshot row as one INSERT; the
            # generated column cannot be part of it.
            self.conn.execute(
                f"INSERT INTO videos ({','.join(names)}) VALUES ({','.join(['%s'] * len(names))}) "
                "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title",
                [writable[name] for name in names],
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT title, bitrate_kbps, quality_tier FROM videos WHERE id = 'generated-1'"
            ).fetchone(),
            ("Generated", 12000, "high"),
        )

        # A low bitrate file becomes high as soon as Telegram reports 2160p: the
        # tier sums the two signals with OR, so nothing that was HQ drops back.
        with self.conn:
            self.conn.execute(
                "UPDATE videos SET file_size_bytes = 300000000, height = 2160 WHERE id = 'generated-1'"
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT bitrate_kbps, quality_tier FROM videos WHERE id = 'generated-1'"
            ).fetchone(),
            (1333, "high"),
        )
        with self.conn:
            self.conn.execute("UPDATE videos SET height = 720 WHERE id = 'generated-1'")
        # 720p is HD: the 700 pixel cut keeps it out of the SD tier.
        self.assertEqual(
            self.conn.execute("SELECT quality_tier FROM videos WHERE id = 'generated-1'").fetchone()[0],
            "medium",
        )
        with self.conn:
            self.conn.execute("UPDATE videos SET height = 480 WHERE id = 'generated-1'")
        self.assertEqual(
            self.conn.execute("SELECT quality_tier FROM videos WHERE id = 'generated-1'").fetchone()[0],
            "low",
        )


class TestMigrationsEngine(PgTestCase):
    def test_fresh_database_applies_the_chain_and_is_then_up_to_date(self):
        applied = recreate_database(self.conn)
        self.assertEqual([m.version for m in applied], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(applied[0].name, "0001_baseline")

        status = get_migration_status(self.conn)
        self.assertTrue(status.is_up_to_date)
        self.assertEqual(status.current_version, 10)
        self.assertEqual(status.pending, [])

        records = get_applied_migrations(self.conn)
        self.assertEqual([(r.version, r.name) for r in records],
                         [(1, "0001_baseline"), (2, "0002_catalog_sort_indexes"),
                      (3, "0003_performer_stats"), (4, "0004_admin_audit_open_findings"),
                      (5, "0005_video_channel_name"),
                      (6, "0006_video_bitrate_quality"),
                      (7, "0007_video_resolution_quality_tier"),
                      (8, "0008_quality_tier_hd_cut"),
                      (9, "0009_video_channel_name_index"),
                      (10, "0010_catalog_search_trigram_favorites_index")])

        existing_tables = db.table_names(self.conn)
        for table in APPLICATION_TABLES:
            self.assertIn(table, existing_tables, f"Table {table} must exist")
        self.assertIn(SCHEMA_MIGRATIONS_TABLE, existing_tables)

    def test_baseline_script_covers_every_application_table(self):
        _drop_everything(self.conn)
        self.addCleanup(recreate_database, self.conn)
        with self.conn:
            self.conn.execute(SCHEMA_BASELINE_SQL.read_text(encoding="utf-8"))
        created = set(db.table_names(self.conn)) - {SCHEMA_MIGRATIONS_TABLE}
        self.assertEqual(created, set(APPLICATION_TABLES))

    def test_baseline_seeds_the_reconcile_schedule_disabled(self):
        recreate_database(self.conn)
        row = self.conn.execute(
            "SELECT enabled, next_run_at, revision, action FROM admin_schedules "
            "WHERE id = 'duplicate-reconcile'"
        ).fetchone()
        self.assertEqual(row[0], False)
        self.assertIsNone(row[1])
        self.assertEqual(row[2], 1)
        self.assertEqual(row[3], "duplicate_reconcile")

    def test_apply_migrations_is_idempotent(self):
        recreate_database(self.conn)
        self.assertEqual(apply_migrations(self.conn), [])
        self.assertTrue(get_migration_status(self.conn).is_up_to_date)

    def test_apply_migrations_on_an_empty_database_stamps_the_baseline(self):
        _drop_everything(self.conn)
        self.addCleanup(recreate_database, self.conn)
        applied = apply_migrations(self.conn)
        self.assertEqual([m.version for m in applied], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])

    def test_concurrent_apply_migrations_is_serialized(self):
        # Containers starting together may both call apply_migrations on an
        # empty database; the advisory lock must keep the second writer from
        # hitting a duplicate-key insert in schema_migrations.
        _drop_everything(self.conn)
        self.addCleanup(recreate_database, self.conn)

        results: list[list] = []
        errors: list[BaseException] = []

        def worker():
            try:
                results.append(apply_migrations(self.db_url))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(len(result) for result in results), [0, len(MIGRATIONS)])
        duplicate_rows = self.conn.execute(
            f"SELECT version, COUNT(*) FROM {SCHEMA_MIGRATIONS_TABLE} "
            "GROUP BY version HAVING COUNT(*) > 1"
        ).fetchall()
        self.assertEqual(duplicate_rows, [])

    def test_recreate_database_clears_old_data_and_reapplies(self):
        recreate_database(self.conn)
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, title, duration) VALUES ('v-test-1', 'Test Video', 600)")
        self.assertIsNotNone(
            self.conn.execute("SELECT id FROM videos WHERE id='v-test-1'").fetchone())

        applied = recreate_database(self.conn)
        self.assertEqual([m.version for m in applied], [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertIsNone(
            self.conn.execute("SELECT id FROM videos WHERE id='v-test-1'").fetchone())

    def test_stale_rows_of_the_removed_chain_are_dropped(self):
        recreate_database(self.conn)
        with self.conn:
            _record(self.conn, 11, "0011_admin_operation_plans")
            _record(self.conn, 77, "0077_remove_dangling_synced_files")

        self.assertEqual(apply_migrations(self.conn), [])

        versions = [record.version for record in get_applied_migrations(self.conn)]
        self.assertNotIn(11, versions)
        self.assertNotIn(77, versions)
        status = get_migration_status(self.conn)
        self.assertEqual(status.current_version, 10)
        self.assertTrue(status.is_up_to_date)

    def test_a_new_migration_takes_over_a_number_the_removed_chain_held(self):
        recreate_database(self.conn)
        self.addCleanup(recreate_database, self.conn)
        with self.conn:
            _record(self.conn, 11, "0011_admin_operation_plans")

        def _add_marker(conn, **_kwargs):
            conn.execute("ALTER TABLE videos ADD COLUMN IF NOT EXISTS test_marker integer")

        fake = Migration(version=11, name="0011_test_marker", description="fixture",
                         apply_fn=_add_marker)
        with mock.patch("common.migrations.runner.MIGRATIONS", MIGRATIONS + (fake,)):
            applied = apply_migrations(self.conn)

        self.assertEqual([record.version for record in applied], [11])
        self.assertEqual(applied[0].name, "0011_test_marker")
        self.assertIn("test_marker", db.table_columns(self.conn, "videos"))

    def test_catalog_sort_indexes_exist_after_the_chain(self):
        recreate_database(self.conn)
        names = {row[0] for row in self.conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename='videos'").fetchall()}
        self.assertIn("idx_videos_client_latest", names)
        self.assertIn("idx_videos_client_duration", names)

    def test_catalog_sort_index_migration_is_idempotent(self):
        recreate_database(self.conn)
        with self.conn:
            catalog_sort_indexes.apply(self.conn)
        with self.conn:
            catalog_sort_indexes.apply(self.conn)

    def test_database_with_tables_and_no_history_is_refused(self):
        # A pre-0.6.0 database, or one that lost schema_migrations, must not be
        # stamped as current: the numbered chain it needs no longer exists.
        _drop_everything(self.conn)
        self.addCleanup(recreate_database, self.conn)
        with self.conn:
            self.conn.execute("CREATE TABLE videos (id text PRIMARY KEY)")

        with self.assertRaisesRegex(RuntimeError, "predates the 0.6.0 squash"):
            get_migration_status(self.conn)
        with self.assertRaisesRegex(RuntimeError, "predates the 0.6.0 squash"):
            apply_migrations(self.conn)
        # Nothing but the hand-made probe and the bookkeeping table survives.
        self.assertEqual(
            sorted(db.table_names(self.conn)),
            ["schema_migrations", "videos"],
        )

    def test_database_with_old_history_but_no_baseline_is_refused(self):
        _drop_everything(self.conn)
        self.addCleanup(recreate_database, self.conn)
        with self.conn:
            self.conn.execute("CREATE TABLE videos (id text PRIMARY KEY)")
            ensure_schema_migrations_table(self.conn)
            _record(self.conn, 40, "0040_performer_profile_overrides")

        with self.assertRaisesRegex(RuntimeError, "predates the 0.6.0 squash"):
            apply_migrations(self.conn)
        self.assertEqual(
            [row[0] for row in self.conn.execute(
                f"SELECT version FROM {SCHEMA_MIGRATIONS_TABLE}").fetchall()],
            [40],
        )

    def test_recorded_baseline_without_tables_rebuilds_the_schema(self):
        # A stale history row is metadata, not a promise: a dropped schema is
        # recreated rather than reported as up to date.
        _drop_everything(self.conn)
        self.addCleanup(recreate_database, self.conn)
        ensure_schema_migrations_table(self.conn)
        with self.conn:
            for migration in MIGRATIONS:
                _record(self.conn, migration.version, migration.name)

        self.assertTrue(get_migration_status(self.conn).is_up_to_date)
        self.assertEqual(recreate_database(self.conn)[0].version, 1)
        self.assertIn("videos", db.table_names(self.conn))


class TestMigrationRecoverability(PgTestCase):
    """A failing statement must leave the schema at the last committed state."""

    def test_failed_migration_does_not_corrupt_schema(self):
        _drop_everything(self.conn)
        self.addCleanup(recreate_database, self.conn)
        ensure_schema_migrations_table(self.conn)
        with self.conn:
            baseline.apply(self.conn)
            _record(self.conn, baseline.version, baseline.name)
        snapshot_before = schema_snapshot(self.conn)

        try:
            with self.conn:
                self.conn.execute("CREATE TABLE _test_rollback_probe (x int)")
                raise RuntimeError("simulated migration failure")
        except RuntimeError:
            pass

        self.assertEqual(snapshot_before, schema_snapshot(self.conn))
        self.assertNotIn("_test_rollback_probe", db.table_names(self.conn))

    def test_idempotent_apply_does_not_alter_schema(self):
        _drop_everything(self.conn)
        self.addCleanup(recreate_database, self.conn)
        ensure_schema_migrations_table(self.conn)
        apply_migrations(self.conn)
        snapshot_before = schema_snapshot(self.conn)

        apply_migrations(self.conn)

        self.assertEqual(snapshot_before, schema_snapshot(self.conn))


if __name__ == "__main__":
    unittest.main()
