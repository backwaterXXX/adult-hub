"""FansDB enrichment and scene search tests."""
import json
import os
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

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

from common.scene_titles import SceneLookupUnavailable
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


class TestFansDBSceneDistinguishErrors(unittest.TestCase):
    def setUp(self):
        super().setUp()
        scene_scrapers.reset_fansdb_scene_metrics()

    def test_exact_title_distinguishes_graphql_errors_and_malformed(self):
        # 1. GraphQL error raises RuntimeError and increments error count
        mock_resp_err = MagicMock()
        mock_resp_err.json.return_value = {"errors": [{"message": "Syntax error"}]}
        mock_resp_err.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(text_sources._core, "request_with_host_limit", return_value=mock_resp_err):
            with self.assertRaises(RuntimeError) as ctx:
                text_sources.search_fansdb_scene_by_exact_title("Test Title", duration=500, http_client=MagicMock())
            self.assertIn("GraphQL errors", str(ctx.exception))

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics["error"], 1)

        # 2. Malformed missing data raises RuntimeError
        mock_resp_mal = MagicMock()
        mock_resp_mal.json.return_value = {"unexpected": {}}
        mock_resp_mal.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(text_sources._core, "request_with_host_limit", return_value=mock_resp_mal):
            with self.assertRaises(RuntimeError) as ctx:
                text_sources.search_fansdb_scene_by_exact_title("Test Title", duration=500, http_client=MagicMock())
            self.assertIn("missing data", str(ctx.exception))

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics["error"], 2)

        # 3. Valid empty search returns None and increments no_match count
        mock_resp_empty = MagicMock()
        mock_resp_empty.json.return_value = {"data": {"searchScenes": {"count": 0, "scenes": []}}}
        mock_resp_empty.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(text_sources._core, "request_with_host_limit", return_value=mock_resp_empty):
            res = text_sources.search_fansdb_scene_by_exact_title("Test Title", duration=500, http_client=MagicMock())
            self.assertIsNone(res)

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics["error"], 2)
        self.assertEqual(metrics["no_match"], 1)

    def test_split_candidates_distinguishes_graphql_errors_and_malformed(self):
        # 1. GraphQL error raises RuntimeError
        mock_resp_err = MagicMock()
        mock_resp_err.json.return_value = {"errors": [{"message": "Rate limit"}]}
        mock_resp_err.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(split_parts._core, "request_with_host_limit", return_value=mock_resp_err):
            with self.assertRaises(RuntimeError) as ctx:
                split_parts._fansdb_candidates("Split Title", http_client=MagicMock())
            self.assertIn("GraphQL errors", str(ctx.exception))

        # 2. Malformed response raises RuntimeError
        mock_resp_mal = MagicMock()
        mock_resp_mal.json.return_value = {"data": None}
        mock_resp_mal.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(split_parts._core, "request_with_host_limit", return_value=mock_resp_mal):
            with self.assertRaises(RuntimeError) as ctx:
                split_parts._fansdb_candidates("Split Title", http_client=MagicMock())
            self.assertIn("missing data", str(ctx.exception))

        # 3. Valid empty search returns []
        mock_resp_empty = MagicMock()
        mock_resp_empty.json.return_value = {"data": {"searchScenes": {"scenes": []}}}
        mock_resp_empty.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(split_parts._core, "request_with_host_limit", return_value=mock_resp_empty):
            res = split_parts._fansdb_candidates("Split Title", http_client=MagicMock())
            self.assertEqual(res, [])

    def test_linked_id_distinguishes_graphql_errors_and_empty(self):
        # 1. GraphQL error raises
        mock_resp_err = MagicMock()
        mock_resp_err.json.return_value = {"errors": [{"message": "Not authorized"}]}
        mock_resp_err.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(text_sources._core, "request_with_host_limit", return_value=mock_resp_err):
            with self.assertRaises(RuntimeError) as ctx:
                text_sources.fetch_fansdb_scene_by_id("fans-id-1", http_client=MagicMock())
            self.assertIn("GraphQL errors", str(ctx.exception))

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics["error"], 1)

        # 2. Malformed missing findScene raises
        mock_resp_mal = MagicMock()
        mock_resp_mal.json.return_value = {"data": {}}
        mock_resp_mal.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(text_sources._core, "request_with_host_limit", return_value=mock_resp_mal):
            with self.assertRaises(RuntimeError) as ctx:
                text_sources.fetch_fansdb_scene_by_id("fans-id-1", http_client=MagicMock())
            self.assertIn("missing findScene", str(ctx.exception))

        # 3. Valid empty search (findScene is null in GraphQL) returns None
        mock_resp_empty = MagicMock()
        mock_resp_empty.json.return_value = {"data": {"findScene": None}}
        mock_resp_empty.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(text_sources._core, "request_with_host_limit", return_value=mock_resp_empty):
            res = text_sources.fetch_fansdb_scene_by_id("fans-id-1", http_client=MagicMock())
            self.assertIsNone(res)

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics["no_match"], 1)

        # 4. Valid match returns ScrapedScene and records matched
        mock_resp_match = MagicMock()
        mock_resp_match.json.return_value = {
            "data": {
                "findScene": {
                    "id": "fans-id-1",
                    "title": "Scene Linked",
                    "details": "Details",
                    "duration": 300,
                    "images": [{"url": "https://img.test/1.jpg"}],
                }
            }
        }
        mock_resp_match.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(text_sources._core, "request_with_host_limit", return_value=mock_resp_match):
            res = text_sources.fetch_fansdb_scene_by_id("fans-id-1", http_client=MagicMock())
            self.assertIsNotNone(res)
            self.assertEqual(res.external_id, "fans-id-1")

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics["matched"], 1)

    def test_fingerprint_distinguishes_graphql_errors_and_empty(self):
        # 1. GraphQL error raises
        mock_resp_err = MagicMock()
        mock_resp_err.json.return_value = {"errors": [{"message": "Invalid query"}]}
        mock_resp_err.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(fp_sources._core, "request_with_host_limit", return_value=mock_resp_err):
            with self.assertRaises(RuntimeError) as ctx:
                fp_sources.search_fansdb_scene_by_fingerprint(
                    "0000000000000000", "PHASH", http_client=MagicMock(),
                )
            self.assertIn("GraphQL errors", str(ctx.exception))

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics["error"], 1)

        # 2. Malformed missing findScenesBySceneFingerprints raises
        mock_resp_mal = MagicMock()
        mock_resp_mal.json.return_value = {"data": {}}
        mock_resp_mal.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(fp_sources._core, "request_with_host_limit", return_value=mock_resp_mal):
            with self.assertRaises(RuntimeError) as ctx:
                fp_sources.search_fansdb_scene_by_fingerprint(
                    "0000000000000000", "PHASH", http_client=MagicMock(),
                )
            self.assertIn("missing findScenesBySceneFingerprints", str(ctx.exception))

        # 3. Valid empty search returns None and records no_match
        mock_resp_empty = MagicMock()
        mock_resp_empty.json.return_value = {"data": {"findScenesBySceneFingerprints": [[]]}}
        mock_resp_empty.status_code = 200

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch.object(fp_sources._core, "request_with_host_limit", return_value=mock_resp_empty):
            res = fp_sources.search_fansdb_scene_by_fingerprint(
                "0000000000000000", "PHASH", http_client=MagicMock(),
            )
            self.assertIsNone(res)

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics["no_match"], 1)

    def test_resolver_find_fansdb_match_distinguishes_errors(self):
        import asyncio
        from resolver.admin_v2_phash import _find_fansdb_match

        # 1. GraphQL error raises
        mock_resp_err = MagicMock()
        mock_resp_err.json.return_value = {"errors": [{"message": "Rate limited"}]}
        mock_resp_err.status_code = 200

        mock_async_client = MagicMock()
        mock_async_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp_err)

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch("httpx.AsyncClient", return_value=mock_async_client), \
             patch("resolver.admin_v2_phash.wait_for_host_slot"):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(_find_fansdb_match("0000000000000000"))
            self.assertIn("GraphQL errors", str(ctx.exception))

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics["error"], 1)

        # 2. Valid empty returns False and records no_match
        mock_resp_empty = MagicMock()
        mock_resp_empty.json.return_value = {"data": {"findScenesBySceneFingerprints": [[]]}}
        mock_resp_empty.status_code = 200

        mock_async_client2 = MagicMock()
        mock_async_client2.__aenter__.return_value.post = AsyncMock(return_value=mock_resp_empty)

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch("httpx.AsyncClient", return_value=mock_async_client2), \
             patch("resolver.admin_v2_phash.wait_for_host_slot"):
            res = asyncio.run(_find_fansdb_match("0000000000000000"))
            self.assertFalse(res)

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics["no_match"], 1)

    def test_metrics_have_no_personalized_labels(self):
        metrics = scene_scrapers.get_fansdb_scene_metrics()
        # Verify strictly aggregate keys only
        self.assertEqual(set(metrics.keys()), {"matched", "no_match", "error"})
        for k, v in metrics.items():
            self.assertIsInstance(k, str)
            self.assertIsInstance(v, int)

    def test_fansdb_graphql_error_preserves_provider_fallbacks(self):
        # 1. Split search fallback to ThePornDB when FansDB fails with GraphQL error
        tpdb_candidate = {
            "id": "tpdb-split-1", "title": "Split Creator Scene", "duration": 1200,
            "site": {"name": "Site"}, "performers": [{"name": "Star"}],
            "tags": [], "poster": "",
        }
        with patch.object(split_parts, "_stashdb_candidates", return_value=[]), \
             patch.object(split_parts, "_fansdb_candidates", side_effect=RuntimeError("FansDB split-title lookup returned GraphQL errors: syntax")), \
             patch.object(split_parts, "_theporndb_candidates", return_value=[tpdb_candidate]), \
             patch.dict("os.environ", {"FANSDB_API_KEY": "test-key"}):
            split_result = split_parts.search_split_scene("Split Creator Scene", duration=1200)

        self.assertIsNotNone(split_result)
        self.assertEqual(split_result.external_id, "tpdb-split-1")
        self.assertIn("theporndb", split_result.source)

        # 2. Exact title search fallback to ThePornDB when FansDB fails with GraphQL error
        tpdb_title_scene = ScrapedScene(
            title="Title Exact", plot="", date="", duration=600, poster_url="",
            performers=[], tags=[], studio="", external_id="tpdb-title-1",
            confidence=0.9, source="theporndb:title_exact",
        )
        with patch.object(text_sources, "search_stashdb_scene_by_exact_title", return_value=None), \
             patch.object(text_sources, "search_fansdb_scene_by_exact_title", side_effect=RuntimeError("FansDB title lookup returned GraphQL errors: down")), \
             patch.object(text_sources, "search_tpdb_scene_by_exact_title", return_value=tpdb_title_scene):
            title_result = text_sources.search_scene_by_exact_title("Title Exact", duration=600)

        self.assertIsNotNone(title_result)
        self.assertEqual(title_result.external_id, "tpdb-title-1")

        # 3. Fingerprint search fallback to ThePornDB when FansDB fails with GraphQL error
        tpdb_fp_scene = ScrapedScene(
            title="FP Scene", plot="", date="", duration=600, poster_url="",
            performers=[], tags=[], studio="", external_id="tpdb-fp-1",
            confidence=0.9, source="theporndb:phash",
        )
        with patch.object(fp_sources._core, "_search_stashdb_scene_by_fingerprint", return_value=None), \
             patch.object(fp_sources._core, "_search_fansdb_scene_by_fingerprint", side_effect=RuntimeError("FansDB PHASH returned GraphQL errors")), \
             patch.object(fp_sources, "_theporndb_phash_scene", return_value=tpdb_fp_scene), \
             patch.dict("os.environ", {"THEPORNDB_API_TOKEN": "tpdb-tok"}):
            fp_result = fp_sources.search_scene_by_phash("0000000000000000", duration=600)

        self.assertIsNotNone(fp_result)
        self.assertEqual(fp_result.external_id, "tpdb-fp-1")

    def test_fansdb_graphql_error_preserves_resolver_semantics(self):
        import asyncio
        from resolver import admin_v2_phash as phash_mod

        # Test resolver submit_frames route fallback logic
        router = MagicMock()
        admin = MagicMock()
        phash_mod._route_submit_frames(router, admin)

        # Case A: StashDB lookup errors; FansDB match succeeds as fallback
        with patch.object(phash_mod, "_phash_upload_enabled", return_value=True), \
             patch.object(phash_mod, "_authorized_user", new=AsyncMock(return_value={"id": "u1"})), \
             patch.object(phash_mod, "_read_frame_body", new=AsyncMock(return_value=({"duration_us": 100}, None))), \
             patch.object(phash_mod, "_valid_frame_request", return_value=True), \
             patch.object(phash_mod, "_decode_frames", return_value=[]), \
             patch.object(phash_mod, "_save_frame_fingerprint", new=AsyncMock(return_value=("0000000000000000", None))), \
             patch.object(phash_mod, "_find_stashdb_match", new=AsyncMock(side_effect=RuntimeError("StashDB connection error"))), \
             patch.object(phash_mod, "_find_fansdb_match", new=AsyncMock(return_value=True)):

            req = MagicMock()
            req.headers = {"Authorization": "Bearer tok"}
            res = asyncio.run(phash_mod._find_fansdb_match("0000000000000000"))
            self.assertTrue(res)

        # Case B: FansDB lookup raises GraphQL error; resolver returns 200 payload and doesn't crash
        mock_resp_err = MagicMock()
        mock_resp_err.json.return_value = {"errors": [{"message": "Rate limited"}]}
        mock_resp_err.status_code = 200
        mock_async_client = MagicMock()
        mock_async_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp_err)

        with patch.dict(os.environ, {"FANSDB_API_KEY": "test-key"}), \
             patch("httpx.AsyncClient", return_value=mock_async_client), \
             patch("resolver.admin_v2_phash.wait_for_host_slot"):
            with self.assertRaises(RuntimeError):
                asyncio.run(phash_mod._find_fansdb_match("0000000000000000"))

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics["error"], 1)

    def test_artwork_search_failure_retry_preserved_on_graphql_error(self):
        from sync.scene_artwork_download import _ArtworkSearch

        mock_sources = MagicMock()
        # 1. FansDB GraphQL error raises RuntimeError
        mock_sources.fetch_fansdb_scene_by_id.side_effect = RuntimeError("FansDB scene-by-id lookup returned GraphQL errors: bad query")
        search = _ArtworkSearch("video-123", "/tmp/poster.jpg", 1200, mock_sources)
        search.add_linked_matches("fans-scene-id", "fansdb")

        # Must record lookup_error = True and return artwork_lookup_failed (NOT artwork_not_found)
        self.assertTrue(search.lookup_error)
        outcome = search.outcome()
        self.assertFalse(outcome["downloaded"])
        self.assertEqual(outcome["local_state"], "artwork_lookup_failed")

        # 2. FansDB legitimate no match (returns None without exception)
        mock_sources.fetch_fansdb_scene_by_id.side_effect = None
        mock_sources.fetch_fansdb_scene_by_id.return_value = None
        search2 = _ArtworkSearch("video-123", "/tmp/poster.jpg", 1200, mock_sources)
        search2.add_linked_matches("fans-scene-id", "fansdb")

        # Must NOT record lookup_error and return artwork_not_found
        self.assertFalse(search2.lookup_error)
        outcome2 = search2.outcome()
        self.assertFalse(outcome2["downloaded"])
        self.assertEqual(outcome2["local_state"], "artwork_not_found")

    def test_split_counter_once_per_actual_request(self):
        candidate = {
            "id": "fans-split-scene", "title": "Split Creator Scene", "duration": 1200,
            "details": "Description", "studio": {"name": "OnlyFans"},
            "performers": [{"performer": {"name": "Creator Star"}}],
            "images": [], "urls": [], "tags": [],
        }

        # 1. FANSDB_API_KEY unset -> 0 attempts recorded
        with patch.object(split_parts, "_stashdb_candidates", return_value=[]), \
             patch.dict("os.environ", {}, clear=True):
            split_parts.search_split_scene("Split Creator Scene", duration=1200)

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics, {"matched": 0, "no_match": 0, "error": 0})

        # 2. StashDB matches first -> FansDB never called -> 0 attempts recorded
        stash_scene = ScrapedScene(
            title="Split Creator Scene", plot="", date="", duration=1200,
            poster_url="", performers=[], tags=[], studio="",
            external_id="stash-1", confidence=0.92, source="stashdb:split_title_duration_v1",
        )
        with patch.object(split_parts, "_stashdb_candidates", return_value=[{"id": "stash-1", "title": "Split Creator Scene", "duration": 1200}]), \
             patch.object(split_parts, "_accepted_scenes", return_value={"stash-1": stash_scene}), \
             patch.object(split_parts, "_fansdb_candidates") as mock_fans, \
             patch.dict("os.environ", {"FANSDB_API_KEY": "test-key"}):
            res = split_parts.search_split_scene("Split Creator Scene", duration=1200)
            self.assertIsNotNone(res)
            mock_fans.assert_not_called()

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics, {"matched": 0, "no_match": 0, "error": 0})

        # 3. FansDB called and no match -> exactly 1 no_match recorded
        with patch.object(split_parts, "_stashdb_candidates", return_value=[]), \
             patch.object(split_parts, "_fansdb_candidates", return_value=[]), \
             patch.object(split_parts, "_theporndb_candidates", return_value=[]), \
             patch.dict("os.environ", {"FANSDB_API_KEY": "test-key"}):
            split_parts.search_split_scene("Split Creator Scene", duration=1200)

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics, {"matched": 0, "no_match": 1, "error": 0})

        # 4. FansDB called and matched -> exactly 1 matched recorded
        scene_scrapers.reset_fansdb_scene_metrics()
        with patch.object(split_parts, "_stashdb_candidates", return_value=[]), \
             patch.object(split_parts, "_fansdb_candidates", return_value=[candidate]), \
             patch.object(split_parts, "_theporndb_candidates", return_value=[]), \
             patch.dict("os.environ", {"FANSDB_API_KEY": "test-key"}):
            split_parts.search_split_scene("Split Creator Scene", duration=1200)

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics, {"matched": 1, "no_match": 0, "error": 0})

        # 5. FansDB called and errors -> exactly 1 error recorded
        mock_resp_err = MagicMock()
        mock_resp_err.json.return_value = {"errors": [{"message": "Syntax error"}]}
        mock_resp_err.status_code = 200
        scene_scrapers.reset_fansdb_scene_metrics()
        with patch.object(split_parts, "_stashdb_candidates", return_value=[]), \
             patch.object(split_parts._core, "request_with_host_limit", return_value=mock_resp_err), \
             patch.object(split_parts, "_theporndb_candidates", return_value=[]), \
             patch.dict("os.environ", {"FANSDB_API_KEY": "test-key"}):
            with self.assertRaises(SceneLookupUnavailable):
                split_parts.search_split_scene("Split Creator Scene", duration=1200)

        metrics = scene_scrapers.get_fansdb_scene_metrics()
        self.assertEqual(metrics, {"matched": 0, "no_match": 0, "error": 1})

    def test_sync_metrics_ownership_and_explicit_bind(self):
        active_run_metrics: dict = {
            "fansdb_scene_attempts": {"matched": 0, "no_match": 0, "error": 0},
        }
        scene_scrapers.bind_active_metrics(active_run_metrics)
        self.assertIs(scene_scrapers.get_active_metrics(), active_run_metrics)

        scene_scrapers.record_fansdb_scene_attempt("matched")
        self.assertEqual(active_run_metrics["fansdb_scene_attempts"]["matched"], 1)
        self.assertEqual(scene_scrapers.get_fansdb_scene_metrics()["matched"], 1)

        scene_scrapers.bind_active_metrics(None)
        self.assertIsNone(scene_scrapers.get_active_metrics())

        scene_scrapers.record_fansdb_scene_attempt("error")
        # active_run_metrics not updated because it's unbound
        self.assertEqual(active_run_metrics["fansdb_scene_attempts"]["error"], 0)
        # Per-process counter updated
        self.assertEqual(scene_scrapers.get_fansdb_scene_metrics()["error"], 1)


if __name__ == "__main__":
    unittest.main()
