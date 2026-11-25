"""Tests for the squashed migration baseline and schema recreation engine."""

from __future__ import annotations

import threading
import unittest

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

    def test_chain_is_the_single_squashed_baseline(self):
        self.assertEqual([migration.version for migration in MIGRATIONS], [1])
        self.assertEqual(MIGRATIONS[0].name, "0001_baseline")
        self.assertIs(MIGRATIONS[0], baseline)


class TestMigrationsEngine(PgTestCase):
    def test_fresh_database_applies_one_baseline_and_is_then_up_to_date(self):
        applied = recreate_database(self.conn)
        self.assertEqual([m.version for m in applied], [1])
        self.assertEqual(applied[0].name, "0001_baseline")

        status = get_migration_status(self.conn)
        self.assertTrue(status.is_up_to_date)
        self.assertEqual(status.current_version, 1)
        self.assertEqual(status.pending, [])

        records = get_applied_migrations(self.conn)
        self.assertEqual([(r.version, r.name) for r in records], [(1, "0001_baseline")])

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
        self.assertEqual([m.version for m in applied], [1])

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
        self.assertEqual(sorted(len(result) for result in results), [0, 1])
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
        self.assertEqual([m.version for m in applied], [1])
        self.assertIsNone(
            self.conn.execute("SELECT id FROM videos WHERE id='v-test-1'").fetchone())

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
            _record(self.conn, 1, "0001_baseline")

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
