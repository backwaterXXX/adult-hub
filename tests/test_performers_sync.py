"""Performer persistence and sync pipeline integration."""

import json
from pathlib import Path
import tempfile
import unittest

from common.performers import normalize_performers
from pg_helper import PgTestCase, TEST_URL, reset_db
from resolver.providers.base import is_performer_segment, is_valid_performer_name
from test_support import load_sync_module

sync_module = load_sync_module()
SYNC_AVAILABLE = True


class TestPerformerSyncPassthrough(PgTestCase):
    def setUp(self):
        if not SYNC_AVAILABLE:
            self.skipTest("sync module not available")
        reset_db(self.db_url)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.conn = sync_module.init_db(TEST_URL)
        self.addCleanup(self.conn.close)

    def test_performers_persisted_and_updated_in_db(self):
        video = {
            "provider": "exampleweb",
            "provider_video_id": "1/s",
            "title": "T",
            "slug": "s",
            "url": "",
            "thumbnail": "",
            "duration": 600,
            "performers": ["First Guy"],
        }
        sync_module.sync_unique_video(video, set(), set(), self.media, self.conn)
        row = self.conn.execute(
            "SELECT performers FROM videos WHERE id = %s", ("exampleweb:1/s",)).fetchone()
        self.assertEqual(json.loads(row[0]), ["First Guy"])
        # Re-sync with new performers updates the row (no stale data).
        video["performers"] = ["First Guy", "Second Guy"]
        sync_module.sync_unique_video(video, set(), set(), self.media, self.conn)
        row = self.conn.execute(
            "SELECT performers FROM videos WHERE id = %s", ("exampleweb:1/s",)).fetchone()
        self.assertEqual(json.loads(row[0]), ["First Guy", "Second Guy"])

    def test_collect_merges_performers_across_sightings(self):
        from unittest.mock import patch
        first = {
            "provider": "exampleweb", "provider_video_id": "1/s", "title": "T",
            "performers": ["First Guy"], "categories": [],
        }
        second = {
            "provider": "exampleweb", "provider_video_id": "1/s", "title": "T",
            "performers": ["Second Guy", "First Guy"], "categories": [],
        }
        collected = {"exampleweb:1/s": {"video": first, "genres": set(), "tags": set()}}
        with patch.object(sync_module, "fetch_provider_categories",
                           return_value=[{"id": "solo", "title": "Solo"}]), \
             patch.object(sync_module, "fetch_provider_category_videos",
                           return_value=[second]), \
             patch.object(sync_module.time, "sleep"):
            sync_module.collect_provider_categories(collected, "exampleweb", 1)
        self.assertEqual(
            collected["exampleweb:1/s"]["video"]["performers"],
            ["First Guy", "Second Guy"],
        )

    def test_backfill_performers_from_titles(self):
        video = {
            "provider": "exampleweb",
            "provider_video_id": "69769",
            "title": "Alex Ink, Alexandro Cabrera & Pietro – A Hot Surprise When He Gets Home",
            "slug": "alex-ink",
            "url": "https://example.com/video/69769",
            "thumbnail": "",
            "duration": 1680,
            "performers": [],
        }
        sync_module.sync_unique_video(video, {"Anal"}, {"Exampleweb"}, self.media, self.conn)

        updated = sync_module.backfill_performers_from_titles(self.conn, self.media)
        self.assertEqual(updated, 1)

        row = self.conn.execute("SELECT performers FROM videos WHERE id = 'exampleweb:69769'").fetchone()
        self.assertEqual(json.loads(row[0]), ["Alex Ink", "Alexandro Cabrera", "Pietro"])

    def test_multi_part_title_prefers_full_names(self):
        from common.performer_titles import parse_title_performers_with_known_map
        known_map = {'claude': 'Claude', 'malik': 'Malik'}

        # Full names in last part should win over first names / summary in first part
        perfs = parse_title_performers_with_known_map(
            'Claude and Tim - 8906 - Claude Sorel, Tim Campbell.mp4', known_map
        )
        self.assertEqual(perfs, ['Claude Sorel', 'Tim Campbell'])

        # Multi-part with .part001.mp4 extension
        perfs = parse_title_performers_with_known_map(
            'Todd and Claude - 9742 - Claude Sorel, Todd Rosset.part001.mp4', known_map
        )
        self.assertEqual(perfs, ['Claude Sorel', 'Todd Rosset'])

        # Title words in first part vs full names in second part
        perfs = parse_title_performers_with_known_map(
            '[Bro Network] Recruit & Tackle - Marcus McNeill & Enzo Muller', known_map
        )
        self.assertEqual(perfs, ['Marcus McNeill', 'Enzo Muller'])

    def test_shared_title_parser_regressions(self):
        from common.performer_titles import (
            parse_title_performers_with_known_map,
            strip_performer_credits,
        )

        self.assertEqual(
            parse_title_performers_with_known_map('Alex Ink and John Doe - Morning Scene'),
            ['Alex Ink', 'John Doe'],
        )
        self.assertEqual(
            parse_title_performers_with_known_map(
                'John Doe meets Christopher Reynolds',
                {'john doe': 'John Doe', 'christopher reynolds': 'Christopher Reynolds'},
            ),
            ['John Doe', 'Christopher Reynolds'],
        )
        overlapping_names = {'felipe eduardo': 'Felipe Eduardo', 'felipe': 'Felipe'}
        self.assertEqual(
            parse_title_performers_with_known_map(
                '#ANAL #OF - Venezolano Felipe Eduardo & texasgymjock',
                overlapping_names,
            ),
            ['Felipe Eduardo'],
        )
        self.assertEqual(
            parse_title_performers_with_known_map(
                'Felipe Eduardo meets Felipe at the gym', overlapping_names
            ),
            ['Felipe Eduardo', 'Felipe'],
        )
        telegram_known = {
            **overlapping_names,
            'venezolano felipe eduardo': 'Venezolano Felipe Eduardo',
            'texasgymjock': 'Texas Gym Jock',
            'cj delco': 'CJ Delco',
        }
        self.assertEqual(
            parse_title_performers_with_known_map(
                '#ANAL #OF - Venezolano Felipe Eduardo & texasgymjock',
                telegram_known,
            ),
            ['Felipe Eduardo', 'Texas Gym Jock'],
        )
        self.assertEqual(
            parse_title_performers_with_known_map(
                '#OF #ANAL Only Fans – CJ Delco And Felipe Eduardo',
                telegram_known,
            ),
            ['CJ Delco', 'Felipe Eduardo'],
        )
        known_name = {'first second': 'First Second'}
        self.assertEqual(
            parse_title_performers_with_known_map('FirstSecond and Third_Fourth - Scene'),
            ['First Second', 'Third Fourth'],
        )
        self.assertEqual(
            parse_title_performers_with_known_map('FirstSecond fucks Third_Fourth'),
            ['First Second', 'Third Fourth'],
        )
        for title in (
            'Scene-FirstSecond',
            'Scene-First_Second',
            'Scene_First_Second',
            'Scene-First-Second',
            'Scene-First Second',
        ):
            with self.subTest(title=title):
                self.assertEqual(
                    parse_title_performers_with_known_map(title, known_name),
                    ['First Second'],
                )
        self.assertEqual(
            parse_title_performers_with_known_map(
                'Scene-FirstSecondary', known_name
            ),
            [],
        )
        self.assertEqual(
            strip_performer_credits(
                'Alex Ink - Foo — Bar', ['Alex Ink'], is_performer_segment
            ),
            'Foo — Bar',
        )
        self.assertEqual(
            strip_performer_credits(
                'Alex Ink - John Doe - Scene',
                ['Alex Ink', 'John Doe'],
                is_performer_segment,
            ),
            'Scene',
        )

    def test_lucas_not_treated_as_single_word_performer(self):
        self.assertFalse(is_valid_performer_name("Lucas"))
        self.assertFalse(is_valid_performer_name("Falcon"))
        self.assertTrue(is_valid_performer_name("Michael Lucas"))
        self.assertTrue(is_valid_performer_name("Lucas Dias"))
        self.assertFalse(is_performer_segment("[Lucas Raunch]", ["Lucas"]))
        self.assertFalse(is_performer_segment("[Lucas Entertainment]", ["Lucas"]))

    def test_black_and_jordan_not_treated_as_single_word_performers(self):
        self.assertFalse(is_valid_performer_name("Black"))
        self.assertFalse(is_valid_performer_name("black"))
        self.assertFalse(is_valid_performer_name("Jordan"))
        self.assertFalse(is_valid_performer_name("jordan"))
        self.assertTrue(is_valid_performer_name("Morgan Black"))
        self.assertTrue(is_valid_performer_name("Heath Jordan"))
        self.assertTrue(is_valid_performer_name("Jordan Starr"))
        self.assertTrue(is_valid_performer_name("Jordan Neo"))
        self.assertTrue(is_valid_performer_name("Damian Black"))
        self.assertTrue(is_valid_performer_name("Conner Habib"))

    def test_nationality_prefixes_stripped_from_performers(self):
        from common.performer_titles import parse_title_performers_with_known_map

        self.assertFalse(is_valid_performer_name("Brasileiro"))
        self.assertFalse(is_valid_performer_name("Brasileiros"))
        self.assertFalse(is_valid_performer_name("Argentino"))
        self.assertFalse(is_valid_performer_name("Colombiano"))
        self.assertTrue(is_valid_performer_name("That Switch"))

        self.assertEqual(
            normalize_performers(["Brasileiro That Switch"]),
            ["That Switch"],
        )
        self.assertEqual(
            normalize_performers(["Brasileiros That Switch"]),
            ["That Switch"],
        )
        self.assertEqual(
            normalize_performers(["Colombiano Flaquito Vergon"]),
            ["Flaquito Vergon"],
        )

        title1 = "💕  #OF  #GROUP  OnlyFans - Argentinos ViktorOnee, Emiliano Terra, Federico Martin, & NakedYogi, Brasileiro That Switch"
        self.assertEqual(
            parse_title_performers_with_known_map(title1, {"viktoronee": "Viktor Onee"}),
            ["Viktor Onee", "Emiliano Terra", "Federico Martin", "Naked Yogi", "That Switch"],
        )

        title2 = "🔥  #GROUP  RFC  - Brasileiros That Switch and Jeff Carvalho, Argentino CraptusLion"
        self.assertEqual(
            parse_title_performers_with_known_map(title2),
            ["That Switch", "Jeff Carvalho", "Craptus Lion"],
        )

        title3 = "💕  #GROUP  #OF - Orgy in Brazil - Brasileiro Victor Vivone, Argentinos Felipe Hervera, Emiliano Terra & Julian Oxilia, V"
        self.assertEqual(
            parse_title_performers_with_known_map(title3),
            ["Victor Vivone", "Felipe Hervera", "Emiliano Terra", "Julian Oxilia"],
        )



if __name__ == "__main__":
    unittest.main()
