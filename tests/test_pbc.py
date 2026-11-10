"""PBC (Porn Base Central) enrichment: parsing + backfill."""
import json
import unittest
from unittest.mock import patch

from pg_helper import PgTestCase



try:
    import sync.pbc as pbc_module
    PBC_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    PBC_AVAILABLE = False


FIXTURE_WIKITEXT = """{{Infobox performer
| Name: | Ryan Rose
| Birth name: | Ryan Smith
| Aliases: | Danny Rose, danny rose; Johnny R
| Birth: | January 5, 1990 (age 36); Los Angeles
}}

'''Ryan Rose''' (a.k.a. Danny Rose, Bobby R; born January 5, 1990) is an [[United States|American]] '''gay''' [[pornographic actor|actor]]<ref>some source</ref>{{fact}}.

Second paragraph that must not leak into the bio.
"""


class TestPageTitle(unittest.TestCase):
    def setUp(self):
        if not PBC_AVAILABLE:
            self.skipTest("pbc module not available")

    def test_basic(self):
        self.assertEqual(pbc_module.page_title("Rico Marlon"), "Rico_Marlon")

    def test_strip_and_collapse(self):
        self.assertEqual(pbc_module.page_title("  Rico   Marlon  "), "Rico_Marlon")

    def test_non_string(self):
        self.assertEqual(pbc_module.page_title(None), "")


class TestFetchWikitext(unittest.TestCase):
    def setUp(self):
        if not PBC_AVAILABLE:
            self.skipTest("pbc module not available")

    def _api_doc(self, content):
        return {"query": {"pages": [{"title": "Ryan_Rose",
                                     "revisions": [{"content": content}]}]}}

    def test_success_dict_payload(self):
        doc = self._api_doc("hello")
        self.assertEqual(
            pbc_module.fetch_wikitext("Ryan_Rose", http_get=lambda *a, **k: doc),
            "hello",
        )

    def test_missing_page(self):
        doc = {"query": {"pages": [{"title": "Nope", "missing": True}]}}
        self.assertIsNone(
            pbc_module.fetch_wikitext("Nope", http_get=lambda *a, **k: doc)
        )

    def test_error_returns_none(self):
        def boom(*a, **k):
            raise RuntimeError("net down")

        self.assertIsNone(pbc_module.fetch_wikitext("X", http_get=boom))

    def test_url_encodes_title(self):
        seen = {}

        def fake(url, timeout=None):
            seen["url"] = url
            return self._api_doc("x")

        pbc_module.fetch_wikitext("Rico Marlon", http_get=fake)
        self.assertIn("titles=Rico%20Marlon", seen["url"])
        self.assertIn("rvprop=content", seen["url"])


class TestParsePerson(unittest.TestCase):
    def setUp(self):
        if not PBC_AVAILABLE:
            self.skipTest("pbc module not available")

    def test_none_input(self):
        self.assertEqual(
            pbc_module.parse_person(None),
            {"aliases": [], "bio": "", "birth": ""},
        )

    def test_aliases_from_infobox_and_aka_deduped(self):
        parsed = pbc_module.parse_person(FIXTURE_WIKITEXT)
        aliases = parsed["aliases"]
        # Infobox has Danny Rose (x2 case-variant) + Johnny R;
        # first paragraph adds Bobby R and repeats Danny Rose.
        self.assertIn("Danny Rose", aliases)
        self.assertIn("Johnny R", aliases)
        self.assertIn("Bobby R", aliases)
        self.assertEqual(len(aliases), len({a.lower() for a in aliases}))

    def test_bio_is_plain_text(self):
        parsed = pbc_module.parse_person(FIXTURE_WIKITEXT)
        bio = parsed["bio"]
        for marker in ("[[", "]]", "'''", "<ref", "{{", "}}"):
            self.assertNotIn(marker, bio)
        self.assertIn("Ryan Rose", bio)
        self.assertIn("American", bio)
        self.assertIn("actor", bio)
        self.assertNotIn("Second paragraph", bio)
        self.assertLessEqual(len(bio), 2000)

    def test_birth_cut_at_paren(self):
        parsed = pbc_module.parse_person(FIXTURE_WIKITEXT)
        self.assertEqual(parsed["birth"], "January 5, 1990")

    def test_birth_missing(self):
        parsed = pbc_module.parse_person("Just some text without infobox.")
        self.assertEqual(parsed["birth"], "")


class TestBackfill(PgTestCase):
    def setUp(self):
        super().setUp()
        if not PBC_AVAILABLE:
            self.skipTest("pbc module not available")
        self.conn.execute(
            "INSERT INTO videos (id, performers) VALUES (%s, %s)",
            ("exampleweb:1", json.dumps(["Ryan Rose", "Solo Guy"])),
        )
        self.conn.execute(
            "INSERT INTO videos (id, performers) VALUES (%s, %s)",
            ("secondweb:2", json.dumps(["Ryan Rose", "Missing Guy"])),
        )
        self.conn.commit()

    def _fetch(self, title):
        if title == "Ryan_Rose":
            return FIXTURE_WIKITEXT
        if title == "Solo_Guy":
            return "{{Infobox}}\n| Aliases: | Solo Guy, Other Guy\n\nSolo Guy is a performer."
        return None

    def test_inserts_aliases_and_empty_row(self):
        with patch.object(pbc_module.time, "sleep"):
            checked, updated = pbc_module.backfill_pbc(
                self.conn, limit=25, sleep=0, fetch=self._fetch
            )
        # Ryan Rose, Solo Guy, Missing Guy (sorted) — all missing bios.
        self.assertEqual((checked, updated), (3, 3))
        bios = {
            row[0]: row
            for row in self.conn.execute(
                "SELECT name, bio, birth, aliases, pbc_url FROM performer_bios"
            ).fetchall()
        }
        self.assertIn("Ryan Rose", bios)
        self.assertIn("Missing Guy", bios)
        self.assertEqual(bios["Ryan Rose"][4], "https://pbc.xxx/wiki/Ryan_Rose")
        self.assertEqual(bios["Missing Guy"][4], "")
        # Empty fetch result still writes a row (no endless re-query).
        self.assertEqual(bios["Missing Guy"][1], "")
        self.assertEqual(json.loads(bios["Missing Guy"][3]), [])
        # Aliases stored, canonical itself skipped, key lowercased.
        alias_rows = dict(
            self.conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall()
        )
        self.assertEqual(alias_rows.get("danny rose"), "Ryan Rose")
        self.assertEqual(alias_rows.get("bobby r"), "Ryan Rose")
        self.assertEqual(alias_rows.get("other guy"), "Solo Guy")
        self.assertNotIn("solo guy", alias_rows)
        # Canonical value keeps the exact videos.performers spelling.
        self.assertEqual(alias_rows.get("johnny r"), "Ryan Rose")

    def test_limit_is_respected(self):
        with patch.object(pbc_module.time, "sleep"):
            checked, updated = pbc_module.backfill_pbc(
                self.conn, limit=1, sleep=0, fetch=self._fetch
            )
        self.assertEqual((checked, updated), (1, 1))
        count = self.conn.execute("SELECT count(*) FROM performer_bios").fetchone()[0]
        self.assertEqual(count, 1)
        # Alphabetically first missing name wins.
        row = self.conn.execute("SELECT name FROM performer_bios").fetchone()
        self.assertEqual(row[0], "Missing Guy")

    def test_search_page_title_hit(self):
        doc = {"query": {"search": [{"title": "Johnny Rapid (actor)"}]}}
        self.assertEqual(
            pbc_module.search_page_title("Johnny Rapid", http_get=lambda *a, **k: doc),
            "Johnny Rapid (actor)",
        )

    def test_search_page_title_miss(self):
        doc = {"query": {"search": []}}
        self.assertIsNone(
            pbc_module.search_page_title("Unknown Guy", http_get=lambda *a, **k: doc)
        )

    def test_backfill_with_search_fallback(self):
        def _fetch_with_search_target(title):
            if title == "Johnny_Rapid_(actor)":
                return "{{Infobox}}\n| Aliases: | J-Rap, Missing Guy\n\nJohnny Rapid is an actor."
            return None

        def _mock_search(name):
            if name == "Missing Guy":
                return "Johnny Rapid (actor)"
            return None

        with patch.object(pbc_module.time, "sleep"):
            checked, updated = pbc_module.backfill_pbc(
                self.conn, limit=25, sleep=0, fetch=_fetch_with_search_target, search=_mock_search
            )
        bios = dict(self.conn.execute("SELECT name, bio FROM performer_bios").fetchall())
        self.assertIn("Missing Guy", bios)
        self.assertIn("Johnny Rapid is an actor", bios["Missing Guy"])
        alias_rows = dict(self.conn.execute("SELECT alias, canonical FROM performer_aliases").fetchall())
        self.assertEqual(alias_rows.get("j-rap"), "Missing Guy")


if __name__ == "__main__":
    unittest.main()
