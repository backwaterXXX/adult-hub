"""Focused tests for provider title lookup helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sync import scene_scraper_text_sources as sources
from sync.scene_scrapers import ScrapedScene


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class TitleLookupTests(unittest.TestCase):
    def test_title_key_keeps_letters_and_digits(self):
        self.assertEqual(sources._title_key("  Demo Scene #2! "), "demoscene2")
        self.assertEqual(sources._title_key(""), "")

    def test_tpdb_exact_matches_filter_title_runtime_and_duplicates(self):
        rows = [
            {"id": "one", "title": "Demo Scene", "duration": 719,
             "site": {"name": "Site"}, "performers": [{"name": "Actor"}],
             "tags": [{"name": "Tag"}], "poster_image": "https://img/one.jpg"},
            {"id": "one", "title": "Demo Scene", "duration": 719},
            {"id": "two", "title": "Demo Scene 2", "duration": 720},
            {"id": "three", "title": "Demo Scene", "duration": 800},
            "not-a-row",
        ]
        matches = sources._tpdb_exact_matches(rows, "demoscene", 720, 2)
        self.assertEqual([item.external_id for item in matches], ["one"])
        self.assertEqual(matches[0].performers, ["Actor"])
        self.assertEqual(matches[0].tags, ["Tag"])
        self.assertEqual(matches[0].studio, "Site")

    def test_tpdb_lookup_requires_token_and_one_exact_match(self):
        candidate = {"id": "one", "title": "Demo Scene", "duration": 720}
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(sources.search_tpdb_scene_by_exact_title("Demo Scene", duration=720))
        with patch.dict("os.environ", {"THEPORNDB_API_KEY": "key"}), \
             patch.object(sources, "_tpdb_title_results", return_value=[candidate]):
            scene = sources.search_tpdb_scene_by_exact_title("Demo Scene", duration=720)
        self.assertEqual(scene.external_id, "one")
        self.assertEqual(scene.source, "theporndb:title_exact")

    def test_stashdb_lookup_requires_exactly_one_title_and_runtime_match(self):
        payload = {"data": {"searchScenes": {"scenes": [
            {"id": "stash-1", "title": "Demo Scene", "duration": 720,
             "studio": {"name": "Studio"}, "performers": [{"performer": {"name": "Actor"}}],
             "tags": [{"name": "Tag"}], "images": [{"url": "https://img/one.jpg"}]},
        ]}}}
        with patch.dict("os.environ", {"STASHDB_API_KEY": "key"}), \
             patch.object(sources._core, "request_with_host_limit", return_value=_Response(payload)):
            scene = sources.search_stashdb_scene_by_exact_title("Demo Scene", duration=720)
        self.assertEqual(scene.external_id, "stash-1")
        self.assertEqual(scene.source, "stashdb:title_exact")
        self.assertEqual(scene.performers, ["Actor"])

        with patch.dict("os.environ", {"STASHDB_API_KEY": "key"}), \
             patch.object(sources._core, "request_with_host_limit", return_value=_Response(
                 {"data": {"searchScenes": {"scenes": []}}})):
            self.assertIsNone(sources.search_stashdb_scene_by_exact_title("Demo Scene", duration=720))

    def test_exact_search_tries_variants_and_collects_transient_failures(self):
        scene = ScrapedScene(title="Demo", duration=720, source="stashdb:title_exact")
        with patch.object(sources, "search_stashdb_scene_by_exact_title", return_value=None), \
             patch.object(sources, "search_fansdb_scene_by_exact_title", return_value=scene), \
             patch.object(sources, "search_tpdb_scene_by_exact_title") as tpdb:
            self.assertIs(sources.search_scene_by_exact_title("Studio - Demo s1", duration=720), scene)
        tpdb.assert_not_called()

        with patch.object(sources, "search_stashdb_scene_by_exact_title", side_effect=RuntimeError("429")), \
             patch.object(sources, "search_fansdb_scene_by_exact_title", return_value=None), \
             patch.object(sources, "search_tpdb_scene_by_exact_title", return_value=None):
            with self.assertRaises(sources.SceneLookupUnavailable):
                sources.search_scene_by_exact_title("Demo", duration=720)


if __name__ == "__main__":
    unittest.main()
