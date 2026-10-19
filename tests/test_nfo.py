import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase

sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


try:
    import sync as sync_module
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



class TestBackfillNegativeCache(PgTestCase):
    """Puste lookupy nie blokuja kolejki (fix #1)."""

    def setUp(self):
        super().setUp()
        if not SYNC_AVAILABLE:
            self.skipTest("sync module not available")

    def _db(self):
        self.conn.execute(
            "INSERT INTO videos (id, provider, title, slug, duration, performers,"
            " categories, tags, first_seen, last_seen)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("exampleweb:1", "exampleweb", "T", "s", 600, "", "[]", "[]", 1, 100))
        self.conn.commit()
        return self.conn

    def test_empty_result_skipped_next_run(self):
        orig_fetch = sync_module.fetch_json_with_retry
        orig_sleep = sync_module.time.sleep
        orig_limit = sync_module.PERFORMER_BACKFILL_PER_RUN
        try:
            sync_module.PERFORMER_BACKFILL_PER_RUN = 10
            sync_module.time.sleep = lambda *a: None
            sync_module.fetch_json_with_retry = lambda url, **kw: {"performers": []}
            with tempfile.TemporaryDirectory() as d:
                conn = self._db()
                conn.execute("UPDATE videos SET title = %s WHERE id = %s",
                             ("Jane Doe & Jack Smith - Scene", "exampleweb:1"))
                conn.commit()
                checked, updated = sync_module.backfill_performers(
                    conn, Path(d), providers=("exampleweb",))
                self.assertEqual((checked, updated), (1, 0))
                self.assertEqual(conn.execute(
                    "SELECT performers FROM videos WHERE id = %s",
                    ("exampleweb:1",)).fetchone()[0], "[]")
                checked2, _ = sync_module.backfill_performers(
                    conn, Path(d), providers=("exampleweb",))
                self.assertEqual(checked2, 0)
        finally:
            sync_module.fetch_json_with_retry = orig_fetch
            sync_module.time.sleep = orig_sleep
            sync_module.PERFORMER_BACKFILL_PER_RUN = orig_limit

    def test_failed_page_lookup_does_not_store_title_guess(self):
        with tempfile.TemporaryDirectory() as d:
            conn = self._db()
            conn.execute("UPDATE videos SET title = %s WHERE id = %s",
                         ("Jane Doe & Jack Smith - Scene", "exampleweb:1"))
            conn.commit()
            with patch.object(sync_module, "fetch_json_with_retry", side_effect=OSError("temporary")), \
                 patch.object(sync_module.time, "sleep", return_value=None):
                checked, updated = sync_module.backfill_performers(
                    conn, Path(d), providers=("exampleweb",))
            self.assertEqual((checked, updated), (1, 0))
            row = conn.execute("SELECT performers, performers_checked_at FROM videos WHERE id = %s",
                               ("exampleweb:1",)).fetchone()
            self.assertEqual(row[0], "[]")
            self.assertLessEqual(abs(row[1] - (int(time.time()) - 29 * 86400)), 2)
            with patch.object(sync_module, "fetch_json_with_retry", side_effect=AssertionError("too soon")):
                self.assertEqual(sync_module.backfill_performers(
                    conn, Path(d), providers=("exampleweb",)), (0, 0))
            conn.execute("UPDATE videos SET performers_checked_at = %s WHERE id = %s",
                         (int(time.time()) - 30 * 86400 - 1, "exampleweb:1"))
            conn.commit()
            with patch.object(sync_module, "fetch_json_with_retry", return_value={"performers": []}), \
                 patch.object(sync_module.time, "sleep", return_value=None):
                self.assertEqual(sync_module.backfill_performers(
                    conn, Path(d), providers=("exampleweb",)), (1, 0))
            empty_marker = conn.execute("SELECT performers_checked_at FROM videos WHERE id = %s",
                                        ("exampleweb:1",)).fetchone()[0]
            self.assertLessEqual(abs(empty_marker - int(time.time())), 2)

    def test_page_cast_wins_over_title_for_lookup_provider(self):
        with tempfile.TemporaryDirectory() as d:
            media = Path(d) / "media"
            media.mkdir()
            conn = self._db()
            conn.execute("UPDATE videos SET title = %s WHERE id = %s",
                         ("Jane Doe & Jack Smith - Scene", "exampleweb:1"))
            conn.commit()
            with patch.object(sync_module, "performer_backfill_providers", return_value=("exampleweb",)), \
                 patch.object(sync_module, "fetch_json_with_retry", return_value={"performers": ["Real Actor"]}), \
                 patch.object(sync_module.time, "sleep", return_value=None):
                self.assertEqual(sync_module.backfill_performers_from_titles(conn, media), 0)
                checked, updated = sync_module.backfill_performers(
                    conn, media, providers=("exampleweb",))
            self.assertEqual((checked, updated), (1, 1))
            names = json.loads(conn.execute("SELECT performers FROM videos WHERE id = %s",
                                            ("exampleweb:1",)).fetchone()[0])
            self.assertEqual(names, ["Real Actor"])




class TestPerformerAudit(PgTestCase):
    """Podejrzane obsady weryfikowane strona filmu (audyt)."""

    def setUp(self):
        super().setUp()
        if not SYNC_AVAILABLE:
            self.skipTest("sync module not available")

    def _db(self):
        self.conn.execute(
            "INSERT INTO videos (id, provider, title, slug, duration, performers,"
            " categories, tags, first_seen, last_seen)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("exampleweb:9/x", "exampleweb", "T", "x", 600,
             json.dumps(["A B", "C D", "E F", "G H", "I J"]),
             "[]", "[]", 1, 2))
        self.conn.commit()
        return self.conn

    def test_corrects_from_page(self):
        orig_fetch = sync_module.fetch_json_with_retry
        orig_sleep = sync_module.time.sleep
        try:
            sync_module.time.sleep = lambda *a: None
            sync_module.fetch_json_with_retry = lambda url, **kw: {"performers": ["A B"]}
            with tempfile.TemporaryDirectory() as d:
                media = Path(d) / "media"
                media.mkdir()
                conn = self._db()
                checked, corrected = sync_module.audit_suspicious_performers(
                    conn, media, threshold=5, limit=10, providers=("exampleweb",))
                self.assertEqual((checked, corrected), (1, 1))
                row = conn.execute(
                    "SELECT performers FROM videos WHERE id='exampleweb:9/x'").fetchone()
                self.assertEqual(json.loads(row[0]), ["A B"])
                nfo = next(media.glob("*.nfo")).read_text(encoding="utf-8")
                self.assertIn("<name>A B</name>", nfo)
                self.assertNotIn("<name>C D</name>", nfo)
        finally:
            sync_module.fetch_json_with_retry = orig_fetch
            sync_module.time.sleep = orig_sleep

    def test_below_threshold_skipped(self):
        orig_fetch = sync_module.fetch_json_with_retry
        try:
            calls = []
            sync_module.fetch_json_with_retry = lambda url, **kw: calls.append(url) or {"performers": []}
            with tempfile.TemporaryDirectory() as d:
                conn = self.conn
                conn.execute(
                    "INSERT INTO videos (id, provider, title, slug, duration, performers,"
                    " categories, tags, first_seen, last_seen)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    ("exampleweb:1/x", "exampleweb", "T", "x", 600,
                     json.dumps(["A B"]), "[]", "[]", 1, 2))
                conn.commit()
                checked, corrected = sync_module.audit_suspicious_performers(
                    conn, Path(d), threshold=5, limit=10)
                self.assertEqual((checked, corrected), (0, 0))
                self.assertEqual(calls, [])
        finally:
            sync_module.fetch_json_with_retry = orig_fetch

    def test_rotates_when_limit_is_smaller_than_suspect_count(self):
        with tempfile.TemporaryDirectory() as d:
            conn = self.conn
            for suffix in ("1", "2", "3"):
                conn.execute(
                    "INSERT INTO videos (id, provider, title, slug, duration, performers, categories, tags) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (f"exampleweb:{suffix}", "exampleweb", "T", suffix, 600,
                     json.dumps(["A B", "C D"]), "[]", "[]"))
            conn.commit()
            visited = []
            def fetch(url, **kwargs):
                visited.append(url.split("pid=", 1)[1].split("&", 1)[0])
                return {"performers": ["A B", "C D"]}
            with patch.object(sync_module, "fetch_json_with_retry", side_effect=fetch), \
                 patch.object(sync_module.time, "sleep", return_value=None):
                for _ in range(3):
                    self.assertEqual(sync_module.audit_suspicious_performers(
                        conn, Path(d), threshold=2, limit=1,
                        providers=("exampleweb",)), (1, 0))
            self.assertEqual(visited, ["1", "2", "3"])


if __name__ == "__main__":
    unittest.main()
