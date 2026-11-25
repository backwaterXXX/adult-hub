from pathlib import Path
from pg_helper import PgTestCase, TEST_URL

import tempfile
import unittest
from unittest.mock import patch
from test_support import optional_import_failed
from tests.helpers.db_session import db_session

try:
    import resolver.admin as admin
    ADMIN_AVAILABLE = True
except ImportError as exc:
    optional_import_failed(exc)
    ADMIN_AVAILABLE = False


@unittest.skipUnless(ADMIN_AVAILABLE, "FastAPI / admin module not available")
class TestAdminDbStats(PgTestCase):

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.cfg_path = Path(self.tmpdir.name) / "test_config.yaml"

        # Create dummy config with telegram channels
        self.cfg_path.write_text(
            """
sources:
  - provider: telegram
    channels:
      - id: "-1001111111111"
        studio: "Channel Alpha"
      - id: "-1002222222222"
        studio: "Channel Beta"
"""
        )

    def _insert_videos(self, rows):
        with db_session(TEST_URL) as conn:
            conn.executemany(
                "INSERT INTO videos (id, provider, studio, last_seen) VALUES (%s, %s, %s, %s)",
                rows,
            )

    def tearDown(self):
        pass

    def test_load_telegram_channel_names(self):
        with patch.object(admin, "CONFIG_PATH", self.cfg_path):
            ch_map = admin.admin_data.channel_labels(admin.CONFIG_PATH)
            self.assertEqual(ch_map.get("-1001111111111"), "Channel Alpha")
            self.assertEqual(ch_map.get("-1002222222222"), "Channel Beta")

    def test_db_stats_telegram_channels_breakdown(self):
        # 3 in Alpha, 1 in Beta, 2 in secondweb, 1 in telegram with empty studio but valid ID
        rows = [
            ("telegram:-1001111111111:1:a", "telegram", "Channel Alpha", 100),
            ("telegram:-1001111111111:2:b", "telegram", "Channel Alpha", 100),
            ("telegram:-1001111111111:3:c", "telegram", "Channel Alpha", 100),
            ("telegram:-1002222222222:1:d", "telegram", "Channel Beta", 100),
            ("telegram:-1002222222222:2:e", "telegram", "", 100),  # fallback to id -> mapped to Channel Beta
            ("secondweb:1", "secondweb", "", 100),
            ("secondweb:2", "secondweb", "", 100),
        ]
        self._insert_videos(rows)

        with patch.object(admin, "HUB_DB_URL", TEST_URL), \
             patch.object(admin, "CONFIG_PATH", self.cfg_path):
            stats = admin._db_stats()
            self.assertTrue(stats["db_ok"])
            self.assertEqual(stats["videos_total"], 7)
            self.assertEqual(stats["telegram_total"], 5)
            self.assertEqual(stats["telegram_channels"], {
                "Channel Alpha": 3,
                "Channel Beta": 2,
            })
            by_prov = stats["by_provider"]
            self.assertEqual(by_prov.get("tg:[Channel Alpha]"), 3)
            self.assertEqual(by_prov.get("tg:[Channel Beta]"), 2)
            self.assertEqual(by_prov.get("secondweb"), 2)
            # Order should be descending
            keys = list(by_prov.keys())
            self.assertEqual(keys[0], "tg:[Channel Alpha]")

    def test_db_stats_no_telegram(self):
        self._insert_videos([('og:1', 'secondweb', '', 100)])
        with patch.object(admin, "HUB_DB_URL", TEST_URL), \
             patch.object(admin, "CONFIG_PATH", self.cfg_path):
            stats = admin._db_stats()
            self.assertTrue(stats["db_ok"])
            self.assertEqual(stats["videos_total"], 1)
            self.assertIsNone(stats["telegram_total"])
            self.assertEqual(stats["by_provider"], {"secondweb": 1})

    def test_trim_channel_name(self):
        trim = admin.admin_data.trim_channel_name
        self.assertEqual(trim("Short Name", 25), "Short Name")
        long_name = "• NakedSword Studios Media Online ▸ Access ®"
        trimmed = trim(long_name, 25)
        self.assertLessEqual(len(trimmed), 25)
        self.assertTrue(trimmed.endswith("…"))
        self.assertEqual(trimmed, "• NakedSword Studios Med…")

    def test_db_stats_truncates_long_channel_names(self):
        long_ch = "• NakedSword Studios Media Online ▸ Access ®"
        # Channel statistics are keyed by the stable ID embedded in the video
        # identity, so provide a matching channel label in the fixture config.
        self.cfg_path.write_text(
            "sources:\n"
            "  - provider: telegram\n"
            "    channels:\n"
            "      - id: \"1\"\n"
            f"        studio: \"{long_ch}\"\n",
            encoding="utf-8",
        )
        self._insert_videos([('telegram:1', 'telegram', long_ch, 100)])
        with patch.object(admin, "HUB_DB_URL", TEST_URL), \
             patch.object(admin, "CONFIG_PATH", self.cfg_path):
            stats = admin._db_stats()
            self.assertIn("tg:[• NakedSword Studios Med…]", stats["by_provider"])
            self.assertIn(long_ch, stats["telegram_channels"])


@unittest.skipUnless(ADMIN_AVAILABLE, "FastAPI / admin module not available")
class TestAdminOperations(unittest.TestCase):
    def test_backup_status_reports_latest_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            backup_dir = Path(directory)
            backup = backup_dir / "adult_hub-test.db"
            backup.write_bytes(b"sqlite snapshot")
            with patch.object(admin, "BACKUP_DIR", backup_dir):
                status = admin._backup_status()
        self.assertTrue(status["available"])
        self.assertEqual(status["count"], 1)
        self.assertEqual(status["latest"]["name"], backup.name)


if __name__ == "__main__":
    unittest.main()
