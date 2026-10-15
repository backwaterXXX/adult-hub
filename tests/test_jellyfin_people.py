import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase

sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
from jellyfin_people import format_pbc_overview, sync_jellyfin_people


class Response:
    def __init__(self, data=None, status_code=200, content=b"", headers=None):
        self.data = data or {}
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class TestJellyfinPeople(PgTestCase):
    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at) VALUES (%s, %s, %s, %s, %s)",
            ("Aaron Chu", "Aaron Chu is an adult model.", "1990", '["A. Chu"]', 123),
        )
        self.conn.execute(
            "INSERT INTO performer_photos (name, thumb, updated_at) VALUES (%s, %s, %s)",
            ("Aaron Chu", "https://cdn.example.org/performers/aaron.jpg", 123),
        )
        self.conn.commit()

    def test_format_pbc_overview(self):
        overview = format_pbc_overview("Armond is an actor.", "1988-06-15")
        self.assertIn("Armond is an actor.", overview)
        self.assertIn("Born: 1988-06-15", overview)

    def test_updates_existing_person_and_preserves_existing_bio(self):
        posted = []

        def get(*args, **kwargs):
            return Response({"Id": "person-id", "Name": "Aaron Chu", "Type": "Person",
                             "Overview": "Existing user bio", "ProviderIds": {"x": "y"}})

        def post(url, **kwargs):
            posted.append((url, kwargs))
            return Response(status_code=204)

        result = sync_jellyfin_people(
            self.conn, "http://jellyfin", "key", 25, get, post,
            lambda *a, **k: Response(content=b"image", headers={"content-type": "image/jpeg"}),
        )
        self.assertEqual(result, (1, 1))
        self.assertEqual(len(posted), 2)
        self.assertTrue(posted[0][1]["json"]["Overview"].startswith("Existing user bio\n\nAaron Chu is an adult model."))
        self.assertEqual(posted[0][1]["json"]["ProviderIds"], {"x": "y"})
        self.assertTrue(posted[1][0].endswith("/Images/Primary"))
        self.assertEqual(posted[1][1]["content"], b"aW1hZ2U=")
        # Second call skips because it's already recorded in jellyfin_person_sync
        self.assertEqual(sync_jellyfin_people(self.conn, "http://jellyfin", "key", 25, get, post), (0, 0))

    def test_missing_person_is_retried(self):
        def get(*args, **kwargs):
            return Response(status_code=404)

        self.assertEqual(sync_jellyfin_people(self.conn, "http://jellyfin", "key", 25, get), (1, 0))
        self.assertEqual(sync_jellyfin_people(self.conn, "http://jellyfin", "key", 25, get), (1, 0))

    def test_replaces_old_stashdb_markers(self):
        self.conn.execute("""CREATE TABLE IF NOT EXISTS jellyfin_person_stash_sync (
            name TEXT PRIMARY KEY, stash_updated_at INTEGER NOT NULL
        )""")
        self.conn.execute("DELETE FROM jellyfin_person_stash_sync")
        self.conn.execute("INSERT INTO jellyfin_person_stash_sync VALUES (%s, %s)", ("Aaron Chu", 123))
        self.conn.commit()
        posted = []

        def get(*args, **kwargs):
            return Response({"Id": "person-id", "Name": "Aaron Chu", "Type": "Person",
                             "Overview": "Biogram.\n\nStashDB: gender: MALE · ethnicity: ASIAN",
                             "ImageTags": {"Primary": "existing"}})

        def post(url, **kwargs):
            posted.append(kwargs["json"]["Overview"])
            return Response(status_code=204)

        self.assertEqual(sync_jellyfin_people(self.conn, "http://jellyfin", "key", 25, get, post), (1, 1))
        self.assertTrue(posted[0].startswith("Biogram.\n\nAaron Chu is an adult model."))
        self.assertNotIn("StashDB:", posted[0])
        self.assertNotIn("ASIAN", posted[0])

    def test_format_pbc_overview_with_nationality(self):
        overview = format_pbc_overview("Armond is an actor.", "1988-06-15", "Canadian")
        self.assertIn("Armond is an actor.", overview)
        self.assertIn("Born: 1988-06-15", overview)
        self.assertIn("Nationality: Canadian", overview)

    def test_format_pbc_overview_with_links(self):
        overview = format_pbc_overview(
            "Armond is an actor.",
            "1988-06-15",
            "Canadian",
            stashdb_url="https://stashdb.org/performers/abc-123",
            pbc_url="https://pbc.xxx/wiki/Armond_Rizzo",
        )
        self.assertIn("Armond is an actor.", overview)
        self.assertIn("Born: 1988-06-15", overview)
        self.assertIn("Nationality: Canadian", overview)
        self.assertIn("StashDB: https://stashdb.org/performers/abc-123 | PBC: https://pbc.xxx/wiki/Armond_Rizzo", overview)

    def test_sync_jellyfin_people_sets_external_urls_and_provider_ids(self):
        self.conn.execute(
            "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at, stashdb_url, pbc_url) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                "Johnny Rapid",
                "Johnny is an actor.",
                "1992-08-26",
                "[]",
                300,
                "https://stashdb.org/performers/99999-aaaa",
                "https://pbc.xxx/wiki/Johnny_Rapid",
            ),
        )
        self.conn.commit()

        from urllib.parse import unquote
        posted_items = []

        def get(url, *args, **kwargs):
            pname = unquote(url.rsplit("/", 1)[-1])
            return Response({"Id": f"{pname.lower()}-id", "Name": pname, "Type": "Person",
                             "Overview": "", "Tags": [], "ProviderIds": {}})

        def post(url, **kwargs):
            posted_items.append(kwargs.get("json"))
            return Response(status_code=204)

        sync_jellyfin_people(
            self.conn, "http://jellyfin", "key", 25, get, post,
            http_image_get=lambda *a, **k: Response(content=b"image", headers={"content-type": "image/jpeg"}),
        )
        johnny_item = next(it for it in posted_items if it and it.get("Name") == "Johnny Rapid")
        self.assertEqual(johnny_item["ProviderIds"]["StashDB"], "99999-aaaa")
        self.assertEqual(
            johnny_item["ExternalUrls"],
            [
                {"Name": "StashDB", "Url": "https://stashdb.org/performers/99999-aaaa"},
                {"Name": "PBC", "Url": "https://pbc.xxx/wiki/Johnny_Rapid"},
            ],
        )
        self.assertIn(
            "StashDB: https://stashdb.org/performers/99999-aaaa | PBC: https://pbc.xxx/wiki/Johnny_Rapid",
            johnny_item["Overview"],
        )

    def test_sync_jellyfin_people_enriches_nationality_and_premiere_date(self):
        self.conn.execute(
            "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at, nationality) VALUES (%s, %s, %s, %s, %s, %s)",
            ("Diego Sans", "Diego is a performer.", "1989-03-31", "[]", 200, "Brazilian"),
        )
        self.conn.commit()

        from urllib.parse import unquote
        posted_items = []

        def get(url, *args, **kwargs):
            pname = unquote(url.rsplit("/", 1)[-1])
            return Response({"Id": f"{pname.lower()}-id", "Name": pname, "Type": "Person",
                             "Overview": "", "Tags": []})

        def post(url, **kwargs):
            posted_items.append(kwargs.get("json"))
            return Response(status_code=204)

        result = sync_jellyfin_people(
            self.conn, "http://jellyfin", "key", 25, get, post,
            http_image_get=lambda *a, **k: Response(content=b"image", headers={"content-type": "image/jpeg"}),
        )
        # Aaron Chu + Diego Sans are both candidates
        self.assertEqual(result, (2, 2))
        diego_item = next(it for it in posted_items if it and it.get("Name") == "Diego Sans")
        self.assertEqual(diego_item["PremiereDate"], "1989-03-31T00:00:00.0000000Z")
        self.assertEqual(diego_item["ProductionYear"], 1989)
        self.assertEqual(diego_item["ProductionLocations"], ["Brazilian"])
        self.assertIn("Brazilian", diego_item["Tags"])
        self.assertIn("Nationality: Brazilian", diego_item["Overview"])


if __name__ == "__main__":
    unittest.main()
