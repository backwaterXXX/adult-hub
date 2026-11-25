"""Tests for Direct Play additive database groundwork (user state tables)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from common import db
from common.migrations import (
    APPLICATION_TABLES,
    USER_STATE_TABLES,
    recreate_database,
)
from common.migrations.m0001_baseline import SCHEMA_BASELINE_SQL
from tests.pg_helper import PgTestCase


class TestDirectPlaySchemaGroundwork(unittest.TestCase):
    """Verifies that the Direct Play user-state tables stay part of the schema."""

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
        """Recreation by default drops prior application tables but preserves user state."""
        executed_sqls: list[str] = []

        mock_conn = MagicMock(spec=db.Connection)
        mock_conn.execute.side_effect = lambda sql, *args: executed_sqls.append(sql)

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
