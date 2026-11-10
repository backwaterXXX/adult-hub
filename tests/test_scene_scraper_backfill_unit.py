import json
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

import sync.scene_scraper_backfill as backfill
from sync.scene_scrapers import ScrapedScene
import sync.scene_scraper_text_sources as scene_scraper_text_sources


class _Cursor:
    def __init__(self, rows=(), rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return self.rows


class _FakeConn:
    def __init__(self, video_rows=(), people=(), aliases=()):
        self.video_rows = list(video_rows)
        self.people = list(people)
        self.aliases = list(aliases)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if ("SELECT id,source_text,performer_resolution" in query
                or "SELECT v.id,v.source_text,v.duration" in query):
            return _Cursor(self.video_rows)
        if "SELECT id,name FROM performer_catalog" in query:
            return _Cursor(self.people)
        if "SELECT a.performer_id,a.alias" in query:
            return _Cursor(self.aliases)
        if "UPDATE videos SET performer_resolution" in query:
            return _Cursor(rowcount=1)
        raise AssertionError(f"Unexpected SQL: {query}")


class TestStashdbCaptionBackfill(unittest.TestCase):
    def test_uses_only_official_catalog_and_saves_exact_match_evidence(self):
        video_id = "telegram:channel:1:file"
        conn = _FakeConn(
            video_rows=[(video_id, "Featuring Alex Sample", {"method": "legacy", "source": "telegram"})],
            people=[("stashdb:person-1", "Alex Sample")],
            aliases=[("stashdb:person-1", "Alex Sample")],
        )

        checked, updated = backfill._backfill_stashdb_caption_matches(conn, limit=10)

        self.assertEqual((checked, updated), (1, 1))
        update = next((params for query, params in conn.calls if "UPDATE videos SET performer_resolution" in query), None)
        self.assertIsNotNone(update)
        resolution = json.loads(update[0])
        self.assertEqual(resolution["method"], "stashdb_caption_alias_v1")
        self.assertEqual(resolution["performer_ids"], ["stashdb:person-1"])
        self.assertEqual(resolution["performer_names"], ["Alex Sample"])
        self.assertEqual(json.loads(update[2]), ["Alex Sample"])
        self.assertIn("a.origin='stashdb' AND a.approved=TRUE", " ".join(q for q, _ in conn.calls))


class TestCaptionTitleBackfill(unittest.TestCase):
    def test_requires_exact_title_and_runtime_search_result(self):
        conn = _FakeConn(video_rows=[("telegram:channel:2:file", "Title: Demo Scene\nCast: Alex Sample", 720)])
        scene = ScrapedScene(
            title="Demo Scene", plot="", date="", duration=720, poster_url="",
            performers=["Alex Sample"], tags=[], studio="Demo Studio",
            external_id="scene-1", url="https://stashdb.org/scenes/scene-1",
            confidence=0.99, source="stashdb:title_exact",
        )

        with patch.object(backfill._core, "record_attempt") as record_attempt, \
             patch.object(backfill._core, "_apply_matched_scene", return_value=True) as apply_scene, \
             patch.object(scene_scraper_text_sources, "search_scene_by_exact_title", return_value=scene) as search:
            checked, updated = backfill._backfill_caption_title_matches(
                conn, Path("/tmp"), limit=5, sleep_sec=0, http_client=None,
            )

        self.assertEqual((checked, updated), (1, 1))
        search.assert_called_once_with("Demo Scene", duration=720, http_client=None, duration_tolerance=2)
        apply_scene.assert_called_once()
        self.assertEqual(record_attempt.call_args.args[4], "found")


if __name__ == "__main__":
    unittest.main()
