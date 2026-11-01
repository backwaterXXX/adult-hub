import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase, TEST_URL, reset_db

import tempfile
import unittest
from unittest.mock import patch
from test_support import optional_import_failed

try:
    sys.path.insert(0, str(Path(__file__).parent.parent / "resolver"))
    import admin
    from common import db
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
        conn = db.connect(TEST_URL)
        with conn:
            conn.executemany(
                "INSERT INTO videos (id, provider, studio, last_seen) VALUES (%s, %s, %s, %s)",
                rows,
            )
        conn.close()

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


@unittest.skipUnless(ADMIN_AVAILABLE, "FastAPI / admin module not available")
class TestManagedCollections(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from pg_helper import init_test_db
        init_test_db()

    def setUp(self):
        reset_db()
    class Response:
        def __init__(self, data=None):
            self.data = data or {}
        def raise_for_status(self):
            return None
        def json(self):
            return self.data

    class Client:
        def __init__(self):
            self.created = []
        async def get(self, url, params=None, headers=None):
            if params.get("includeItemTypes") == "Movie,Video":
                return TestManagedCollections.Response({
                    "Items": [
                        {"Id": f"jf-{i}", "Path": f"/media/adult/file-{i}.strm"}
                        for i in range(3)
                    ], "TotalRecordCount": 3,
                })
            return TestManagedCollections.Response({"Items": [], "TotalRecordCount": 0})
        async def post(self, url, params=None, headers=None):
            self.created.append((url, params))
            return TestManagedCollections.Response({"Id": "collection"})
        async def delete(self, url, params=None, headers=None):
            return TestManagedCollections.Response()

    async def test_creates_newest_studio_performer_and_tag_collections(self):
        conn = db.connect(TEST_URL)
        with conn:
            for index in range(3):
                conn.execute(
                    "INSERT INTO videos (id, title, studio, performers, tags, first_seen, duration) "
                    "VALUES (%s, %s, %s, %s, %s, %s, 600)",
                    (f"telegram:{index}", f"Title {index}", "Studio", '["Performer"]', '["Theme"]', 100 + index))
                conn.execute("INSERT INTO synced_files (video_id, file_path) VALUES (%s, %s)",
                             (f"telegram:{index}", f"file-{index}.strm"))
        conn.close()
        client = self.Client()
        from common.managed_collections import refresh_managed_collections
        result = await refresh_managed_collections(
            client, "http://jellyfin", "key", TEST_URL)
        self.assertEqual(result["matched_videos"], 3)
        self.assertGreaterEqual(len(client.created), 4)

    async def test_ambiguous_jellyfin_basenames_are_not_mapped_to_the_wrong_video(self):
        conn = db.connect(TEST_URL)
        with conn:
            conn.executemany(
                "INSERT INTO videos (id, title, first_seen, duration) VALUES (%s, %s, %s, 600)",
                [("telegram:shared", "Shared", 300), ("telegram:unique-a", "Unique A", 200),
                 ("telegram:unique-b", "Unique B", 100)],
            )
            conn.executemany(
                "INSERT INTO synced_files (video_id, file_path) VALUES (%s, %s)",
                [("telegram:shared", "shared.strm"), ("telegram:unique-a", "unique-a.strm"),
                 ("telegram:unique-b", "unique-b.strm")],
            )
        conn.close()

        class CollidingClient(self.Client):
            async def get(self, url, params=None, headers=None):
                if params.get("includeItemTypes") == "Movie,Video" and params.get("isFavorite"):
                    return TestManagedCollections.Response({"Items": [], "TotalRecordCount": 0})
                if params.get("includeItemTypes") == "Movie,Video":
                    return TestManagedCollections.Response({
                        "Items": [
                            {"Id": "jf-shared-a", "Path": "/media/adult/a/shared.strm"},
                            {"Id": "jf-shared-b", "Path": "/media/adult/b/shared.strm"},
                            {"Id": "jf-unique-a", "Path": "/media/adult/a/unique-a.strm"},
                            {"Id": "jf-unique-b", "Path": "/media/adult/b/unique-b.strm"},
                        ], "TotalRecordCount": 4,
                    })
                return TestManagedCollections.Response({"Items": [], "TotalRecordCount": 0})

        client = CollidingClient()
        from common.managed_collections import refresh_managed_collections
        result = await refresh_managed_collections(client, "http://jellyfin", "key", TEST_URL)

        self.assertEqual(result["matched_videos"], 2)
        recently_added = next(params for _, params in client.created
                              if params.get("name") == "Adult Hub · Recently Added")
        self.assertEqual(set(recently_added["ids"].split(",")), {"jf-unique-a", "jf-unique-b"})

    async def test_stale_managed_group_membership_is_cleared_below_group_threshold(self):
        conn = db.connect(TEST_URL)
        with conn:
            conn.execute(
                "INSERT INTO videos (id, title, studio, first_seen, duration) VALUES (%s, %s, %s, %s, 600)",
                ("telegram:one", "One", "Renamed Studio", 300),
            )
            conn.execute(
                "INSERT INTO synced_files (video_id, file_path) VALUES (%s, %s)",
                ("telegram:one", "one.strm"),
            )
        conn.close()

        class ExistingCollectionClient(self.Client):
            def __init__(self):
                super().__init__()
                self.deleted = []

            async def get(self, url, params=None, headers=None):
                if params.get("includeItemTypes") == "Movie,Video" and params.get("isFavorite"):
                    return TestManagedCollections.Response({"Items": [], "TotalRecordCount": 0})
                if params.get("includeItemTypes") == "Movie,Video":
                    return TestManagedCollections.Response({
                        "Items": [{"Id": "jf-one", "Path": "/media/adult/one.strm"}],
                        "TotalRecordCount": 1,
                    })
                if params.get("includeItemTypes") == "BoxSet":
                    return TestManagedCollections.Response({
                        "Items": [{"Id": "stale-studio", "Name": "Adult Hub · Studio · Old Studio"}],
                        "TotalRecordCount": 1,
                    })
                if params.get("parentId") == "stale-studio":
                    return TestManagedCollections.Response({
                        "Items": [{"Id": "jf-old", "Path": "/media/adult/old.strm"}],
                        "TotalRecordCount": 1,
                    })
                return TestManagedCollections.Response({"Items": [], "TotalRecordCount": 0})

            async def delete(self, url, params=None, headers=None):
                self.deleted.append((url, params))
                return TestManagedCollections.Response()

        client = ExistingCollectionClient()
        from common.managed_collections import refresh_managed_collections
        result = await refresh_managed_collections(client, "http://jellyfin", "key", TEST_URL)

        self.assertEqual(client.deleted, [
            ("http://jellyfin/Collections/stale-studio/Items", {"ids": "jf-old"}),
        ])
        self.assertIn(
            {"name": "Adult Hub · Studio · Old Studio", "items": 0, "created": False},
            result["collections"],
        )

if __name__ == "__main__":
    unittest.main()
