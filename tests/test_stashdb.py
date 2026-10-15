"""StashDB enrichment tests."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase
from test_support import load_json_fixture

sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))


try:
    import stashdb as stash_module
    STASH_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    STASH_AVAILABLE = False


class TestStashDB(PgTestCase):
    def setUp(self):
        super().setUp()
        if not STASH_AVAILABLE:
            self.skipTest("stashdb module not available")

    def test_search_performer_no_token(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(stash_module.search_performer("Malik Delgaty"))

    def test_search_performer_parses_graphql(self):
        payload = load_json_fixture("api", "stashdb_performer.json")
        res = stash_module.search_performer(
            "Malik Delgaty", token="stash-token", http_post=lambda *a, **k: payload
        )
        self.assertIsNotNone(res)
        self.assertEqual(res["bio"], "Canadian adult star and fitness model.")
        self.assertEqual(res["birth"], "2000-09-29")
        self.assertEqual(res["nationality"], "Canada")
        self.assertIn("Malik", res["aliases"])
        self.assertIn("M. Delgaty", res["aliases"])
        self.assertEqual(res["photo"], "https://images.stashdb.org/malik-hd.jpg")
        self.assertEqual(res["stashdb_id"], "stash-123")
        self.assertEqual(res["stashdb_url"], "https://stashdb.org/performers/stash-123")

    def test_search_performer_rejects_unrelated_first_result(self):
        payload = {"data": {"queryPerformers": {"performers": [
            {"name": "Unrelated Performer", "disambiguation": "wrong bio"}
        ]}}}
        self.assertIsNone(stash_module.search_performer(
            "Requested Performer", token="stash-token", http_post=lambda *a, **k: payload))

    def test_backfill_stashdb_enriches_performers(self):
        self.conn.execute(
            "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at, nationality) VALUES (%s, %s, %s, %s, %s, %s)",
            ("Malik Delgaty", "", "", "[]", 100, ""),
        )
        self.conn.commit()

        payload = {
            "data": {
                "queryPerformers": {
                    "performers": [
                        {
                            "id": "stash-123",
                            "name": "Malik Delgaty",
                            "disambiguation": "Canadian fitness model.",
                            "birth_date": "2000-09-29",
                            "country": "Canada",
                            "aliases": ["Malik D"],
                            "images": [
                                {"url": "https://images.stashdb.org/malik-hd.jpg"}
                            ],
                        }
                    ]
                }
            }
        }

        with patch.object(stash_module.time, "sleep"):
            checked, updated = stash_module.backfill_stashdb(
                self.conn, limit=10, sleep=0, token="test-token", fetch=lambda *a, **k: payload
            )

        self.assertEqual(checked, 1)
        self.assertEqual(updated, 1)

        row = self.conn.execute(
            "SELECT bio, birth, nationality, aliases, stashdb_url FROM performer_bios WHERE name = 'Malik Delgaty'"
        ).fetchone()
        self.assertEqual(row[0], "Canadian fitness model.")
        self.assertEqual(row[1], "2000-09-29")
        self.assertEqual(row[2], "Canada")
        self.assertIn("Malik D", json.loads(row[3]))
        self.assertEqual(row[4], "https://stashdb.org/performers/stash-123")

        photo = self.conn.execute(
            "SELECT thumb FROM performer_photos WHERE name = 'Malik Delgaty'"
        ).fetchone()
        self.assertEqual(photo[0], "https://images.stashdb.org/malik-hd.jpg")

    def test_no_match_does_not_pin_the_first_performer(self):
        for name in ("Alice", "Bob", "Carol"):
            self.conn.execute(
                "INSERT INTO performer_bios (name,bio,birth,aliases,updated_at,nationality) "
                "VALUES (%s,'','','[]',1,'')", (name,))
        self.conn.commit()
        checked_names = []

        def no_match(url, **kwargs):
            checked_names.append(kwargs["json"]["variables"]["name"])
            return {"data": {"queryPerformers": {"performers": []}}}

        with patch.object(stash_module.time, "sleep"):
            self.assertEqual(stash_module.backfill_stashdb(
                self.conn, limit=1, sleep=0, token="test-token", fetch=no_match), (1, 0))
            self.assertEqual(stash_module.backfill_stashdb(
                self.conn, limit=1, sleep=0, token="test-token", fetch=no_match), (1, 0))
        self.assertEqual(checked_names, ["Alice", "Bob"])


if __name__ == "__main__":
    unittest.main()
