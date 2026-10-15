"""Performer photo cache (sync/photos.py): tables + resolver backfill, no network."""
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase

sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import db

try:
    import photos as photos_module
    PHOTOS_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    PHOTOS_AVAILABLE = False


def _name_from_url(url):
    qs = parse_qs(urlparse(url).query)
    return unquote(qs.get("name", [""])[0])


def _fake_fetch(mapping, calls=None, fail_for=()):
    def _fetch(url, timeout=15.0):
        if calls is not None:
            calls.append(url)
        name = _name_from_url(url)
        if name in fail_for:
            raise RuntimeError(f"fetch failed for {name}")
        return {"photo_url": mapping.get(name, "")}
    return _fetch


class TestPerformerPhotoTables(PgTestCase):
    def setUp(self):
        super().setUp()
        if not PHOTOS_AVAILABLE:
            self.skipTest("sync.photos module not available")

    def test_ensure_tables_creates(self):
        conn = self.conn
        photos_module.ensure_tables(conn)
        self.assertTrue(db.table_exists(conn, "performer_photos"))
        # Idempotent + writable with defaults.
        photos_module.ensure_tables(conn)
        conn.execute("INSERT INTO performer_photos (name) VALUES (%s)", ("X",))
        conn.commit()
        got = conn.execute("SELECT name, thumb FROM performer_photos WHERE name = %s", ("X",)).fetchone()
        self.assertEqual(got, ("X", ""))


class TestPerformerPhotoBackfill(PgTestCase):
    def setUp(self):
        super().setUp()
        if not PHOTOS_AVAILABLE:
            self.skipTest("sync.photos module not available")
        photos_module.ensure_tables(self.conn)

    def _seed(self, rows):
        for vid, perfs in rows:
            self.conn.execute(
                "INSERT INTO videos (id, performers) VALUES (%s, %s)", (vid, json.dumps(perfs)))
        self.conn.commit()

    def test_backfill_saves_photos_and_empty_thumb(self):
        self._seed([("exampleweb:a", ["Alice", "Bob"]), ("exampleweb:b", ["Bob", "Cid & Co"])])
        mapping = {"Alice": "https://cdn.example.com/a.jpg", "Cid & Co": "https://cdn.example.com/c.jpg"}
        calls = []
        checked, updated = photos_module.backfill_photos(
            self.conn, "http://resolver:8090", limit=25, sleep=0,
            fetch=_fake_fetch(mapping, calls), providers=("exampleweb",))
        self.assertEqual((checked, updated), (3, 2))
        rows = {r[0]: r[1] for r in
                self.conn.execute("SELECT name, thumb FROM performer_photos").fetchall()}
        self.assertEqual(rows["Alice"], "https://cdn.example.com/a.jpg")
        self.assertEqual(rows["Cid & Co"], "https://cdn.example.com/c.jpg")
        # Empty thumb still stores a row (with updated_at) so we don't re-ask.
        self.assertIn("Bob", rows)
        self.assertEqual(rows["Bob"], "")
        stamp = self.conn.execute("SELECT updated_at FROM performer_photos WHERE name='Bob'").fetchone()[0]
        self.assertIsInstance(stamp, int)
        self.assertGreater(stamp, 0)
        # Resolver URL shape: provider + quoted name; one call per name
        # for the single neutral provider. (The web order exampleweb ->
        # secondweb is covered by the private suite.)
        self.assertEqual(len(calls), 3)
        for url in calls:
            self.assertIn("/admin/api/performer-photo?provider=exampleweb&name=", url)

    def test_backfill_skips_existing_rows(self):
        self._seed([("exampleweb:a", ["Alice", "Bob"])])
        self.conn.execute(
            "INSERT INTO performer_photos (name, thumb, updated_at) VALUES (%s, %s, %s)",
            ("Alice", "https://cdn.example.com/a.jpg", 1))
        self.conn.commit()
        calls = []
        checked, updated = photos_module.backfill_photos(
            self.conn, "http://resolver:8090", limit=25, sleep=0,
            fetch=_fake_fetch({"Bob": "https://cdn.example.com/b.jpg"}, calls),
            providers=("exampleweb",))
        self.assertEqual((checked, updated), (1, 1))
        self.assertEqual([_name_from_url(u) for u in calls], ["Bob"])

    def test_backfill_limit(self):
        self._seed([("exampleweb:a", ["Ann", "Ben", "Cal", "Dan"])])
        calls = []
        checked, updated = photos_module.backfill_photos(
            self.conn, "http://resolver:8090", limit=2, sleep=0,
            fetch=_fake_fetch({}, calls), providers=("exampleweb",))
        self.assertEqual(checked, 2)
        self.assertEqual(updated, 0)
        # One call per name with the single neutral provider.
        self.assertEqual(len(calls), 2)
        # ORDER BY name: Ann, Ben first.
        self.assertEqual([_name_from_url(u) for u in calls],
                         ["Ann", "Ben"])

    def test_negative_photo_lookup_does_not_starve_later_names(self):
        self._seed([("exampleweb:a", ["Alice", "Bob", "Carol"])])
        checked_names = []

        def no_photo(url, **kwargs):
            checked_names.append(_name_from_url(url))
            return {}

        self.assertEqual(photos_module.backfill_photos(
            self.conn, "http://resolver:8090", limit=1, sleep=0,
            fetch=no_photo, providers=("exampleweb",)), (1, 0))
        self.assertEqual(photos_module.backfill_photos(
            self.conn, "http://resolver:8090", limit=1, sleep=0,
            fetch=no_photo, providers=("exampleweb",)), (1, 0))
        self.assertEqual(checked_names, ["Alice", "Bob"])

    def test_backfill_fetch_error_never_raises(self):
        self._seed([("exampleweb:a", ["Alice", "Bob"])])
        checked, updated = photos_module.backfill_photos(
            self.conn, "http://resolver:8090", limit=25, sleep=0,
            fetch=_fake_fetch({"Bob": "https://cdn.example.com/b.jpg"}, fail_for=("Alice",)),
            providers=("exampleweb",))
        self.assertEqual((checked, updated), (2, 1))
        rows = {r[0]: r[1] for r in
                self.conn.execute("SELECT name, thumb FROM performer_photos").fetchall()}
        self.assertNotIn("Alice", rows)
        self.assertEqual(rows["Bob"], "https://cdn.example.com/b.jpg")

    def test_backfill_no_videos(self):
        calls = []
        self.assertEqual(
            photos_module.backfill_photos(
                self.conn, "http://resolver:8090", limit=25, sleep=0,
                fetch=_fake_fetch({}, calls), providers=("exampleweb",)),
            (0, 0))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
