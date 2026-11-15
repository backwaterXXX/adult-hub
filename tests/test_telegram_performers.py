"""Performer matching against Telegram message captions, filenames, aliases, and snapshots."""

import asyncio
import os
import unittest
from unittest.mock import patch

from pg_helper import TEST_URL, init_test_db, require_url

if TEST_URL:
    os.environ["DATABASE_URL"] = TEST_URL

from tests.test_telegram import (
    _require_telegram,
    _video_message,
    load_known_performers,
    match_known_performers,
    message_to_video,
    tg_module,
)


class TestPerformerMatching(unittest.TestCase):
    def setUp(self):
        _require_telegram(self)
        if match_known_performers is None:
            self.fail("required Telegram function missing: match_known_performers")

    @staticmethod
    def _catalog(names, aliases=None):
        aliases = aliases or {}
        return [
            {"id": f"stashdb:{index}", "name": name,
             "aliases": [alias for alias, canonical in aliases.items() if canonical == name],
             "trusted_aliases": [alias for alias, canonical in aliases.items() if canonical == name]}
            for index, name in enumerate(names, 1)
        ]

    def test_full_name_match(self):
        out = match_known_performers("OF Andres Milan Rico Marlon 720p",
                                     ["Rico Marlon", "Andres Milan"])
        self.assertIn("Rico Marlon", out)
        self.assertIn("Andres Milan", out)

    def test_concatenated_name_no_match(self):
        # Krotka sklejka (< 8 liter po sklejeniu) nie lapie sie — brak zgadywania.
        out = match_known_performers("AlBo show", ["Al Bo"])
        self.assertEqual(out, [])

    def test_glued_filename_match(self):
        # Dlugie wielowyrazowe sklejki z nazw plikow lapia sie wariantem bezspacjowym.
        out = match_known_performers("OF AndresMilan RicoMarlon 720p",
                                     ["Rico Marlon", "Andres Milan"])
        self.assertIn("Rico Marlon", out)
        self.assertIn("Andres Milan", out)

    def test_short_glued_no_match(self):
        out = match_known_performers("AlPacino show", ["Al Pacino"])
        self.assertEqual(out, ["Al Pacino"])
        # "alpacino" ma 8 liter -> prog zaliczony; to celowy trade-off,
        # ale krotkie sklejki ponizej 8 liter odpadaja:
        out2 = match_known_performers("JoDoe show", ["Jo Doe"])
        self.assertEqual(out2, [])

    def test_alias_maps_to_canonical(self):
        out = match_known_performers("Pierce bareback", ["Ryan Rose"],
                                     aliases={"pierce": "Ryan Rose"})
        self.assertEqual(out, ["Ryan Rose"])

    def test_alias_only_dictionary(self):
        out = match_known_performers("Pierce bareback", [],
                                     aliases={"pierce": "Ryan Rose"})
        self.assertEqual(out, ["Ryan Rose"])

    def test_alias_no_double_count(self):
        out = match_known_performers("Ryan Rose aka Pierce", ["Ryan Rose"],
                                     aliases={"pierce": "Ryan Rose"})
        self.assertEqual(out, ["Ryan Rose"])

    def test_alias_unknown_ignored(self):
        out = match_known_performers("Some Guy show", ["Ryan Rose"],
                                     aliases={"": "X", "  ": "Y"})
        self.assertEqual(out, [])

    def test_case_insensitive(self):
        out = match_known_performers("RICO MARLON show", ["Rico Marlon"])
        self.assertEqual(out, ["Rico Marlon"])

    def test_no_partial_word_match(self):
        out = match_known_performers("Marlonetti show", ["Marlon"])
        self.assertEqual(out, [])

    def test_empty_dict_means_empty(self):
        self.assertEqual(match_known_performers("Rico Marlon", []), [])
        self.assertEqual(match_known_performers("Rico Marlon", None), [])

    def test_max_five(self):
        names = [f"Actor Number{i}" for i in range(10)]
        text = " ".join(names)
        self.assertEqual(len(match_known_performers(text, names)), 5)

    def test_subname_collision_prevented(self):
        # Mononyms like 'Bryce' and 'James' must not match inside full names like 'Bryce Jax' or 'James Cassidy'
        text = "🔥 Only Fans - James Cassidy, Bryce Jax & Matthew Ellis"
        names = ["Matthew Ellis", "James Cassidy", "Bryce Jax", "Bryce", "James"]
        out = match_known_performers(text, names)
        self.assertEqual(sorted(out), ["Bryce Jax", "James Cassidy", "Matthew Ellis"])

    def test_separate_mononym_occurrence_is_preserved(self):
        out = match_known_performers("James Cassidy and James", ["James Cassidy", "James"])
        self.assertEqual(out, ["James Cassidy", "James"])

    def test_invalid_names_like_editors_cut_and_fucked_are_ignored(self):
        out = match_known_performers(
            "MenAtPlay - Campaign Erection, Editor's Cut.mp4 - Gael Kriok fucked by Ronny",
            ["Editor's Cut.mp4", "Fucked", "fucked", "Gael Kriok", "Ronny", "Campaign Erection"]
        )
        self.assertIn("Gael Kriok", out)
        self.assertIn("Ronny", out)
        self.assertNotIn("Editor's Cut.mp4", out)
        self.assertNotIn("Fucked", out)
        self.assertNotIn("fucked", out)
        self.assertNotIn("Campaign Erection", out)

    def test_user_cases_no_false_alias_and_first_name_truncation(self):
        # 1. Lance Bennett & Leon Fox:
        # 'Lance' must not match inside 'Lance Bennett'
        # 'leon' alias (e.g. Paddy O'Brian) must not match inside 'Leon Fox'
        out = match_known_performers(
            "Lance Bennett & Leon Fox",
            ["Lance", "Leon Fox"],
            aliases={"leon": "Paddy O'Brian"}
        )
        self.assertNotIn("Lance", out)
        self.assertNotIn("Paddy O'Brian", out)
        self.assertIn("Leon Fox", out)

    def test_message_to_video_uses_aliases_and_suppresses_name_overlap(self):
        msg = _video_message(
            caption="#ANAL #OF - Venezolano Felipe Eduardo & texasgymjock"
        )
        catalog = self._catalog(["Felipe Eduardo", "Texas Gym Jock"], {
            "Venezolano Felipe Eduardo": "Felipe Eduardo", "texasgymjock": "Texas Gym Jock",
        })
        with patch.object(tg_module, "load_performer_catalog", return_value=catalog):
            item = message_to_video(msg, "Kanal")
        self.assertEqual(list(item.performers), ["Felipe Eduardo", "Texas Gym Jock"])

        msg = _video_message(
            caption="#OF #ANAL Only Fans – CJ Delco And Felipe Eduardo"
        )
        catalog = self._catalog(["Felipe Eduardo", "CJ Delco"])
        with patch.object(tg_module, "load_performer_catalog", return_value=catalog):
            item = message_to_video(msg, "Kanal")
        self.assertEqual(list(item.performers), ["CJ Delco", "Felipe Eduardo"])

    def test_user_cases_message_to_video_title_syntax(self):
        # 1. [Only Fans] Rhyheim Shabazz, Cade Maddox & Alonzo Jordon
        # Uncatalogued credits are proposed for review rather than auto-added.
        msg1 = _video_message(caption="[Only Fans] Rhyheim Shabazz, Cade Maddox & Alonzo Jordon")
        catalog = self._catalog(["Rhyheim Shabazz", "Cade Maddox", "Leon Reddz", "Adrian Hart", "Leon Fox"])
        with patch.object(tg_module, "load_performer_catalog", return_value=catalog):
            item1 = message_to_video(msg1, "Kanal")
        self.assertEqual(list(item1.performers), ["Rhyheim Shabazz", "Cade Maddox"])
        self.assertTrue(any(p["matched_text"] == "Alonzo Jordon" for p in item1.performer_proposals))

        # 2. [Just For Fans] - Rhyheim Shabazz, Krave, Trapp, Fame & Leon Reddz fuck & DP Adrian Hart
        # Must not have Paddy O'Brian
        msg2 = _video_message(
            caption="[Just For Fans] - Rhyheim Shabazz, Krave, Trapp, Fame & Leon Reddz fuck & DP Adrian Hart"
        )
        with patch.object(tg_module, "load_performer_catalog", return_value=catalog):
            item2 = message_to_video(msg2, "Kanal")
        self.assertNotIn("Paddy O'Brian", item2.performers)
        self.assertIn("Leon Reddz", item2.performers)
        self.assertIn("Adrian Hart", item2.performers)
        self.assertIn("Rhyheim Shabazz", item2.performers)

        # 3. Lance Bennett & Leon Fox
        msg3 = _video_message(caption="Lance Bennett & Leon Fox")
        with patch.object(tg_module, "load_performer_catalog", return_value=catalog):
            item3 = message_to_video(msg3, "Kanal")
        self.assertNotIn("Paddy O'Brian", item3.performers)
        self.assertNotIn("Lance", item3.performers)
        self.assertEqual(list(item3.performers), ["Leon Fox"])
        self.assertTrue(any(p["matched_text"] == "Lance Bennett" for p in item3.performer_proposals))

    def test_auto_loaded_aliases(self):
        msg = _video_message(caption="Pierce bareback")
        catalog = self._catalog(["Ryan Rose"], {"pierce": "Ryan Rose"})
        with patch.object(tg_module, "load_performer_catalog", return_value=catalog):
            item = message_to_video(msg, "Kanal")
        self.assertEqual(list(item.performers), ["Ryan Rose"])

    def test_video_gets_performers_from_dict(self):
        if message_to_video is None:
            self.fail("required Telegram function missing: message_to_video")
        msg = _video_message(caption="Rico Marlon bareback")
        with patch.object(tg_module, "load_performer_catalog",
                          return_value=self._catalog(["Rico Marlon"])):
            item = message_to_video(msg, "Kanal")
        self.assertEqual(list(item.performers), ["Rico Marlon"])

    def test_load_known_performers_missing_db(self):
        if load_known_performers is None:
            self.fail("required Telegram function missing: load_known_performers")
        require_url()
        init_test_db().close()
        tg_module._KNOWN_PERFORMERS_CACHE.update({"mtime": 0.0, "names": []})
        tg_module._ALIAS_MAP_CACHE.update({"mtime": 0.0, "entries": {}})
        with patch.dict(os.environ, {"DATABASE_URL": TEST_URL}):
            self.assertEqual(load_known_performers(), [])



class PerformerSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_share_off_loop_snapshot_and_refresh_after_ttl(self):
        import threading
        started = threading.Event()
        release = threading.Event()
        def load():
            started.set()
            release.wait(3)
            return ['Actor']
        with patch.object(tg_module, '_performer_snapshot', None), \
             patch.object(tg_module, '_performer_snapshot_at', 0), \
             patch.object(tg_module, 'load_known_performers', side_effect=load) as known, \
             patch.object(tg_module, 'load_alias_map', return_value={}) as aliases, \
             patch.object(tg_module, '_prepare_external_performer_catalog', return_value={}) as catalog:
            first = asyncio.create_task(asyncio.to_thread(tg_module._load_performer_snapshot))
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            second = asyncio.create_task(asyncio.to_thread(tg_module._load_performer_snapshot))
            await asyncio.sleep(0)
            release.set()
            one, two = await asyncio.gather(first, second)
            self.assertIs(one, two)
            self.assertEqual((known.call_count, aliases.call_count, catalog.call_count), (1, 1, 1))
            tg_module._performer_snapshot_at -= tg_module._PERFORMER_SNAPSHOT_TTL + 1
            await asyncio.to_thread(tg_module._load_performer_snapshot)
            self.assertEqual(known.call_count, 2)



if __name__ == "__main__":
    unittest.main()
