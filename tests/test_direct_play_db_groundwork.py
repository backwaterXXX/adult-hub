"""Tests for Direct Play Stage 1 additive database groundwork."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from common import db
from common.migrations import (
    APPLICATION_TABLES,
    MIGRATIONS,
    USER_STATE_TABLES,
    apply_migrations,
    recreate_database,
)
from common.migrations.base import MigrationRecord
from common.migrations.legacy import POST_BASELINE_MIGRATIONS
from common.migrations.m0001_baseline import SCHEMA_BASELINE_SQL
from common.migrations.m0069_direct_play_user_state import (
    current_migration,
    migration as legacy_migration,
)
from common.migrations.runner import (
    CURRENT_MIGRATIONS,
    POST_BASELINE_CEILING,
    _migration_plan,
    _validate_active_migrations,
)
from tests.pg_helper import PgTestCase


def _rec(version: int, name: str) -> MigrationRecord:
    return MigrationRecord(version, name, datetime.now(timezone.utc))


class TestDirectPlayMigrationRegistration(unittest.TestCase):
    """Verifies that Stage 1 additive groundwork is cleanly registered."""

    def test_legacy_migration_registered_and_ordered(self):
        self.assertIn(legacy_migration, POST_BASELINE_MIGRATIONS)
        self.assertEqual(legacy_migration.version, 69)
        self.assertEqual(legacy_migration.name, "0069_direct_play_user_state")
        self.assertEqual(POST_BASELINE_CEILING, 75)
        self.assertIn(legacy_migration, POST_BASELINE_MIGRATIONS)
        _validate_active_migrations(POST_BASELINE_MIGRATIONS)

    def test_current_migration_discovered_and_ordered(self):
        self.assertIn(current_migration, CURRENT_MIGRATIONS)
        self.assertEqual(current_migration.version, 21)
        self.assertEqual(current_migration.name, "0021_direct_play_user_state")
        self.assertIn(current_migration, MIGRATIONS)
        _validate_active_migrations(MIGRATIONS)

    def test_application_tables_contain_direct_play_tables(self):
        for table in (
            "client_users",
            "client_user_watch_progress",
            "client_user_favorites",
        ):
            self.assertIn(table, APPLICATION_TABLES)
            self.assertIn(table, USER_STATE_TABLES)
        self.assertEqual(len(USER_STATE_TABLES), 3)

    def test_baseline_sql_contains_additive_schema_definitions(self):
        baseline_content = SCHEMA_BASELINE_SQL.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS public.client_users", baseline_content)
        self.assertIn("CREATE TABLE IF NOT EXISTS public.client_user_watch_progress", baseline_content)
        self.assertIn("CREATE TABLE IF NOT EXISTS public.client_user_favorites", baseline_content)
        self.assertIn("client_users_pkey", baseline_content)
        self.assertIn("client_user_watch_progress_pkey", baseline_content)
        self.assertIn("client_user_favorites_pkey", baseline_content)
        self.assertIn("client_user_watch_progress_position_seconds_check", baseline_content)
        self.assertIn("client_user_watch_progress_duration_seconds_check", baseline_content)
        self.assertIn("client_user_watch_progress_played_percentage_check", baseline_content)
        self.assertIn("idx_client_users_username", baseline_content)
        self.assertIn("idx_client_user_watch_progress_video_id", baseline_content)
        self.assertIn("idx_client_user_favorites_video_id", baseline_content)

    def test_canonical_id_merge_semantics_validation(self):
        """Validates that user state tables avoid restrictive or cascading foreign keys

        to `videos(id)`, ensuring duplicate merge retirement does not wipe watch progress.
        """
        baseline_content = SCHEMA_BASELINE_SQL.read_text(encoding="utf-8")
        # user_id must cascade from client_users
        self.assertIn(
            "client_user_watch_progress_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.client_users(id) ON DELETE CASCADE",
            baseline_content,
        )
        self.assertIn(
            "client_user_favorites_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.client_users(id) ON DELETE CASCADE",
            baseline_content,
        )
        # video_id must NOT have FK to videos(id)
        self.assertNotIn("client_user_watch_progress_video_id_fkey", baseline_content)
        self.assertNotIn("client_user_favorites_video_id_fkey", baseline_content)

    def test_recreate_drop_selection_preserves_user_state(self):
        """Recreation by default drops prior application tables but preserves user state at all target versions."""
        executed_sqls: list[str] = []

        mock_conn = MagicMock(spec=db.Connection)
        mock_conn.execute.side_effect = lambda sql, *args: executed_sqls.append(sql)

        # preserve_user_state=True (default) without target_version
        with unittest.mock.patch("common.migrations.runner.apply_migrations", return_value=[]):
            recreate_database(mock_conn, preserve_user_state=True)

        dropped_tables = [
            sql.replace("DROP TABLE IF EXISTS ", "").replace(" CASCADE", "")
            for sql in executed_sqls
            if sql.startswith("DROP TABLE IF EXISTS")
        ]
        for user_tbl in USER_STATE_TABLES:
            self.assertNotIn(user_tbl, dropped_tables)
        self.assertIn("videos", dropped_tables)
        self.assertIn("schema_migrations", dropped_tables)

        # preserve_user_state=True (default) with target_version < INTERIM_BASELINE_VERSION (no implicit deletion)
        executed_sqls.clear()
        with unittest.mock.patch("common.migrations.runner.apply_migrations", return_value=[]):
            recreate_database(mock_conn, target_version=8)

        dropped_tables_v8 = [
            sql.replace("DROP TABLE IF EXISTS ", "").replace(" CASCADE", "")
            for sql in executed_sqls
            if sql.startswith("DROP TABLE IF EXISTS")
        ]
        for user_tbl in USER_STATE_TABLES:
            self.assertNotIn(user_tbl, dropped_tables_v8)
        self.assertIn("videos", dropped_tables_v8)
        self.assertIn("schema_migrations", dropped_tables_v8)

        # Explicit preserve_user_state=False with target_version < INTERIM_BASELINE_VERSION
        executed_sqls.clear()
        with unittest.mock.patch("common.migrations.runner.apply_migrations", return_value=[]):
            recreate_database(mock_conn, target_version=8, preserve_user_state=False)

        dropped_tables_v8_destructive = [
            sql.replace("DROP TABLE IF EXISTS ", "").replace(" CASCADE", "")
            for sql in executed_sqls
            if sql.startswith("DROP TABLE IF EXISTS")
        ]
        for user_tbl in USER_STATE_TABLES:
            self.assertIn(user_tbl, dropped_tables_v8_destructive)

        # preserve_user_state=False
        executed_sqls.clear()
        with unittest.mock.patch("common.migrations.runner.apply_migrations", return_value=[]):
            recreate_database(mock_conn, preserve_user_state=False)

        dropped_tables_full = [
            sql.replace("DROP TABLE IF EXISTS ", "").replace(" CASCADE", "")
            for sql in executed_sqls
            if sql.startswith("DROP TABLE IF EXISTS")
        ]
        for user_tbl in USER_STATE_TABLES:
            self.assertIn(user_tbl, dropped_tables_full)

    def test_lineage_a_historical_chain_isolation_and_no_collision(self):
        """Historical pre-squash databases carry legacy 21 ('0021_performer_catalog')

        and must advance to v69 ('0069_direct_play_user_state') without colliding with
        current forward-track v21 ('0021_direct_play_user_state').
        """
        # A historical database carrying baseline 1 and post-baseline migrations up to 68
        # (which includes legacy 21: '0021_performer_catalog')
        applied = [_rec(1, "0001_baseline")] + [
            _rec(m.version, m.name) for m in POST_BASELINE_MIGRATIONS if m.version <= 68
        ]
        # Confirm that legacy 21 is indeed among applied records
        applied_names = {r.version: r.name for r in applied}
        self.assertEqual(applied_names[21], "0021_performer_catalog")

        pending = _migration_plan(applied)
        self.assertEqual(pending[0].version, 69)
        self.assertEqual(pending[0].name, "0069_direct_play_user_state")

        # After applying 69 through 75, replay is idempotent
        applied.append(_rec(69, "0069_direct_play_user_state"))
        applied.append(_rec(70, "0070_client_auth"))
        applied.append(_rec(71, "0071_telegram_sources_backfill"))
        applied.append(_rec(72, "0072_performer_lookup_indexes"))
        applied.append(_rec(73, "0073_performer_catalog_image"))
        applied.append(_rec(74, "0074_drop_jellyfin_person_sync"))
        applied.append(_rec(75, "0075_drop_nfo_audit_kinds"))
        self.assertEqual(_migration_plan(applied), [])

    def test_lineage_b_current_forward_chain_isolation_and_no_collision(self):
        """Modern databases starting from baseline v1 apply current forward upgrades

        2..21, terminating at current v21 ('0021_direct_play_user_state') without touching
        historical migrations 10..69.
        """
        applied = [_rec(1, "0001_baseline")] + [
            _rec(m.version, m.name) for m in CURRENT_MIGRATIONS if m.version < 21
        ]
        pending = _migration_plan(applied)
        self.assertEqual(pending[0].version, 21)
        self.assertEqual(pending[0].name, "0021_direct_play_user_state")

        # After applying 21, 22, 23, 24, and 25, replay is idempotent
        applied.append(_rec(21, "0021_direct_play_user_state"))
        applied.append(_rec(22, "0022_client_auth"))
        applied.append(_rec(23, "0023_telegram_sources_backfill"))
        applied.append(_rec(24, "0024_performer_lookup_indexes"))
        applied.append(_rec(25, "0025_performer_catalog_image"))
        self.assertEqual(_migration_plan(applied), [])

    def test_old_code_rollback_nondestructive_on_migrated_database(self):
        """Simulating old application code inspecting a database upgraded to v21/v69.

        Old code has CURRENT_MIGRATIONS up to 20 and POST_BASELINE up to 68.
        When encountering a database with the new migration applied, pending is empty
        and no destructive down-migrations or wipes occur.
        """
        # Old code's known migration definitions
        old_current = tuple(m for m in CURRENT_MIGRATIONS if m.version < 21)
        old_post_baseline = tuple(m for m in POST_BASELINE_MIGRATIONS if m.version < 69)

        # Database on Lineage B with v21 applied
        applied_b = [_rec(1, "0001_baseline")] + [_rec(m.version, m.name) for m in CURRENT_MIGRATIONS]
        applied_versions_b = {r.version for r in applied_b}
        pending_b = [m for m in old_current if m.version not in applied_versions_b]
        self.assertEqual(pending_b, [])

        # Database on Lineage A with v69 applied
        applied_a = [_rec(1, "0001_legacy_initial_schema")] + [
            _rec(m.version, m.name) for m in POST_BASELINE_MIGRATIONS
        ]
        applied_versions_a = {r.version for r in applied_a}
        pending_a = [m for m in old_post_baseline if m.version not in applied_versions_a]
        self.assertEqual(pending_a, [])


class TestDirectPlayDatabaseGroundworkLive(PgTestCase):
    """Executes schema creation and tests DDL against disposable TEST_DATABASE_URL when available."""

    def test_direct_play_tables_created_and_queryable(self):
        existing = set(self.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall())
        for table in ("client_users", "client_user_watch_progress", "client_user_favorites"):
            self.assertIn((table,), existing)

    def test_recreate_database_preserves_user_state_live(self):
        # Insert a client user and watch progress record
        with self.conn:
            self.conn.execute(
                "INSERT INTO client_users (id, username, password_hash) VALUES ('u-live-1', 'testuser', 'hash123')"
            )
            self.conn.execute(
                "INSERT INTO client_user_watch_progress (user_id, video_id, position_seconds, duration_seconds, played_percentage) "
                "VALUES ('u-live-1', 'vid-test-1', 120, 600, 20.0)"
            )
            self.conn.execute(
                "INSERT INTO videos (id, title, duration) VALUES ('vid-test-1', 'Test Title', 600)"
            )

        # Verify initial rows
        self.assertIsNotNone(self.conn.execute("SELECT id FROM client_users WHERE id='u-live-1'").fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT user_id FROM client_user_watch_progress WHERE user_id='u-live-1'").fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT id FROM videos WHERE id='vid-test-1'").fetchone())

        # Recreate with default preserve_user_state=True
        recreate_database(self.conn)

        # Prior application tables (videos) were cleared
        self.assertIsNone(self.conn.execute("SELECT id FROM videos WHERE id='vid-test-1'").fetchone())

        # User state tables were preserved intact!
        user_row = self.conn.execute("SELECT id, username FROM client_users WHERE id='u-live-1'").fetchone()
        self.assertIsNotNone(user_row)
        self.assertEqual(user_row[0], "u-live-1")

        progress_row = self.conn.execute(
            "SELECT position_seconds, duration_seconds, played_percentage FROM client_user_watch_progress WHERE user_id='u-live-1'"
        ).fetchone()
        self.assertIsNotNone(progress_row)
        self.assertEqual(progress_row[0], 120)
        self.assertEqual(progress_row[1], 600)
        self.assertAlmostEqual(progress_row[2], 20.0)

        # Recreate with preserve_user_state=False wipes user state too
        recreate_database(self.conn, preserve_user_state=False)
        self.assertIsNone(self.conn.execute("SELECT id FROM client_users WHERE id='u-live-1'").fetchone())
        self.assertIsNone(self.conn.execute("SELECT user_id FROM client_user_watch_progress WHERE user_id='u-live-1'").fetchone())

    def test_recreate_database_preserves_user_state_at_older_targets_live(self):
        """Recreation targeting older pre-baseline versions (target_version < INTERIM_BASELINE_VERSION)

        must preserve user-state tables unless explicitly requested by caller.
        """
        try:
            # Insert client user, watch progress, and favorite
            with self.conn:
                self.conn.execute(
                    "INSERT INTO client_users (id, username, password_hash) VALUES ('u-v8-1', 'v8user', 'hash789')"
                )
                self.conn.execute(
                    "INSERT INTO client_user_watch_progress (user_id, video_id, position_seconds, duration_seconds, played_percentage) "
                    "VALUES ('u-v8-1', 'vid-v8-1', 45, 300, 15.0)"
                )
                self.conn.execute(
                    "INSERT INTO client_user_favorites (user_id, video_id) VALUES ('u-v8-1', 'vid-v8-1')"
                )

            # Recreate to target_version=8 (default preserve_user_state=True)
            recreate_database(self.conn, target_version=8)

            # Verify user state tables and rows are preserved intact across recreation to v8
            user_row = self.conn.execute(
                "SELECT id, username FROM client_users WHERE id='u-v8-1'"
            ).fetchone()
            self.assertIsNotNone(user_row)
            self.assertEqual(user_row[0], "u-v8-1")

            progress_row = self.conn.execute(
                "SELECT position_seconds, duration_seconds, played_percentage FROM client_user_watch_progress WHERE user_id='u-v8-1'"
            ).fetchone()
            self.assertIsNotNone(progress_row)
            self.assertEqual(progress_row[0], 45)
            self.assertEqual(progress_row[1], 300)
            self.assertAlmostEqual(progress_row[2], 15.0)

            fav_row = self.conn.execute(
                "SELECT user_id, video_id FROM client_user_favorites WHERE user_id='u-v8-1'"
            ).fetchone()
            self.assertIsNotNone(fav_row)
            self.assertEqual(fav_row[0], "u-v8-1")

            # Roll forward all migrations to latest version; verify existing user rows remain untouched
            apply_migrations(self.conn)

            user_row_post = self.conn.execute(
                "SELECT id, username FROM client_users WHERE id='u-v8-1'"
            ).fetchone()
            self.assertIsNotNone(user_row_post)
            self.assertEqual(user_row_post[0], "u-v8-1")

            # Verify explicit preserve_user_state=False drops user state
            recreate_database(self.conn, target_version=8, preserve_user_state=False)
            user_table_exists = self.conn.execute("SELECT to_regclass('client_users')").fetchone()[0] is not None
            if user_table_exists:
                self.assertIsNone(self.conn.execute("SELECT id FROM client_users WHERE id='u-v8-1'").fetchone())
            else:
                self.assertFalse(user_table_exists)
        finally:
            try:
                self.conn.rollback()
            except Exception:
                pass
            recreate_database(self.conn)

    def test_watch_progress_check_constraints_live(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO client_users (id, username, password_hash) VALUES ('u-ck-1', 'checkuser', 'hash456')"
            )

        # Negative position_seconds rejected
        with self.assertRaises(Exception):
            with self.conn:
                self.conn.execute(
                    "INSERT INTO client_user_watch_progress (user_id, video_id, position_seconds, duration_seconds, played_percentage) "
                    "VALUES ('u-ck-1', 'v1', -5, 600, 10.0)"
                )

        # Negative duration_seconds rejected
        with self.assertRaises(Exception):
            with self.conn:
                self.conn.execute(
                    "INSERT INTO client_user_watch_progress (user_id, video_id, position_seconds, duration_seconds, played_percentage) "
                    "VALUES ('u-ck-1', 'v2', 0, -10, 10.0)"
                )

        # played_percentage < 0.0 rejected
        with self.assertRaises(Exception):
            with self.conn:
                self.conn.execute(
                    "INSERT INTO client_user_watch_progress (user_id, video_id, position_seconds, duration_seconds, played_percentage) "
                    "VALUES ('u-ck-1', 'v3', 0, 600, -1.0)"
                )

        # played_percentage > 100.0 rejected
        with self.assertRaises(Exception):
            with self.conn:
                self.conn.execute(
                    "INSERT INTO client_user_watch_progress (user_id, video_id, position_seconds, duration_seconds, played_percentage) "
                    "VALUES ('u-ck-1', 'v4', 0, 600, 100.5)"
                )

        # Valid progress values accepted
        with self.conn:
            self.conn.execute(
                "INSERT INTO client_user_watch_progress (user_id, video_id, position_seconds, duration_seconds, played_percentage) "
                "VALUES ('u-ck-1', 'v5', 300, 600, 50.0)"
            )
        row = self.conn.execute(
            "SELECT position_seconds, played_percentage FROM client_user_watch_progress WHERE user_id='u-ck-1' AND video_id='v5'"
        ).fetchone()
        self.assertEqual(row[0], 300)
        self.assertAlmostEqual(row[1], 50.0)


if __name__ == "__main__":
    unittest.main()
