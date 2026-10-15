import unittest
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.config_loader import AppConfig, SourceConfig, TelegramChannelConfig, SyncConfig
sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).parent.parent / "resolver"))
try:
    import sync as sync_module
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    sync_module = None


class TestConfigSourcesMigration(PgTestCase):

    def setUp(self):
        super().setUp()
        if sync_module is None:
            self.skipTest("sync module not available in resolver container")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.media_dir = Path(self.temp_dir.name) / "media"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_url

    def tearDown(self):
        if sync_module is not None:
            self.temp_dir.cleanup()

    def _insert_video(self, vid_id: str, provider: str, title: str, duration: int, last_seen: int):
        with self.conn:
            self.conn.execute("""
                INSERT INTO videos (id, provider, title, duration, last_seen)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET provider=EXCLUDED.provider,
                    title=EXCLUDED.title,duration=EXCLUDED.duration,last_seen=EXCLUDED.last_seen
            """, (vid_id, provider, title, duration, last_seen))
            strm = self.media_dir / f"{title}.strm"
            strm.write_text("http://stream\n")
            self.conn.execute("""
                INSERT INTO synced_files (file_path, video_id, category, created_at)
                VALUES (%s, %s, 'test', %s)
                ON CONFLICT (file_path) DO UPDATE SET video_id=EXCLUDED.video_id,
                    category=EXCLUDED.category,created_at=EXCLUDED.created_at
            """, (str(strm), vid_id, last_seen))

    def test_retention_preserves_disabled_provider_and_channel(self):
        """Task 5: cleanup_retention() preserves disabled provider and disabled telegram channel."""
        now = int(time.time())
        old_time = now - (40 * 86400)  # 40 days old (> 30 days retention)

        # 1. Enabled provider (exampleweb) -> expired
        self._insert_video("exampleweb:101", "exampleweb", "Example Video", 600, old_time)
        # 2. Disabled provider (exampleweb2) -> expired but disabled
        self._insert_video("exampleweb2:201", "exampleweb2", "Disabled Video", 600, old_time)
        # 3. Enabled telegram channel (-1000000000001) -> expired
        self._insert_video("telegram:-1000000000001:1:u1", "telegram", "TG Active", 600, old_time)
        # 4. Disabled telegram channel (-1000000000002) -> expired but channel disabled
        self._insert_video("telegram:-1000000000002:2:u2", "telegram", "TG Disabled", 600, old_time)

        mock_config = AppConfig(
            sync=SyncConfig(retention_days=30),
            sources={
                "exampleweb": SourceConfig(provider="exampleweb", enabled=True, address="https://exampleweb.example.com"),
                "exampleweb2": SourceConfig(provider="exampleweb2", enabled=False, address="https://exampleweb2.example.com"),
                "telegram": SourceConfig(
                    provider="telegram",
                    enabled=True,
                    channels=[
                        TelegramChannelConfig(id="-1000000000001", enabled=True),
                        TelegramChannelConfig(id="-1000000000002", enabled=False),
                    ]
                )
            }
        )

        orig_config = sync_module.CONFIG
        orig_info = dict(sync_module._PROVIDER_INFO)
        sync_module._PROVIDER_INFO["exampleweb"] = {
            "name": "Exampleweb", "domain": "exampleweb.example.com",
            "base_url": "https://exampleweb.example.com"}
        try:
            sync_module.CONFIG = mock_config
            sync_module.cleanup_retention(
                self.conn, self.media_dir, 30,
                {"exampleweb", "telegram", "telegram:-1000000000001"})

            # Check what's in DB
            remaining = {r[0] for r in self.conn.execute("SELECT id FROM videos").fetchall()}

            # Enabled ones must be purged
            self.assertNotIn("exampleweb:101", remaining)
            self.assertNotIn("telegram:-1000000000001:1:u1", remaining)

            # Disabled ones must be PRESERVED
            self.assertIn("exampleweb2:201", remaining)
            self.assertIn("telegram:-1000000000002:2:u2", remaining)
        finally:
            sync_module.CONFIG = orig_config
            sync_module._PROVIDER_INFO.clear()
            sync_module._PROVIDER_INFO.update(orig_info)

    def test_telegram_tighten_filter_purges_stored_short_videos(self):
        """Task 6: Tightening filter (e.g. 420 -> 600) purges existing videos with duration < 600."""
        chan_id = "-1000000000001"
        now = int(time.time())

        # Seed channel state with last_min_duration = 420
        with self.conn:
            self.conn.execute("""
                INSERT INTO telegram_channel_state (channel_id, last_min_duration, backfill_in_progress, backfill_cursor, updated_at)
                VALUES (%s, 420, 0, NULL, %s)
            """, (chan_id, now))

        # Seed stored videos: one long (700s), one short (480s)
        self._insert_video(f"telegram:{chan_id}:10:u1", "telegram", "Long Vid", 700, now)
        self._insert_video(f"telegram:{chan_id}:11:u2", "telegram", "Short Vid", 480, now)

        # New channel config with tightened filter: min_duration_seconds = 600
        chan_cfg = TelegramChannelConfig(id=chan_id, studio="Test Studio", min_duration_seconds=600, max_pages=1)

        collected = {}
        effective = AppConfig(sources={"telegram": SourceConfig(
            provider="telegram", enabled=True, channels=[chan_cfg])})
        with patch.object(sync_module, "CONFIG", effective), patch("sync.fetch_json_with_retry", return_value=[]):
            sync_module.sync_telegram_channel_pipeline(chan_id, chan_cfg, self.conn, self.media_dir, collected)

        remaining = {r[0] for r in self.conn.execute("SELECT id FROM videos").fetchall()}
        self.assertIn(f"telegram:{chan_id}:10:u1", remaining)  # 700s kept
        self.assertNotIn(f"telegram:{chan_id}:11:u2", remaining)  # 480s purged!

        # State updated
        state = self.conn.execute("SELECT last_min_duration, backfill_in_progress FROM telegram_channel_state WHERE channel_id = %s", (chan_id,)).fetchone()
        self.assertEqual(state[0], 600)
        self.assertEqual(state[1], 0)

    def test_telegram_relax_filter_triggers_backfill_and_saves_cursor(self):
        """Task 6: Relaxing filter (e.g. 600 -> 300) triggers full history backfill and preserves cursor."""
        chan_id = "-1000000000001"
        now = int(time.time())

        # Seed channel state with last_min_duration = 600
        with self.conn:
            self.conn.execute("""
                INSERT INTO telegram_channel_state (channel_id, last_min_duration, backfill_in_progress, backfill_cursor, updated_at)
                VALUES (%s, 600, 0, NULL, %s)
            """, (chan_id, now))

        # Channel relaxed to 300s
        chan_cfg = TelegramChannelConfig(id=chan_id, studio="Test Studio", min_duration_seconds=300, max_pages=1, history_batch_size=2)

        # Mock backfill batch returning 2 items (lowest message_id is 50)
        mock_batch = [
            {"provider_video_id": f"{chan_id}:60:u60", "title": "Vid 60", "duration": 350, "categories": ["Test"]},
            {"provider_video_id": f"{chan_id}:50:u50", "title": "Vid 50", "duration": 400, "categories": ["Test"]},
        ]

        def fake_fetch(url, **kwargs):
            if "limit=" in url:
                return {"videos": mock_batch, "message_count": 2, "next_offset_id": 50, "has_more": True}
            return []

        collected = {}
        with patch("sync.fetch_json_with_retry", side_effect=fake_fetch):
            sync_module.sync_telegram_channel_pipeline(chan_id, chan_cfg, self.conn, self.media_dir, collected)

        # Backfill cursor should be saved as 50, and backfill_in_progress should be 1
        state = self.conn.execute("SELECT last_min_duration, backfill_in_progress, backfill_cursor FROM telegram_channel_state WHERE channel_id = %s", (chan_id,)).fetchone()
        self.assertEqual(state[0], 300)
        self.assertEqual(state[1], 1)
        self.assertEqual(state[2], 50)

        # Next run: batch returns empty (end of channel)
        with patch("sync.fetch_json_with_retry", side_effect=lambda url, **kwargs: {"videos": [], "message_count": 0, "next_offset_id": None, "has_more": False} if "history/" in url else []):
            sync_module.sync_telegram_channel_pipeline(chan_id, chan_cfg, self.conn, self.media_dir, collected)

        state2 = self.conn.execute("SELECT backfill_in_progress, backfill_cursor, backfill_completed_at FROM telegram_channel_state WHERE channel_id = %s", (chan_id,)).fetchone()
        self.assertEqual(state2[0], 0)  # completed!
        self.assertIsNone(state2[1])
        self.assertIsNotNone(state2[2])

    def test_filtered_empty_history_batch_still_advances_cursor(self):
        chan_id = "-1000000000001"
        cfg = TelegramChannelConfig(id=chan_id, min_duration_seconds=420,
                                    history_batch_size=2, tags_whitelist=["wanted"])
        responses = [
            {"videos": [], "message_count": 2, "next_offset_id": 70, "has_more": True},
            {"videos": [], "message_count": 1, "next_offset_id": 40, "has_more": False},
        ]
        urls = []
        def fetch(url, **kwargs):
            if "history/" in url:
                urls.append(url)
                return responses.pop(0)
            return []
        with patch("sync.fetch_json_with_retry", side_effect=fetch):
            sync_module.sync_telegram_channel_pipeline(chan_id, cfg, self.conn,
                                                       self.media_dir, {})
            state = self.conn.execute("SELECT backfill_in_progress, backfill_cursor FROM telegram_channel_state").fetchone()
            self.assertEqual(state, (1, 70))
            sync_module.sync_telegram_channel_pipeline(chan_id, cfg, self.conn,
                                                       self.media_dir, {})
        self.assertIn("offset_id=70", urls[1])
        state = self.conn.execute("SELECT backfill_in_progress FROM telegram_channel_state").fetchone()
        self.assertEqual(state[0], 0)

    def test_tag_filter_replacement_starts_full_history_scan(self):
        chan_id = "-1000000000001"
        self.conn.execute("""INSERT INTO telegram_channel_state
            (channel_id, last_min_duration, last_tags_whitelist, last_tags_blacklist,
             backfill_in_progress, backfill_cursor, updated_at)
            VALUES (%s, 420, '["old"]', '["blocked"]', 0, 100, %s)""",
            (chan_id, int(time.time())))
        cfg = TelegramChannelConfig(id=chan_id, min_duration_seconds=420,
                                    tags_whitelist=["new"], tags_blacklist=["other"])
        with patch("sync.fetch_json_with_retry", side_effect=lambda url, **kwargs:
                   {"videos": [], "message_count": 1, "next_offset_id": 90,
                    "has_more": True} if "history/" in url else []):
            sync_module.sync_telegram_channel_pipeline(chan_id, cfg, self.conn,
                                                       self.media_dir, {})
        state = self.conn.execute("""SELECT backfill_in_progress, backfill_cursor
            FROM telegram_channel_state WHERE channel_id = %s""", (chan_id,)).fetchone()
        self.assertEqual(state, (1, 90))

    def test_tag_filter_change_rechecks_stored_videos(self):
        chan_id = "-1000000000001"
        now = int(time.time())
        self._insert_video(f"telegram:{chan_id}:10:u1", "telegram", "Tagged", 700, now)
        self.conn.execute("UPDATE videos SET tags = %s WHERE title = 'Tagged'", ('["#other"]',))
        self.conn.execute("""INSERT INTO telegram_channel_state
            (channel_id, last_min_duration, last_tags_whitelist, last_tags_blacklist,
             backfill_in_progress, updated_at) VALUES (%s, 420, '[]', '[]', 0, %s)""",
            (chan_id, now))
        cfg = TelegramChannelConfig(id=chan_id, min_duration_seconds=420,
                                    tags_whitelist=["wanted"])
        effective = AppConfig(sources={"telegram": SourceConfig(
            provider="telegram", enabled=True, channels=[cfg])})
        with patch.object(sync_module, "CONFIG", effective), patch("sync.fetch_json_with_retry", return_value=[]):
            sync_module.sync_telegram_channel_pipeline(chan_id, cfg, self.conn,
                                                       self.media_dir, {})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], 0)

    def test_telegram_failed_history_batch_keeps_resume_cursor(self):
        chan_id = "-1000000000001"
        self.conn.execute("""INSERT INTO telegram_channel_state
            (channel_id, last_min_duration, last_tags_whitelist, last_tags_blacklist,
             backfill_in_progress, backfill_cursor, updated_at)
            VALUES (%s, 420, '[]', '[]', 1, 75, %s)""", (chan_id, int(time.time())))
        cfg = TelegramChannelConfig(id=chan_id, min_duration_seconds=420,
                                    max_pages=1, history_batch_size=2)

        def fail_history(url, **kwargs):
            if "history/" in url:
                raise OSError("temporary resolver failure")
            return []

        with patch("sync.fetch_json_with_retry", side_effect=fail_history):
            with self.assertRaisesRegex(RuntimeError, "fetch/history operations failed"):
                sync_module.sync_telegram_channel_pipeline(
                    chan_id, cfg, self.conn, self.media_dir, {}
                )

        state = self.conn.execute("""SELECT backfill_in_progress, backfill_cursor
            FROM telegram_channel_state WHERE channel_id = %s""", (chan_id,)).fetchone()
        self.assertEqual(state, (1, 75))

    def test_backfill_advances_cursor_when_hidden_duplicates_are_intentionally_skipped(self):
        chan_id = "-1000000000001"
        self.conn.execute("""INSERT INTO telegram_channel_state
            (channel_id, last_min_duration, last_tags_whitelist, last_tags_blacklist,
             backfill_in_progress, backfill_cursor, updated_at)
            VALUES (%s, 420, '[]', '[]', 1, 75, %s)""", (chan_id, int(time.time())))
        hidden_id = f"telegram:{chan_id}:50:u50"
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_decisions "
                "(loser_id,keeper_id,operation_id,state,created_at) "
                "VALUES (%s,'keeper','hidden-op','active',%s)",
                (hidden_id, int(time.time())),
            )
        cfg = TelegramChannelConfig(id=chan_id, min_duration_seconds=420,
                                    max_pages=1, history_batch_size=2)
        batch = {"videos": [
            {"provider_video_id": f"{chan_id}:60:u60", "title": "Visible", "duration": 500, "categories": []},
            {"provider_video_id": f"{chan_id}:50:u50", "title": "Hidden duplicate", "duration": 500, "categories": []},
        ], "message_count": 2, "next_offset_id": 50, "has_more": True}

        def fetch(url, **kwargs):
            return batch if "history/" in url else []

        with patch("sync.fetch_json_with_retry", side_effect=fetch):
            sync_module.sync_telegram_channel_pipeline(
                chan_id, cfg, self.conn, self.media_dir, {}
            )

        state = self.conn.execute(
            "SELECT backfill_in_progress,backfill_cursor FROM telegram_channel_state WHERE channel_id=%s",
            (chan_id,),
        ).fetchone()
        self.assertEqual(state, (1, 50))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos WHERE id=%s", (f"telegram:{chan_id}:60:u60",)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos WHERE id=%s", (hidden_id,)).fetchone()[0], 0)

    def test_new_materials_mode_does_not_advance_or_reset_history_cursor(self):
        chan_id = "-1000000000001"
        self.conn.execute("""INSERT INTO telegram_channel_state
            (channel_id,last_min_duration,last_tags_whitelist,last_tags_blacklist,
             backfill_in_progress,backfill_cursor,updated_at)
            VALUES (%s,420,'[]','[]',1,75,%s)""", (chan_id, int(time.time())))
        cfg = TelegramChannelConfig(id=chan_id, max_pages=1, history_batch_size=2)
        urls = []
        with patch("sync.fetch_json_with_retry", side_effect=lambda url, **kwargs: urls.append(url) or []):
            sync_module.sync_telegram_channel_pipeline(
                chan_id, cfg, self.conn, self.media_dir, {}, sync_mode="new"
            )
        self.assertEqual(len(urls), 1)
        self.assertIn("category/", urls[0])
        state = self.conn.execute(
            "SELECT backfill_in_progress,backfill_cursor FROM telegram_channel_state WHERE channel_id=%s",
            (chan_id,),
        ).fetchone()
        self.assertEqual(state, (1, 75))

    def test_backfill_mode_resumes_cursor_without_fetching_latest(self):
        chan_id = "-1000000000001"
        self.conn.execute("""INSERT INTO telegram_channel_state
            (channel_id,last_min_duration,last_tags_whitelist,last_tags_blacklist,
             backfill_in_progress,backfill_cursor,updated_at)
            VALUES (%s,420,'[]','[]',1,75,%s)""", (chan_id, int(time.time())))
        cfg = TelegramChannelConfig(id=chan_id, max_pages=5, history_batch_size=2)
        urls = []

        def fetch(url, **kwargs):
            urls.append(url)
            return {"videos": [], "message_count": 2, "next_offset_id": 50, "has_more": True}

        with patch("sync.fetch_json_with_retry", side_effect=fetch):
            sync_module.sync_telegram_channel_pipeline(
                chan_id, cfg, self.conn, self.media_dir, {}, sync_mode="backfill"
            )
        self.assertEqual(len(urls), 1)
        self.assertIn("history/", urls[0])
        self.assertIn("offset_id=75", urls[0])
        state = self.conn.execute(
            "SELECT backfill_in_progress,backfill_cursor FROM telegram_channel_state WHERE channel_id=%s",
            (chan_id,),
        ).fetchone()
        self.assertEqual(state, (1, 50))

    def test_backfill_mode_starts_from_the_beginning_when_idle(self):
        chan_id = "-1000000000001"
        self.conn.execute("""INSERT INTO telegram_channel_state
            (channel_id,last_min_duration,last_tags_whitelist,last_tags_blacklist,
             backfill_in_progress,backfill_cursor,updated_at)
            VALUES (%s,420,'[]','[]',0,75,%s)""", (chan_id, int(time.time())))
        cfg = TelegramChannelConfig(id=chan_id, history_batch_size=2)
        urls = []
        with patch("sync.fetch_json_with_retry", side_effect=lambda url, **kwargs:
                   urls.append(url) or {"videos": [], "message_count": 0,
                                        "next_offset_id": None, "has_more": False}):
            sync_module.sync_telegram_channel_pipeline(
                chan_id, cfg, self.conn, self.media_dir, {}, sync_mode="backfill"
            )
        self.assertEqual(len(urls), 1)
        self.assertIn("history/", urls[0])
        self.assertNotIn("offset_id=", urls[0])
        state = self.conn.execute(
            "SELECT backfill_in_progress,backfill_cursor FROM telegram_channel_state WHERE channel_id=%s",
            (chan_id,),
        ).fetchone()
        self.assertEqual(state, (0, None))

class TestProviderConfig(unittest.TestCase):
    def test_provider_set_address(self):
        """Task 3: Changing provider address updates BASE_URL."""
        try:
            from providers import get_provider
        except ImportError as exc:
            from test_support import optional_import_failed
            optional_import_failed(exc)
            self.skipTest("providers module only available in resolver environment")
        prov = get_provider("telegram")
        old = prov.BASE_URL
        try:
            prov.set_address("https://example.com/tg")
            self.assertEqual(prov.BASE_URL, "https://example.com/tg")
        finally:
            # Restore
            prov.set_address(old)


if __name__ == "__main__":
    unittest.main()
