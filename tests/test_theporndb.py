"""ThePornDB enrichment tests."""
import json
import unittest
from unittest.mock import patch

from pg_helper import PgTestCase
from test_support import load_json_fixture



try:
    import sync.theporndb as tpdb_module
    TPDB_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    TPDB_AVAILABLE = False


class TestThePornDB(PgTestCase):
    def setUp(self):
        super().setUp()
        if not TPDB_AVAILABLE:
            self.skipTest("theporndb module not available")

    def test_search_performer_no_token(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(tpdb_module.search_performer("Casey"))

    def test_search_performer_parses_json(self):
        payload = load_json_fixture("api", "theporndb_performer.json")
        res = tpdb_module.search_performer("Casey North", token="test-token", http_get=lambda *a, **k: payload)
        self.assertIsNotNone(res)
        self.assertEqual(res["bio"], "Fictional performer used only in tests.")
        self.assertEqual(res["birth"], "1992-02-02")
        self.assertIn("Casey N", res["aliases"])
        self.assertIn("Northy", res["aliases"])
        self.assertEqual(res["photo"], "https://example.com/casey.jpg")

    def test_backfill_tpdb_enriches_empty_bios(self):
        # Insert one empty bio and one populated bio
        self.conn.execute(
            "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at) VALUES (%s, %s, %s, %s, %s)",
            ("Casey North", "", "", "[]", 100),
        )
        self.conn.execute(
            "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at, nationality) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("Existing Guy", "Already has bio", "1990-01-01", "[]", 200, "USA"),
        )
        self.conn.execute(
            "INSERT INTO performer_photos (name,thumb,updated_at) VALUES (%s,%s,%s)",
            ("Existing Guy", "https://cdn.example.com/existing.jpg", 200))
        self.conn.commit()

        payload = {
            "data": [
                {
                    "name": "Casey North",
                    "bio": "Enriched bio text",
                    "date_of_birth": "1992-02-02",
                    "aliases": ["Casey N"],
                    "image": "https://example.com/casey.jpg",
                }
            ]
        }

        with patch.object(tpdb_module.time, "sleep"):
            checked, updated = tpdb_module.backfill_tpdb(
                self.conn, limit=10, sleep=0, token="test-token", fetch=lambda *a, **k: payload
            )

        self.assertEqual(checked, 1)
        self.assertEqual(updated, 1)

        row = self.conn.execute("SELECT bio, birth, aliases FROM performer_bios WHERE name = 'Casey North'").fetchone()
        self.assertEqual(row[0], "Enriched bio text")
        self.assertEqual(row[1], "1992-02-02")
        self.assertIn("Casey N", json.loads(row[2]))

        alias_row = self.conn.execute("SELECT alias, canonical FROM performer_aliases WHERE alias = 'casey n'").fetchone()
        self.assertEqual(alias_row[1], "Casey North")

        photo_row = self.conn.execute("SELECT thumb FROM performer_photos WHERE name = 'Casey North'").fetchone()
        self.assertEqual(photo_row[0], "https://example.com/casey.jpg")

        # Existing guy was not modified
        existing = self.conn.execute("SELECT bio FROM performer_bios WHERE name = 'Existing Guy'").fetchone()
        self.assertEqual(existing[0], "Already has bio")

    def test_no_match_is_cached_and_later_names_advance(self):
        for name in ("Alice", "Bob", "Carol"):
            self.conn.execute(
                "INSERT INTO performer_bios (name,bio,birth,aliases,updated_at,nationality) "
                "VALUES (%s,'','','[]',1,'')", (name,))
        self.conn.commit()
        checked_names = []

        def no_match(url, **kwargs):
            checked_names.append(url.split("q=", 1)[-1])
            return {"data": []}

        with patch.object(tpdb_module.time, "sleep"):
            self.assertEqual(tpdb_module.backfill_tpdb(
                self.conn, limit=1, sleep=0, token="test-token", fetch=no_match), (1, 0))
            self.assertEqual(tpdb_module.backfill_tpdb(
                self.conn, limit=1, sleep=0, token="test-token", fetch=no_match), (1, 0))
        self.assertEqual(checked_names, ["Alice", "Bob"])
        state = self.conn.execute(
            "SELECT outcome FROM external_metadata_attempts "
            "WHERE entity_type='performer' AND entity_key='alice' AND source='theporndb'"
        ).fetchone()
        self.assertEqual(state[0], "no_match")

    def test_transient_lookup_error_is_backed_off_and_later_names_advance(self):
        for name in ("Alice", "Bob"):
            self.conn.execute(
                "INSERT INTO performer_bios (name,bio,birth,aliases,updated_at,nationality) "
                "VALUES (%s,'','','[]',1,'')", (name,))
        self.conn.commit()
        checked_names = []

        def unavailable(url, **kwargs):
            checked_names.append(url.split("q=", 1)[-1])
            if checked_names[-1] == "Alice":
                raise TimeoutError("upstream unavailable")
            return {"data": []}

        with patch.object(tpdb_module.time, "sleep"):
            self.assertEqual(tpdb_module.backfill_tpdb(
                self.conn, limit=1, sleep=0, token="test-token", fetch=unavailable), (1, 0))
            self.assertEqual(tpdb_module.backfill_tpdb(
                self.conn, limit=1, sleep=0, token="test-token", fetch=unavailable), (1, 0))
        self.assertEqual(checked_names, ["Alice", "Bob"])
        state = self.conn.execute(
            "SELECT outcome,error_streak,last_error_code FROM external_metadata_attempts "
            "WHERE entity_key='alice' AND source='theporndb'"
        ).fetchone()
        self.assertEqual(state, ("error", 1, "TimeoutError"))

    def test_search_performer_parses_extras_and_posters(self):
        payload = {
            "data": [
                {
                    "name": "Diego Sans",
                    "bio": "Popular Brazilian performer.",
                    "extras": {
                        "birthday": "1989-03-31",
                        "nationality": "Brazilian",
                    },
                    "posters": [
                        {"url": "https://cdn.example.com/diego-hd.jpg", "order": 1},
                        {"url": "https://cdn.example.com/diego-thumb.jpg", "order": 2},
                    ],
                    "image": "https://cdn.example.com/diego-fallback.jpg",
                }
            ]
        }
        res = tpdb_module.search_performer("Diego Sans", token="test-token", http_get=lambda *a, **k: payload)
        self.assertIsNotNone(res)
        self.assertEqual(res["bio"], "Popular Brazilian performer.")
        self.assertEqual(res["birth"], "1989-03-31")
        self.assertEqual(res["nationality"], "Brazilian")
        self.assertEqual(res["photo"], "https://cdn.example.com/diego-hd.jpg")

    def test_search_performer_picks_exact_name(self):
        payload = {
            "data": [
                {"name": "Diego Sans Fan", "bio": "Fan club"},
                {"name": "Diego Sans", "bio": "The real Diego"},
            ]
        }
        res = tpdb_module.search_performer("Diego Sans", token="test-token", http_get=lambda *a, **k: payload)
        self.assertIsNotNone(res)
        self.assertEqual(res["bio"], "The real Diego")

    def test_search_performer_rejects_unrelated_first_result(self):
        payload = {"data": [{"name": "Other Performer", "bio": "Wrong person"}]}
        self.assertIsNone(tpdb_module.search_performer(
            "Requested Performer", token="test-token", http_get=lambda *a, **k: payload))


if __name__ == "__main__":
    unittest.main()
