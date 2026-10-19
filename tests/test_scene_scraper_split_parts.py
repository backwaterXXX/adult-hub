import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sync import scene_scraper_split_parts as split_parts
from sync import scene_scraper_backfill as backfill
from sync.scene_scrapers import ScrapedScene


class _Cursor:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if "LEFT JOIN external_metadata_attempts a" in query:
            return _Cursor(self.rows)
        raise AssertionError(f"Unexpected SQL: {query}")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TestSplitPartTitle(unittest.TestCase):
    def test_parses_s_number_and_part_number(self):
        self.assertEqual(
            split_parts.parse_split_part_title("🔥 Studio - Scene Title s2 [1080p].mp4"),
            (2, "Studio - Scene Title", "studio scene title"),
        )
        parsed = split_parts.parse_split_part_title("Scene Title - Part 3 [720p].mkv")
        self.assertEqual(parsed[0], 3)
        self.assertEqual(parsed[2], "scene title")

    def test_does_not_parse_part_marker_inside_a_word(self):
        self.assertIsNone(split_parts.parse_split_part_title("Staxus - video.mp4"))


class TestSplitSceneSearch(unittest.TestCase):
    def test_accepts_unique_exact_title_and_combined_runtime(self):
        candidate = {
            "id": "stash-scene", "title": "Demo Scene", "duration": 1230,
            "details": "Description", "studio": {"name": "Demo Studio"},
            "performers": [{"performer": {"name": "Alex Sample"}}],
            "images": [], "urls": [], "tags": [],
        }
        with patch.object(split_parts, "_stashdb_candidates", return_value=[candidate]), \
             patch.object(split_parts, "_theporndb_candidates") as tpdb, \
             patch.dict("os.environ", {"STASHDB_API_KEY": "test-key"}):
            result = split_parts.search_split_scene("Demo Scene", duration=1200)

        self.assertIsNotNone(result)
        self.assertEqual(result.external_id, "stash-scene")
        self.assertEqual(result.duration, 1230)
        self.assertEqual(result.performers, ["Alex Sample"])
        tpdb.assert_not_called()

    def test_rejects_duration_mismatch_and_ambiguous_matches(self):
        first = {"id": "one", "title": "Demo Scene", "duration": 1200}
        second = {"id": "two", "title": "Demo Scene", "duration": 1201}
        with patch.object(split_parts, "_stashdb_candidates", return_value=[first, second]), \
             patch.object(split_parts, "_theporndb_candidates", return_value=[]):
            result = split_parts.search_split_scene("Demo Scene", duration=1200)
        self.assertIsNone(result)

        with patch.object(split_parts, "_stashdb_candidates", return_value=[
            {"id": "wrong-runtime", "title": "Demo Scene", "duration": 1231},
        ]), patch.object(split_parts, "_theporndb_candidates", return_value=[]):
            result = split_parts.search_split_scene("Demo Scene", duration=1200)
        self.assertIsNone(result)

    def test_non_exact_title_needs_independent_cast_or_studio_evidence(self):
        candidate = {
            "id": "stash-scene", "title": "Please Daddy Shock My Cock and Hole", "duration": 1200,
            "studio": {"name": "Bound Gods"},
            "performers": [{"performer": {"name": "Crush Daddy"}}],
        }
        with patch.object(split_parts, "_stashdb_candidates", return_value=[candidate]), \
             patch.object(split_parts, "_theporndb_candidates", return_value=[]):
            no_evidence = split_parts.search_split_scene(
                "Bound Gods Please Daddy Shock My Cock and Hole", duration=1200,
            )
            with_cast = split_parts.search_split_scene(
                "Please Daddy Shock My Cock and Hole", duration=1200,
                caption_performers=["Crush Daddy"],
            )
        self.assertIsNone(no_evidence)
        self.assertIsNotNone(with_cast)


class TestSplitSequenceBackfill(unittest.TestCase):
    def test_searches_complete_group_once_with_summed_runtime_and_updates_each_part(self):
        rows = [
            ("telegram:-1001:10:a", "Demo Scene s1", "Demo Scene s1.mp4", "Demo Scene s1", 600, {}, None, True),
            ("telegram:-1001:11:b", "Demo Scene s2", "Demo Scene s2.mp4", "Demo Scene s2", 615, {}, None, True),
        ]
        conn = _Conn(rows)
        scene = ScrapedScene(
            title="Demo Scene", duration=1215, performers=["Alex Sample"],
            studio="Demo Studio", source="stashdb:split_title_duration_v1",
            external_id="scene-1", url="https://stashdb.org/scenes/scene-1",
        )
        with patch("sync.scene_scraper_split_parts.search_split_scene", return_value=scene) as search, \
             patch.object(backfill._core, "_apply_matched_scene", return_value=True) as apply_scene, \
             patch.object(backfill._core, "record_attempt") as record_attempt:
            checked, updated = backfill._backfill_split_part_matches(
                conn, Path("/tmp"), limit=10, sleep_sec=0, http_client=None,
            )

        self.assertEqual((checked, updated), (2, 2))
        search.assert_called_once()
        self.assertEqual(search.call_args.args[0], "Demo Scene")
        self.assertEqual(search.call_args.kwargs["duration"], 1215)
        self.assertEqual(search.call_args.kwargs["duration_tolerance"], 30)
        self.assertEqual(apply_scene.call_count, 2)
        self.assertEqual(record_attempt.call_count, 2)

    def test_does_not_search_incomplete_group(self):
        rows = [
            ("telegram:-1001:10:a", "Demo Scene s1", "Demo Scene s1.mp4", "Demo Scene s1", 600, {}, None, True),
        ]
        conn = _Conn(rows)
        with patch("sync.scene_scraper_split_parts.search_split_scene") as search:
            checked, updated = backfill._backfill_split_part_matches(
                conn, Path("/tmp"), limit=10, sleep_sec=0, http_client=None,
            )
        self.assertEqual((checked, updated), (0, 0))
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
