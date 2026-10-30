import logging
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase
from sync.telegram_pipeline import TelegramSyncServices, sync_telegram_channel_pipeline


class LatestCursorTests(PgTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.channel = "-100123"
        self.cfg = SimpleNamespace(max_pages=1, history_batch_size=100, min_duration_seconds=420,
                                   tags_whitelist=[], tags_blacklist=[], studio="test")
        with self.conn:
            self.conn.execute("INSERT INTO telegram_channel_state(channel_id,last_min_duration,last_tags_whitelist,last_tags_blacklist,backfill_in_progress,latest_watermark) VALUES (%s,420,'[]','[]',0,100)", (self.channel,))

    def run_scan(self, responses, flush=None):
        urls = []
        def fetch(url, **kwargs):
            urls.append(url)
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        services = TelegramSyncServices("http://r", "http://r", lambda x: None,
            lambda *x: None, lambda *x, **k: 0, fetch,
            flush or (lambda *x, **k: None), logging.getLogger(__name__), time)
        sync_telegram_channel_pipeline(self.channel, self.cfg, self.conn, Path(self.tmp.name), {}, services=services, sync_mode="new")
        return urls

    def state(self):
        return self.conn.execute("SELECT latest_watermark,latest_cursor,latest_head FROM telegram_channel_state WHERE channel_id=%s", (self.channel,)).fetchone()

    def test_bounded_batches_resume_then_capture_new_head_next_run(self):
        first = {"videos": [], "message_count": 50, "highest_id": 200, "next_offset_id": 151, "has_more": True}
        second = {"videos": [], "message_count": 50, "highest_id": 150, "next_offset_id": 101, "has_more": True}
        third = {"videos": [], "message_count": 2, "highest_id": 100, "next_offset_id": 99, "has_more": False}
        self.run_scan([first])
        self.assertEqual(self.state(), (100, 151, 200))
        self.assertIn("offset_id=151", self.run_scan([second])[0])
        self.assertEqual(self.state(), (100, 101, 200))
        self.run_scan([third])
        self.assertEqual(self.state(), (200, None, None))
        self.run_scan([{"videos": [], "message_count": 2, "highest_id": 205, "next_offset_id": 199, "has_more": False}])
        self.assertEqual(self.state(), (205, None, None))

    def test_failed_write_does_not_advance(self):
        def fail(*args, **kwargs):
            raise RuntimeError("write failed")
        with self.assertRaises(RuntimeError):
            self.run_scan([{"videos": [], "message_count": 50, "highest_id": 200, "next_offset_id": 151, "has_more": True}], flush=fail)
        self.assertEqual(self.state(), (100, None, None))

    def test_deleted_boundary_and_partial_page_complete(self):
        self.run_scan([{"videos": [], "message_count": 50, "highest_id": 149,
                        "next_offset_id": 99, "has_more": True}])
        self.assertEqual(self.state(), (149, None, None))

    def test_empty_initial_channel_keeps_watermark_unset(self):
        self.conn.execute("UPDATE telegram_channel_state SET latest_watermark=NULL WHERE channel_id=%s", (self.channel,))
        self.conn.commit()
        self.run_scan([{"videos": [], "message_count": 0, "highest_id": None,
                        "next_offset_id": None, "has_more": False}])
        self.assertEqual(self.state(), (None, None, None))

    def test_current_migrations_are_idempotent(self):
        from common.migrations import apply_migrations
        self.assertEqual(apply_migrations(self.db_url), [])

    def test_legacy_upgrade_restarts_completed_history_only(self):
        from common.migrations import apply_migrations
        with self.conn:
            self.conn.execute("INSERT INTO telegram_channel_state(channel_id,backfill_in_progress,backfill_cursor) VALUES ('other',1,42)")
            self.conn.execute("DELETE FROM schema_migrations WHERE version > 1")
            for column in ("latest_watermark", "latest_cursor", "latest_head"):
                self.conn.execute(f"ALTER TABLE telegram_channel_state DROP COLUMN {column}")
            self.conn.execute("INSERT INTO schema_migrations(version,name,applied_at) VALUES (53,'0053_visual_history',CURRENT_TIMESTAMP)")
            # Historical migration records prove all preceding upgrades ran.
            from common.migrations.legacy import POST_BASELINE_MIGRATIONS
            for migration in POST_BASELINE_MIGRATIONS:
                if migration.version < 53:
                    self.conn.execute("INSERT INTO schema_migrations(version,name,applied_at) VALUES (%s,%s,CURRENT_TIMESTAMP)", (migration.version, migration.name))
        # Derived from the chain, not written out: a migration registered after
        # version 53 belongs in this expectation without editing the test.
        self.assertEqual([item.version for item in apply_migrations(self.db_url)],
                         [migration.version for migration in POST_BASELINE_MIGRATIONS
                          if migration.version > 53])
        rows = self.conn.execute("SELECT channel_id,backfill_in_progress,backfill_cursor FROM telegram_channel_state ORDER BY channel_id").fetchall()
        self.assertEqual(rows, [(self.channel, 1, None), ("other", 1, 42)])
