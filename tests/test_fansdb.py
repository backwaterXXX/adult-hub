"""FansDB enrichment and scene search tests."""
import json
import os
import unittest
from unittest.mock import patch, MagicMock

try:
    from tests.pg_helper import PgTestCase
except ImportError:
    from pg_helper import PgTestCase

try:
    import sync.fansdb as fans_module
    FANS_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    FANS_AVAILABLE = False

from sync.scene_scraper_models import ScrapedScene
import sync.scene_scrapers as scene_scrapers
import sync.scene_scraper_fingerprint_sources as fp_sources
import sync.scene_scraper_text_sources as text_sources
import sync.scene_scraper_split_parts as split_parts


class TestFansDB(PgTestCase):
    def setUp(self):
        super().setUp()
        if not FANS_AVAILABLE:
            self.skipTest("fansdb module not available")

    def test_search_performer_no_token(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(fans_module.search_performer("Jordan Vale"))

    def test_search_performer_parses_graphql(self):
        payload = {
            "data": {
                "queryPerformers": {
                    "performers": [
                        {
                            "id": "fans-123",
                            "name": "Jordan Vale",
                            "disambiguation": "Creator bio test.",
                            "birth_date": "1995-05-05",
                            "country": "Poland",
                            "aliases": ["Jordan", "J. Vale"],
                            "images": [
                                {"url": "https://images.fansdb.cc/jordan-hd.jpg"}
                            ],
                        }
                    ]
                }
            }
        }
        res = fans_module.search_performer(
            "Jordan Vale", token="fans-token", http_post=lambda *a, **k: payload
        )
        self.assertIsNotNone(res)
        self.assertEqual(res["bio"], "Creator bio test.")
        self.assertEqual(res["birth"], "1995-05-05")
        self.assertEqual(res["nationality"], "Poland")
        self.assertIn("Jordan", res["aliases"])
        self.assertIn("J. Vale", res["aliases"])
        self.assertEqual(res["photo"], "https://images.fansdb.cc/jordan-hd.jpg")
        self.assertEqual(res["fansdb_id"], "fans-123")
        self.assertEqual(res["fansdb_url"], "https://fansdb.cc/performers/fans-123")

    def test_search_performer_rejects_unrelated_first_result(self):
        payload = {"data": {"queryPerformers": {"performers": [
            {"name": "Unrelated Creator", "disambiguation": "wrong bio"}
        ]}}}
        self.assertIsNone(fans_module.search_performer(
            "Requested Creator", token="fans-token", http_post=lambda *a, **k: payload))

    def test_backfill_fansdb_enriches_performers(self):
        self.conn.execute(
            "INSERT INTO performer_bios (name, bio, birth, aliases, updated_at, nationality) VALUES (%s, %s, %s, %s, %s, %s)",
            ("Jordan Vale", "", "", "[]", 100, ""),
        )
        self.conn.commit()

        payload = {
            "data": {
                "queryPerformers": {
                    "performers": [
                        {
                            "id": "fans-123",
                            "name": "Jordan Vale",
                            "disambiguation": "Updated creator bio.",
                            "birth_date": "1995-05-05",
                            "country": "Poland",
                            "aliases": ["Jordan V"],
                            "images": [
                                {"url": "https://images.fansdb.cc/jordan-hd.jpg"}
                            ],
                        }
                    ]
                }
            }
        }

        with patch.object(fans_module.time, "sleep"):
            checked, updated = fans_module.backfill_fansdb(
                self.conn, limit=10, sleep=0, token="test-token", fetch=lambda *a, **k: payload
            )

        self.assertEqual(checked, 1)
        self.assertEqual(updated, 1)

        row = self.conn.execute(
            "SELECT bio, birth, nationality, aliases FROM performer_bios WHERE name = 'Jordan Vale'"
        ).fetchone()
        self.assertEqual(row[0], "Updated creator bio.")
        self.assertEqual(row[1], "1995-05-05")
        self.assertEqual(row[2], "Poland")
        self.assertIn("Jordan V", json.loads(row[3]))

        photo = self.conn.execute(
            "SELECT thumb FROM performer_photos WHERE name = 'Jordan Vale'"
        ).fetchone()
        self.assertEqual(photo[0], "https://images.fansdb.cc/jordan-hd.jpg")

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

        with patch.object(fans_module.time, "sleep"):
            self.assertEqual(fans_module.backfill_fansdb(
                self.conn, limit=1, sleep=0, token="test-token", fetch=no_match), (1, 0))
            self.assertEqual(fans_module.backfill_fansdb(
                self.conn, limit=1, sleep=0, token="test-token", fetch=no_match), (1, 0))
        self.assertEqual(checked_names, ["Alice", "Bob"])


class TestFansDBEnrichmentErrors(unittest.TestCase):
    def setUp(self):
        super().setUp()
        if not FANS_AVAILABLE:
            self.skipTest("fansdb module not available")

    def test_http_429_treated_as_error_with_retry(self):
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 429

        with patch.object(fans_module, "_candidate_rows", return_value=[("Jordan Vale",)]), \
             patch.object(fans_module, "record_attempt") as mock_record:
            fans_module.backfill_fansdb(
                mock_conn, limit=1, sleep=0, token="test-token", fetch=lambda *a, **k: mock_resp
            )

        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        self.assertEqual(args[1], "performer")
        self.assertEqual(args[3], "fansdb")
        self.assertEqual(args[4], "error")
        self.assertNotEqual(args[4], "no_match")

    def test_http_503_treated_as_error_with_retry(self):
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 503

        with patch.object(fans_module, "_candidate_rows", return_value=[("Jordan Vale",)]), \
             patch.object(fans_module, "record_attempt") as mock_record:
            fans_module.backfill_fansdb(
                mock_conn, limit=1, sleep=0, token="test-token", fetch=lambda *a, **k: mock_resp
            )

        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        self.assertEqual(args[4], "error")
        self.assertNotEqual(args[4], "no_match")

    def test_graphql_errors_treated_as_error_with_retry(self):
        mock_conn = MagicMock()
        payload = {"errors": [{"message": "Rate limit exceeded"}]}

        with patch.object(fans_module, "_candidate_rows", return_value=[("Jordan Vale",)]), \
             patch.object(fans_module, "record_attempt") as mock_record:
            fans_module.backfill_fansdb(
                mock_conn, limit=1, sleep=0, token="test-token", fetch=lambda *a, **k: payload
            )

        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        self.assertEqual(args[4], "error")
        self.assertNotEqual(args[4], "no_match")

    def test_candidate_db_query_failure_surfaces_to_caller(self):
        mock_conn = MagicMock()
        with patch.object(fans_module, "_candidate_rows", side_effect=RuntimeError("DB query timeout")):
            with self.assertRaises(RuntimeError) as ctx:
                fans_module.backfill_fansdb(mock_conn, limit=1, sleep=0, token="test-token")
            self.assertIn("DB query timeout", str(ctx.exception))

    def test_valid_empty_result_still_records_no_match(self):
        mock_conn = MagicMock()
        payload = {"data": {"queryPerformers": {"performers": []}}}

        with patch.object(fans_module, "_candidate_rows", return_value=[("Jordan Vale",)]), \
             patch.object(fans_module, "record_attempt") as mock_record:
            fans_module.backfill_fansdb(
                mock_conn, limit=1, sleep=0, token="test-token", fetch=lambda *a, **k: payload
            )

        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        self.assertEqual(args[4], "no_match")

    def test_http_500_treated_as_error_with_retry(self):
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch.object(fans_module, "_candidate_rows", return_value=[("Jordan Vale",)]), \
             patch.object(fans_module, "record_attempt") as mock_record:
            fans_module.backfill_fansdb(
                mock_conn, limit=1, sleep=0, token="test-token", fetch=lambda *a, **k: mock_resp
            )

        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        self.assertEqual(args[4], "error")

    def test_http_400_treated_as_error_with_retry(self):
        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 400

        with patch.object(fans_module, "_candidate_rows", return_value=[("Jordan Vale",)]), \
             patch.object(fans_module, "record_attempt") as mock_record:
            fans_module.backfill_fansdb(
                mock_conn, limit=1, sleep=0, token="test-token", fetch=lambda *a, **k: mock_resp
            )

        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        self.assertEqual(args[4], "error")

    def test_malformed_missing_data_treated_as_error(self):
        mock_conn = MagicMock()
        payload = {"not_data": {}}

        with patch.object(fans_module, "_candidate_rows", return_value=[("Jordan Vale",)]), \
             patch.object(fans_module, "record_attempt") as mock_record:
            fans_module.backfill_fansdb(
                mock_conn, limit=1, sleep=0, token="test-token", fetch=lambda *a, **k: payload
            )

        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        self.assertEqual(args[4], "error")

    def test_malformed_missing_performers_treated_as_error(self):
        mock_conn = MagicMock()
        payload = {"data": {"queryPerformers": {"performers": None}}}

        with patch.object(fans_module, "_candidate_rows", return_value=[("Jordan Vale",)]), \
             patch.object(fans_module, "record_attempt") as mock_record:
            fans_module.backfill_fansdb(
                mock_conn, limit=1, sleep=0, token="test-token", fetch=lambda *a, **k: payload
            )

        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        self.assertEqual(args[4], "error")

    def test_unrelated_name_treated_as_legitimate_no_match(self):
        mock_conn = MagicMock()
        payload = {"data": {"queryPerformers": {"performers": [{"name": "Completely Different Performer"}]}}}

        with patch.object(fans_module, "_candidate_rows", return_value=[("Jordan Vale",)]), \
             patch.object(fans_module, "record_attempt") as mock_record:
            fans_module.backfill_fansdb(
                mock_conn, limit=1, sleep=0, token="test-token", fetch=lambda *a, **k: payload
            )

        mock_record.assert_called_once()
        args = mock_record.call_args[0]
        self.assertEqual(args[4], "no_match")


class TestFansDBSceneSearch(unittest.TestCase):
    def test_search_fansdb_scene_by_fingerprint_accepts_close_phash(self):
        scene_data = {
            "id": "fans-scene-1",
            "title": "Creator Video #1",
            "details": "Details of the creator video",
            "date": "2024-01-01",
            "duration": 600,
            "studio": {"name": "OnlyFans"},
            "performers": [{"performer": {"name": "Creator One"}}],
            "tags": [{"name": "Solo"}],
            "images": [{"url": "https://images.fansdb.cc/scene1.jpg"}],
            "urls": [{"url": "https://fansdb.cc/scenes/fans-scene-1"}],
            "fingerprints": [{"algorithm": "PHASH", "hash": "0000000000000001"}],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": {
                "findScenesBySceneFingerprints": [[scene_data]]
            }
        }
        mock_resp.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(fp_sources._core, "request_with_host_limit", return_value=mock_resp):
            scene = fp_sources.search_fansdb_scene_by_fingerprint(
                "0000000000000000", "PHASH", http_client=MagicMock(),
                max_distance=2, duration=600, duration_tolerance=2,
            )
        self.assertIsNotNone(scene)
        self.assertEqual(scene.external_id, "fans-scene-1")
        self.assertEqual(scene.title, "Creator Video #1")
        self.assertEqual(scene.source, "fansdb:phash")
        self.assertEqual(scene.poster_url, "https://images.fansdb.cc/scene1.jpg")

    def test_search_scene_by_oshash_falls_back_to_fansdb(self):
        expected_scene = ScrapedScene(
            title="FansDB Scene", plot="", date="2024-01-01", duration=300,
            poster_url="", performers=[], tags=[], studio="", external_id="fans-oshash-1",
            url="https://fansdb.cc/scenes/fans-oshash-1", confidence=1.0, source="fansdb:oshash",
        )
        with patch.object(scene_scrapers, "_search_stashdb_scene_by_oshash", return_value=None), \
             patch.object(scene_scrapers, "_search_fansdb_scene_by_oshash", return_value=expected_scene), \
             patch.object(scene_scrapers, "_search_tpdb_scene_by_oshash") as tpdb_mock:
            scene = scene_scrapers.search_scene_by_oshash(
                "0123456789abcdef", duration=300,
            )
        self.assertIsNotNone(scene)
        self.assertEqual(scene.external_id, "fans-oshash-1")
        self.assertEqual(scene.source, "fansdb:oshash")
        tpdb_mock.assert_not_called()

    def test_search_fansdb_scene_by_exact_title(self):
        scene_data = {
            "id": "fans-exact-1",
            "title": "Unique Creator Scene",
            "details": "Great video description",
            "date": "2024-02-02",
            "duration": 500,
            "studio": {"name": "Fansly"},
            "performers": [{"performer": {"name": "Star Model"}}],
            "tags": [{"name": "POV"}],
            "images": [{"url": "https://images.fansdb.cc/exact1.jpg"}],
            "urls": [],
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": {
                "searchScenes": {
                    "count": 1,
                    "scenes": [scene_data],
                }
            }
        }
        mock_resp.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(text_sources._core, "request_with_host_limit", return_value=mock_resp):
            scene = text_sources.search_fansdb_scene_by_exact_title(
                "Unique Creator Scene", duration=500, http_client=MagicMock(),
            )
        self.assertIsNotNone(scene)
        self.assertEqual(scene.external_id, "fans-exact-1")
        self.assertEqual(scene.title, "Unique Creator Scene")
        self.assertEqual(scene.source, "fansdb:title_exact")
        self.assertEqual(scene.performers, ["Star Model"])

    def test_search_scene_by_exact_title_fansdb_fallback(self):
        expected_scene = ScrapedScene(
            title="Fans Exact Title", plot="", date="2024-02-02", duration=400,
            poster_url="", performers=[], tags=[], studio="", external_id="fans-text-1",
            url="https://fansdb.cc/scenes/fans-text-1", confidence=0.99, source="fansdb:title_exact",
        )
        with patch.object(text_sources, "search_stashdb_scene_by_exact_title", return_value=None), \
             patch.object(text_sources, "search_fansdb_scene_by_exact_title", return_value=expected_scene), \
             patch.object(text_sources, "search_tpdb_scene_by_exact_title") as tpdb_mock:
            scene = text_sources.search_scene_by_exact_title("Fans Exact Title", duration=400)
        self.assertIsNotNone(scene)
        self.assertEqual(scene.external_id, "fans-text-1")
        self.assertEqual(scene.source, "fansdb:title_exact")
        tpdb_mock.assert_not_called()

    def test_split_parts_fansdb_candidates(self):
        candidate = {
            "id": "fans-split-scene", "title": "Split Creator Scene", "duration": 1230,
            "details": "Description", "studio": {"name": "OnlyFans"},
            "performers": [{"performer": {"name": "Creator Star"}}],
            "images": [], "urls": [], "tags": [],
        }
        with patch.object(split_parts, "_stashdb_candidates", return_value=[]), \
             patch.object(split_parts, "_fansdb_candidates", return_value=[candidate]), \
             patch.object(split_parts, "_theporndb_candidates") as tpdb, \
             patch.dict("os.environ", {"FANSDB_API_KEY": "test-key"}):
            result = split_parts.search_split_scene("Split Creator Scene", duration=1200)

        self.assertIsNotNone(result)
        self.assertEqual(result.external_id, "fans-split-scene")
        self.assertEqual(result.source, "fansdb:split_title_duration_v1")
        self.assertEqual(result.performers, ["Creator Star"])
        tpdb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
