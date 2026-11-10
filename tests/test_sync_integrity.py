import json
import os
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from pg_helper import PgTestCase

try:
    from test_support import load_sync_module
    sync_module = load_sync_module()
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    sync_module = None


class SyncIntegrityTests(PgTestCase):
    def setUp(self):
        super().setUp()
        if sync_module is None:
            self.skipTest("sync module not available in this environment")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.media = self.root / "media"
        self.media.mkdir()


    def video(self, provider, video_id, title):
        return {
            "provider": provider,
            "provider_video_id": video_id,
            "title": title,
            "duration": 600,
            "performers": [],
        }

    def test_source_snapshot_and_sync_history_file(self):
        sync_module.sync_unique_video(self.video("exampleweb", "one", "First"), set(), set(), self.media, self.conn)
        before = sync_module._video_id_snapshot(self.conn)
        self.assertEqual(before["exampleweb"], {"exampleweb:one"})
        history = self.root / "cache" / "sync_history.json"
        with patch.dict(os.environ, {"SYNC_HISTORY_FILE": str(history)}):
            sync_module._write_sync_history({"success": True, "sources": []})
        self.assertTrue(history.exists())
        self.assertTrue(json.loads(history.read_text())["runs"][0]["success"])

    def test_duration_parser_matches_resolver_and_shared_helper(self):
        from common.duration import parse_duration_seconds

        for value, expected in (("01:23:45", 5025), ("12:34", 754), ("45", 45),
                                ("10 min 25 sec", 625), ("invalid", 0), (None, 0)):
            with self.subTest(value=value):
                self.assertEqual(sync_module.parse_duration_to_seconds(value), expected)
                self.assertEqual(parse_duration_seconds(value), expected)

    def test_titles_and_providers_do_not_collide(self):
        sync_module.sync_unique_video(self.video("exampleweb", "123", "Old title"), set(), set(), self.media, self.conn)
        sync_module.sync_unique_video(self.video("exampleweb", "123", "New title"), set(), set(), self.media, self.conn)
        sync_module.sync_unique_video(self.video("thirdweb", "123", "New title"), set(), set(), self.media, self.conn)

        files = list(self.media.glob("*.strm"))
        self.assertEqual(len(files), 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM synced_files").fetchone()[0], 2)
        self.assertEqual({p.read_text().split("/admin/play/")[1].split("/stream")[0] for p in files},
                         {"exampleweb:123", "thirdweb:123"})

    def test_stream_url_is_refreshed_when_public_address_changes(self):
        old_url = sync_module.RESOLVER_PUBLIC_URL
        self.addCleanup(setattr, sync_module, "RESOLVER_PUBLIC_URL", old_url)
        video = self.video("exampleweb", "123", "Title")
        sync_module.RESOLVER_PUBLIC_URL = "http://old-host:8090"
        sync_module.sync_unique_video(video, set(), set(), self.media, self.conn)
        sync_module.RESOLVER_PUBLIC_URL = "http://new-host:8090"
        sync_module.sync_unique_video(video, set(), set(), self.media, self.conn)
        path = next(self.media.glob("*.strm"))
        self.assertEqual(path.read_text(), "http://new-host:8090/admin/play/exampleweb:123/stream\n")

    def test_telegram_public_url_keeps_proxy_and_other_providers_use_fallback(self):
        with patch.object(sync_module, "RESOLVER_PUBLIC_URL", "http://lan:8090"), \
             patch.object(sync_module, "STREAM_PUBLIC_URL_TELEGRAM", "https://media.example.com"):
            telegram = self.media / "telegram.strm"
            web = self.media / "web.strm"
            sync_module.write_stream_file(telegram, "telegram:123")
            sync_module.write_stream_file(web, "exampleweb:123")
            self.assertEqual(telegram.read_text(),
                             "https://media.example.com/admin/play/telegram:123/stream?mode=proxy\n")
            self.assertEqual(web.read_text(), "http://lan:8090/admin/play/exampleweb:123/stream\n")

    def test_shared_https_origin_is_used_for_remote_playback(self):
        with patch.object(sync_module, "RESOLVER_PUBLIC_URL", "https://adult.example.test"), \
             patch.object(sync_module, "STREAM_PUBLIC_URL_TELEGRAM", ""):
            telegram = self.media / "telegram.strm"
            web = self.media / "web.strm"
            sync_module.write_stream_file(telegram, "telegram:123")
            sync_module.write_stream_file(web, "exampleweb:123")
            self.assertEqual(telegram.read_text(),
                             "https://adult.example.test/admin/play/telegram:123/stream?mode=proxy\n")
            self.assertEqual(web.read_text(),
                             "https://adult.example.test/admin/play/exampleweb:123/stream\n")

    def test_telegram_thumbnail_is_local_sidecar_without_remote_nfo_dependency(self):
        thumb_path = "/admin/api/thumb?provider=telegram&ref=example%3A42%3Au1"
        video = self.video("telegram", "example:42:u1", "Example")
        video["thumbnail"] = thumb_path
        with patch.object(sync_module, "RESOLVER_PUBLIC_URL", "https://adult.example.test"), \
             patch.object(sync_module, "RESOLVER_API_URL", "http://resolver:8090"), \
             patch.object(sync_module, "http_get", return_value=b"\xff\xd8" + b"x" * 640 + b"\xff\xd9") as get:
            with self.assertNoLogs(sync_module.logger, level="WARNING"):
                sync_module.sync_unique_video(video, set(), set(), self.media, self.conn)

        get.assert_called_once_with("http://resolver:8090" + thumb_path, timeout=15.0)
        stored_url = self.conn.execute("SELECT thumbnail FROM videos WHERE id = %s",
                                       ("telegram:example:42:u1",)).fetchone()[0]
        self.assertEqual(stored_url, "https://adult.example.test" + thumb_path)
        jpg = next(self.media.glob("*.jpg"))
        self.assertEqual(jpg.stat().st_size, 644)
        nfo_root = ET.parse(next(self.media.glob("*.nfo"))).getroot()
        self.assertIsNone(nfo_root.find("thumb"))
        self.assertEqual(nfo_root.findtext("lockdata"), "true")
        self.assertEqual(jpg.stem, next(self.media.glob("*.strm")).stem)

        with patch.object(sync_module, "RESOLVER_PUBLIC_URL", "https://adult.example.test"), \
             patch.object(sync_module, "RESOLVER_API_URL", "http://resolver:8090"):
            self.assertEqual(sync_module.thumbnail_urls(stored_url),
                             (stored_url, "http://resolver:8090" + thumb_path))
            self.assertEqual(sync_module.thumbnail_urls("http://192.0.2.1:8090" + thumb_path),
                             (stored_url, "http://resolver:8090" + thumb_path))
            self.assertEqual(sync_module.thumbnail_urls("https://cdn.example.test/poster.jpg"),
                             ("https://cdn.example.test/poster.jpg", "https://cdn.example.test/poster.jpg"))

    def test_metadata_is_ready_before_new_stream_is_visible(self):
        video = self.video("telegram", "example:43:u1", "Example")
        video["thumbnail"] = "/admin/api/thumb?provider=telegram&ref=example%3A43%3Au1"

        def download(url, dest):
            self.assertEqual(list(self.media.glob("*.strm")), [])
            self.assertEqual(len(list(self.media.glob("*.nfo"))), 1)
            dest.write_bytes(b"local preview")

        with patch.object(sync_module, "download_image", side_effect=download):
            sync_module.sync_unique_video(video, set(), set(), self.media, self.conn)
        self.assertEqual(len(list(self.media.glob("*.strm"))), 1)
        self.assertEqual(len(list(self.media.glob("*.jpg"))), 1)

    def test_retention_keeps_other_ids_with_matching_digits(self):
        # The guard needs a registered, enabled source plus a success signal.
        from common.config_loader import AppConfig, SourceConfig, SyncConfig
        orig_config, orig_info = sync_module.CONFIG, dict(sync_module._PROVIDER_INFO)
        sync_module.CONFIG = AppConfig(
            sync=SyncConfig(retention_days=30),
            sources={"exampleweb": SourceConfig(
                provider="exampleweb", enabled=True,
                address="https://exampleweb.example.com")})
        sync_module._PROVIDER_INFO["exampleweb"] = {
            "name": "Exampleweb", "domain": "exampleweb.example.com",
            "base_url": "https://exampleweb.example.com"}
        self.addCleanup(setattr, sync_module, "CONFIG", orig_config)
        self.addCleanup(_restore_provider_info, orig_info)
        now = int(time.time())
        for video_id, name, last_seen in (
            ("exampleweb:123", "expired [123].strm", now - 40 * 86400),
            ("exampleweb:456", "current1 [456].strm", now),
        ):
            (self.media / name).write_text("stream\n")
            self.conn.execute("INSERT INTO videos (id, title, last_seen) VALUES (%s, %s, %s)",
                              (video_id, name, last_seen))
            self.conn.execute("INSERT INTO synced_files (file_path, video_id) VALUES (%s, %s)",
                              (name, video_id))
        self.conn.commit()

        sync_module.cleanup_retention(self.conn, self.media, 30, {"exampleweb"})
        self.assertFalse((self.media / "expired [123].strm").exists())
        self.assertTrue((self.media / "current1 [456].strm").exists())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], 1)

    def test_legacy_paths_migrate_and_remove_title_duplicates(self):
        video = self.video("exampleweb", "123", "Title")
        sync_module.sync_unique_video(video, set(), set(), self.media, self.conn)
        for name in ("Old title [123].strm", "New title [123].strm"):
            (self.media / name).write_text("old stream\n")
            self.conn.execute("INSERT INTO synced_files (file_path, video_id) VALUES (%s, %s)",
                              (name, "exampleweb:123"))
        self.conn.commit()

        sync_module.migrate_synced_files(self.conn, self.media)
        self.assertEqual(len(list(self.media.glob("*.strm"))), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM synced_files").fetchone()[0], 1)

    def test_category_membership_merges_with_existing_video(self):
        existing = self.video("exampleweb", "123/slug", "Title")
        collected = {"exampleweb:123/slug": {
            "video": existing,
            "genres": set(),
            "tags": {"Popular"},
        }}
        categories = [{"id": "muscle", "title": "Muscle"}, {"id": "solo", "title": "Solo"}]
        with patch.object(sync_module, "fetch_provider_categories", return_value=categories), \
             patch.object(sync_module, "fetch_provider_category_videos", return_value=[existing]) as fetch, \
             patch.object(sync_module.time, "sleep"):
            sync_module.collect_provider_categories(collected, "exampleweb", 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected["exampleweb:123/slug"]["genres"], {"Muscle & Jocks", "Solo & Masturbation"})
        self.assertIn("Popular", collected["exampleweb:123/slug"]["tags"])
        self.assertIn("Exampleweb", collected["exampleweb:123/slug"]["tags"])

    def test_pseudo_latest_category_is_not_a_genre(self):
        # Pseudo-category skips arrive via register_category_skip.
        sync_module.register_category_skip("exampleweb", "latest")
        self.addCleanup(sync_module._CATEGORY_SKIP.discard, ("exampleweb", "latest"))
        video = self.video("exampleweb", "123", "Title")
        categories = [{"id": "latest", "title": "Latest"}, {"id": "onlyfans", "title": "OnlyFans"}]
        with patch.object(sync_module, "fetch_provider_categories", return_value=categories), \
             patch.object(sync_module, "fetch_provider_category_videos", return_value=[video]) as fetch, \
             patch.object(sync_module.time, "sleep"):
            collected = {}
            sync_module.collect_provider_categories(collected, "exampleweb", 1)
        fetch.assert_called_once_with("exampleweb", "onlyfans", 1)
        self.assertEqual(collected["exampleweb:123"]["genres"], {"Amateur & OnlyFans"})
        self.assertIn("Exampleweb", collected["exampleweb:123"]["tags"])

    def test_category_page_failure_marks_collection_incomplete(self):
        categories = [{"id": "muscle", "title": "Muscle"}]
        with patch.object(sync_module, "fetch_provider_categories", return_value=categories), \
             patch.object(sync_module, "fetch_provider_category_videos", side_effect=OSError("offline")), \
             patch.object(sync_module.time, "sleep"):
            complete = sync_module.collect_provider_categories({}, "exampleweb", 1)
        self.assertFalse(complete)

    def test_flush_collected_can_fail_when_any_video_write_fails(self):
        videos = {
            "exampleweb:one": {"video": self.video("exampleweb", "one", "One"), "genres": set(), "tags": set()},
            "exampleweb:two": {"video": self.video("exampleweb", "two", "Two"), "genres": set(), "tags": set()},
        }
        original = sync_module.sync_unique_video

        def fail_second(video, *args, **kwargs):
            if video["provider_video_id"] == "two":
                raise OSError("disk full")
            return original(video, *args, **kwargs)

        with patch.object(sync_module, "sync_unique_video", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "Catalog persistence incomplete"):
                sync_module.flush_collected(videos, self.conn, self.media, "Exampleweb", raise_on_error=True)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], 1)

    def test_category_mapping_and_provider_exclusion(self):
        from sync.category_mapping import map_category, map_categories, ALL_TARGET_CATEGORIES
        self.assertEqual(len(ALL_TARGET_CATEGORIES), 24)
        self.assertIsNone(map_category("Secondweb"))
        self.assertIsNone(map_category("Exampleweb"))
        self.assertIsNone(map_category("Fourthweb"))
        self.assertIsNone(map_category("Thirdweb"))
        self.assertIsNone(map_category("Exampleweb"))
        self.assertIsNone(map_category("Promotion"))
        self.assertIsNone(map_category("Uncategorized"))

        self.assertEqual(map_category("anal"), "Anal")
        self.assertEqual(map_category("bareback"), "Bareback & Breeding")
        self.assertEqual(map_category("creampie"), "Bareback & Breeding")
        self.assertEqual(map_category("blowjob"), "Oral & Rimming")
        self.assertEqual(map_category("rimming"), "Oral & Rimming")
        self.assertEqual(map_category("jock"), "Muscle & Jocks")
        self.assertEqual(map_category("twink"), "Twink & Young")
        self.assertEqual(map_category("threesome"), "Threesome")
        self.assertEqual(map_category("group"), "Group")
        self.assertEqual(map_category("orgy"), "Group")
        self.assertEqual(map_category("#orgy"), "Group")
        self.assertEqual(map_category("orgies"), "Group")

        mapped = map_categories(["Secondweb", "anal", "bareback", "creampie", "blowjob", "threesome", "Promotion"])
        self.assertEqual(mapped, {"Anal", "Bareback & Breeding", "Oral & Rimming", "Threesome"})
        self.assertIn("Group", map_categories(["Amateur & OnlyFans"], title="Orgy"))
        self.assertIn("Group", map_categories(["Bareback & Breeding"], title="[Lucas Raunch] - Orgy"))

    def test_migrate_categories_updates_db_and_nfo(self):
        import json
        video = self.video("secondweb", "vid1", "Test Video")
        sync_module.sync_unique_video(video, {"Secondweb", "Anal", "Creampie"}, {"Secondweb"}, self.media, self.conn)

        # Manually tamper DB and NFO to simulate old unmapped state with provider genre
        self.conn.execute("UPDATE videos SET categories = %s WHERE id = %s", (json.dumps(["Secondweb", "Anal", "Creampie"]), "secondweb:vid1"))
        self.conn.execute("INSERT INTO video_categories (video_id, category) VALUES (%s, %s) ON CONFLICT DO NOTHING", ("secondweb:vid1", "Secondweb"))
        self.conn.execute("INSERT INTO video_categories (video_id, category) VALUES (%s, %s) ON CONFLICT DO NOTHING", ("secondweb:vid1", "Creampie"))
        self.conn.commit()

        nfo_path = next(self.media.glob("*.nfo"))
        nfo_path.write_text("""<?xml version="1.0" encoding="utf-8"?>
<movie>
  <title>Test Video</title>
  <genre>Secondweb</genre>
  <genre>Creampie</genre>
  <tag>Secondweb</tag>
</movie>
""", encoding="utf-8")

        sync_module.migrate_categories(self.conn, self.media)

        # Verify DB videos table has mapped categories
        cats = json.loads(self.conn.execute("SELECT categories FROM videos WHERE id = %s", ("secondweb:vid1",)).fetchone()[0])
        self.assertEqual(cats, ["Anal", "Bareback & Breeding"])

        # Verify video_categories has no provider name and only mapped categories
        cur = self.conn.execute("SELECT category FROM video_categories WHERE video_id = %s", ("secondweb:vid1",))
        db_cats = {r[0] for r in cur.fetchall()}
        self.assertEqual(db_cats, {"Anal", "Bareback & Breeding"})

        # Verify NFO file has mapped genres and no provider genre
        new_nfo = nfo_path.read_text(encoding="utf-8")
        self.assertNotIn("<genre>Secondweb</genre>", new_nfo)
        self.assertNotIn("<genre>Creampie</genre>", new_nfo)
        self.assertIn("<genre>Bareback &amp; Breeding</genre>", new_nfo)
        self.assertIn("<tag>Secondweb</tag>", new_nfo)

    def test_flush_collected_saves_and_clears(self):
        batch = {
            "exampleweb:101": {
                "video": self.video("exampleweb", "101", "Incremental Video 1"),
                "genres": {"Anal"},
                "tags": {"Exampleweb"},
            },
            "exampleweb:102": {
                "video": self.video("exampleweb", "102", "Incremental Video 2"),
                "genres": {"Bareback"},
                "tags": {"Exampleweb"},
            },
        }
        written = sync_module.flush_collected(batch, self.conn, self.media, "Exampleweb")
        self.assertEqual(written, 2)
        self.assertEqual(len(batch), 0, "flush_collected must clear dictionary after saving")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM synced_files").fetchone()[0], 2)
        strm_files = list(self.media.glob("*.strm"))
        self.assertEqual(len(strm_files), 2)

    def test_flush_collected_rolls_back_database_batch_on_persist_failure(self):
        batch = {
            f"exampleweb:{video_id}": {
                "video": self.video("exampleweb", video_id, video_id),
                "genres": set(), "tags": set(),
            }
            for video_id in ("first", "second")
        }
        persist = sync_module._persist_prepared_video
        calls = 0

        def fail_second(conn, prepared):
            nonlocal calls
            calls += 1
            persist(conn, prepared)
            if calls == 2:
                raise OSError("simulated database write failure")

        with patch.object(sync_module, "_persist_prepared_video", side_effect=fail_second):
            written = sync_module.flush_collected(batch, self.conn, self.media, "Exampleweb")

        self.assertEqual(written, 0)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], 0)



def _restore_provider_info(orig_info):
    """Restore the provider registry snapshot (LIFO-safe)."""
    sync_module._PROVIDER_INFO.clear()
    sync_module._PROVIDER_INFO.update(orig_info)


if __name__ == "__main__":
    unittest.main()
