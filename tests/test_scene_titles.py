import unittest
from unittest.mock import patch

from common.scene_titles import lookup_title_variants
from sync import scene_scraper_split_parts as split_parts
from sync import scene_scraper_text_sources as text_sources


class LookupTitleVariantTests(unittest.TestCase):
    def test_tries_the_final_segment_before_the_full_studio_title(self):
        self.assertEqual(
            lookup_title_variants("CockyBoys – Fuck Me Raw s1"),
            ["Fuck Me Raw", "CockyBoys – Fuck Me Raw"],
        )

    def test_strips_year_and_scene_suffix(self):
        self.assertEqual(
            lookup_title_variants("Next Door Studios - Shh! Don't Wake My Boyfriend (2023) s2"),
            ["Shh! Don't Wake My Boyfriend", "Next Door Studios - Shh! Don't Wake My Boyfriend"],
        )

    def test_middle_scene_segment_beats_a_trailing_cast_list(self):
        variants = lookup_title_variants(
            "Raging Stallion - Overdrive (2023) Sc1 - Paul Wagner & Derek Kage 4K s2"
        )
        self.assertEqual(variants[0], "Overdrive")
        self.assertIn("Raging Stallion - Overdrive Sc1 - Paul Wagner & Derek Kage", variants)

    def test_plain_title_is_returned_once(self):
        self.assertEqual(lookup_title_variants("Fuck Me Raw"), ["Fuck Me Raw"])

    def test_empty_title_is_ignored_and_media_suffix_is_removed(self):
        self.assertEqual(lookup_title_variants(""), [])
        self.assertEqual(lookup_title_variants("movie.mp4"), ["movie"])


class TitleVariantIntegrationTests(unittest.TestCase):
    def test_exact_lookup_queries_the_short_title_first(self):
        with patch.object(text_sources, "search_stashdb_scene_by_exact_title", return_value=None) as stashdb, \
             patch.object(text_sources, "search_fansdb_scene_by_exact_title", return_value=None), \
             patch.object(text_sources, "search_tpdb_scene_by_exact_title", return_value=None):
            text_sources.search_scene_by_exact_title("CockyBoys – Fuck Me Raw s1", duration=728)
        self.assertEqual(stashdb.call_args_list[0].args[0], "Fuck Me Raw")

    def test_split_lookup_queries_the_middle_scene_title_first(self):
        with patch.object(split_parts, "_stashdb_candidates", return_value=[]) as stashdb, \
             patch.object(split_parts, "_fansdb_candidates", return_value=[]), \
             patch.object(split_parts, "_theporndb_candidates", return_value=[]):
            split_parts.search_split_scene(
                "Raging Stallion - Overdrive (2023) Sc1 - Paul Wagner & Derek Kage 4K",
                duration=2695,
            )
        self.assertEqual(stashdb.call_args_list[0].args[0], "Overdrive")


if __name__ == "__main__":
    unittest.main()
