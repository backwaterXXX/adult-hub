"""Shared normalization rules for imported and displayed titles."""

import unittest

from common.title_cleaning import clean_title_text, extract_title_hashtags


class TestTitleCleaning(unittest.TestCase):
    def test_removes_emoji_promo_runs_and_identical_whole_title_copy(self):
        title = (
            "💪⛓️    ❗️❗️❗️❗️❗️❗️❗️❗️Gymrat KINK  🍆 "
            "💪⛓️    ❗️❗️❗️❗️❗️❗️❗️❗️Gymrat KINK  🍆"
        )
        self.assertEqual(clean_title_text(title), "")

    def test_removes_gymrat_kink_channel_branding_but_keeps_scene_title(self):
        self.assertEqual(
            clean_title_text("💪 GYMRAT Kink - Bareback with Alex #ANAL"),
            "Bareback with Alex",
        )
        self.assertEqual(
            clean_title_text("Alex is bareback GYMRAT Kink"),
            "Alex is bareback",
        )

    def test_removes_category_tags_anywhere_but_keeps_numeric_scene_number(self):
        self.assertEqual(
            clean_title_text("Wank Party #203 #ANAL, #ASIA #OF - Part 1"),
            "Wank Party #203 Part 1",
        )
        self.assertEqual(clean_title_text("Gymrat KINK Gymrat KINK #203"), "#203")

    def test_extracts_casefold_deduplicated_category_tags_and_skips_numeric_ids(self):
        self.assertEqual(
            extract_title_hashtags("#ANAL #anal #ASIA #OF #203"),
            ["ANAL", "ASIA", "OF"],
        )

    def test_cleans_before_truncating(self):
        self.assertEqual(clean_title_text("❗️" * 80 + "x" * 140, max_length=120), "x" * 120)


if __name__ == "__main__":
    unittest.main()
