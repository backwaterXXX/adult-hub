import json
import tempfile
import unittest
from pathlib import Path

from pg_helper import PgTestCase



try:
    from test_support import load_sync_module
    sync_module = load_sync_module()
    SYNC_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    SYNC_AVAILABLE = False


def _video(**kw):
    base = {
        "provider": "telegram",
        "provider_video_id": "-1001:42:UNIQ",
        "title": "Test Title",
        "slug": "UNIQ",
        "url": "https://t.me/c/1/42",
        "thumbnail": "",
        "duration": 900,
        "performers": [],
    }
    base.update(kw)
    return base


class TestNfoStudioDateadded(unittest.TestCase):
    def setUp(self):
        if not SYNC_AVAILABLE:
            self.skipTest("sync module not available")

    def _write(self, video, genres=None, tags=None):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.nfo"
            sync_module.create_nfo_file(video, genres or set(), tags or set(), p)
            return p.read_text(encoding="utf-8")

    def test_studio_variant_1(self):
        xml = self._write(_video(production_studios=["MenAtPlay"], studio="Channel Name"))
        self.assertIn("<studio>MenAtPlay</studio>", xml)
        self.assertIn("<tag>Kanał: Channel Name</tag>", xml)
        self.assertNotIn("<studio>Channel Name</studio>", xml)
        self.assertNotIn("Telegram (t.me)", xml)

    def test_studio_fallback_not_telegram(self):
        xml = self._write(_video())
        self.assertNotIn("<studio>Telegram</studio>", xml)
        self.assertNotIn("<tag>Telegram</tag>", xml)
        self.assertNotIn("<tag>t.me</tag>", xml)

    def test_dateadded_present(self):
        xml = self._write(_video(dateadded="2026-09-26 10:00:00"))
        self.assertIn("<dateadded>2026-09-26 10:00:00</dateadded>", xml)

    def test_dateadded_absent_when_empty(self):
        xml = self._write(_video())
        self.assertNotIn("<dateadded>", xml)

    def test_format_dateadded(self):
        self.assertRegex(sync_module.format_dateadded(0), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(sync_module.format_dateadded("nonsense"), "")

    def test_performers_also_tags(self):
        xml = self._write(_video(performers=["Rico Marlon"]), tags={"Telegram"})
        self.assertIn("<tag>Rico Marlon</tag>", xml)

    def test_performers_as_collections(self):
        xml = self._write(_video(performers=["Rico Marlon", "Andres Milan"]))
        # Format Kodi <set><name> — sam tekst w <set> Jellyfin ignoruje.
        self.assertIn("<name>Rico Marlon</name>", xml)
        self.assertIn("<name>Andres Milan</name>", xml)
        self.assertIn("<set>", xml)

    def test_no_sets_without_performers(self):
        xml = self._write(_video())
        self.assertNotIn("<set>", xml)

    def test_nfo_actor_profiles(self):
        video = _video(
            performers=["Rico Marlon"],
            performer_links={
                "rico marlon": {
                    "stashdb": "https://stashdb.org/performers/1111",
                    "pbc": "https://pbc.xxx/wiki/Rico_Marlon",
                }
            },
        )
        xml = self._write(video)
        self.assertIn("<profile>https://stashdb.org/performers/1111</profile>", xml)
        self.assertIn("<profile>https://pbc.xxx/wiki/Rico_Marlon</profile>", xml)

    def test_nfo_scene_stashdb_and_website(self):
        video = _video(
            scene_match_source="stashdb",
            scene_match_external_id="scene-uuid-1234",
            scene_match_url="https://stashdb.org/scenes/scene-uuid-1234",
        )
        xml = self._write(video)
        self.assertIn('<uniqueid type="stashdb" default="false">scene-uuid-1234</uniqueid>', xml)
        self.assertIn("<website>https://stashdb.org/scenes/scene-uuid-1234</website>", xml)


class TestNfoDbPerformers(PgTestCase):
    """NFO nie traci obsady, gdy swiezy listing jest pusty (fix #3)."""

    def setUp(self):
        super().setUp()
        if not SYNC_AVAILABLE:
            self.skipTest("sync module not available")

    def test_empty_listing_keeps_db_cast(self):
        with tempfile.TemporaryDirectory() as d:
            media = Path(d) / "media"
            media.mkdir()
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, slug, duration, performers,"
                " categories, tags, first_seen, last_seen)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                ("exampleweb:abc", "exampleweb", "T", "s", 600,
                 json.dumps(["Jane Doe"]), "[]", "[]", 1000, 2000))
            self.conn.commit()
            sync_module.sync_unique_video(
                {"provider": "exampleweb", "provider_video_id": "abc",
                 "title": "T", "slug": "s", "duration": 600, "performers": []},
                set(), set(), media, self.conn)
            self.conn.commit()
            nfo = next(media.glob("*.nfo")).read_text(encoding="utf-8")
            self.assertIn("<name>Jane Doe</name>", nfo)
            row = self.conn.execute(
                "SELECT performers FROM videos WHERE id='exampleweb:abc'").fetchone()
            self.assertEqual(json.loads(row[0]), ["Jane Doe"])

    def test_nfo_loads_performer_profiles_from_db(self):
        self.conn.execute(
            "INSERT INTO performer_bios (name, bio, updated_at, stashdb_url, pbc_url) VALUES (%s, %s, %s, %s, %s)",
            ("Jane Doe", "bio", 100, "https://stashdb.org/performers/jane", "https://pbc.xxx/wiki/Jane_Doe"),
        )
        self.conn.commit()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "test.nfo"
            sync_module.create_nfo_file(_video(performers=["Jane Doe"]), set(), set(), p, conn=self.conn)
            xml = p.read_text(encoding="utf-8")
            self.assertIn("<profile>https://stashdb.org/performers/jane</profile>", xml)
            self.assertIn("<profile>https://pbc.xxx/wiki/Jane_Doe</profile>", xml)

    def test_fresh_cast_wins(self):
        with tempfile.TemporaryDirectory() as d:
            media = Path(d) / "media"
            media.mkdir()
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, slug, duration, performers,"
                " categories, tags, first_seen, last_seen)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                ("exampleweb:abc", "exampleweb", "T", "s", 600,
                 json.dumps(["Old Name"]), "[]", "[]", 1000, 2000))
            self.conn.commit()
            sync_module.sync_unique_video(
                {"provider": "exampleweb", "provider_video_id": "abc",
                 "title": "T", "slug": "s", "duration": 600, "performers": ["New Name"]},
                set(), set(), media, self.conn)
            self.conn.commit()
            nfo = next(media.glob("*.nfo")).read_text(encoding="utf-8")
            self.assertIn("<name>New Name</name>", nfo)
            self.assertNotIn("<name>Old Name</name>", nfo)


if __name__ == "__main__":
    unittest.main()
