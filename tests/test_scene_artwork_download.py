"""Tests for admin-triggered scene artwork download path selection."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sync"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import scene_scraper_fingerprint_sources
import scene_scraper_media_files
import scene_scraper_text_sources
from pg_helper import PgTestCase

import scene_artwork_download


def _seed_video(conn, tmp_path: Path, video_id: str = "telegram:123", *,
                match_source=None, external_id=None) -> Path:
    nested = tmp_path / "adult" / "telegram"
    nested.mkdir(parents=True, exist_ok=True)
    strm = nested / f"{video_id.replace(':', '_')}.strm"
    strm.write_text("http://example/stream\n", encoding="utf-8")
    with conn:
        conn.execute(
            "INSERT INTO videos (id, provider, title, duration, scene_match_source, "
            "scene_match_external_id) VALUES (%s, %s, %s, %s, %s, %s)",
            (video_id, "telegram", "Matched Scene Title", 600, match_source, external_id),
        )
        conn.execute(
            "INSERT INTO synced_files (file_path, video_id) VALUES (%s, %s)",
            (f"adult/telegram/{strm.name}", video_id),
        )
    return strm


class ArtworkPathTests(PgTestCase):
    def _seed(self, tmp_path: Path, video_id: str = "telegram:123"):
        return _seed_video(self.conn, tmp_path, video_id)

    def test_nested_strm_receives_artwork_beside_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            strm = self._seed(tmp)
            calls = []

            def fake_download(poster_url, artwork_path, video_id):
                calls.append((str(poster_url), Path(artwork_path), video_id))
                Path(artwork_path).write_bytes(b"fake-jpeg")
                return True

            scene = SimpleNamespace(poster_url="https://example.com/poster.jpg",
                                    source="theporndb")
            with patch.object(scene_scraper_text_sources, "search_scene_by_exact_title",
                              return_value=scene), \
                 patch.object(scene_scraper_media_files, "_download_scene_poster",
                              side_effect=fake_download):
                result = scene_artwork_download.download_artwork_for_video(
                    self.conn, tmp, "telegram:123")

            self.assertEqual(result, {"downloaded": True, "provider": "ThePornDB"})
            self.assertEqual(calls, [(scene.poster_url, strm.with_suffix(".jpg"),
                                      "telegram:123")])
            self.assertTrue(strm.with_suffix(".jpg").is_file())

    def test_symlinked_artwork_target_is_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            strm = self._seed(tmp)
            outside = tmp.parent / f"outside-{id(tmp)}.jpg"
            outside.write_bytes(b"")
            try:
                strm.with_suffix(".jpg").symlink_to(outside)
                with self.assertRaises(ValueError) as ctx:
                    scene_artwork_download.download_artwork_for_video(
                        self.conn, tmp, "telegram:123")
                self.assertIn("not found in library", str(ctx.exception))
            finally:
                outside.unlink(missing_ok=True)

    def test_escaping_file_path_is_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._seed(tmp)
            with self.conn:
                self.conn.execute(
                    "UPDATE synced_files SET file_path = %s",
                    ("../secret.strm",))
            with self.assertRaises(ValueError):
                scene_artwork_download.download_artwork_for_video(
                    self.conn, tmp, "telegram:123")


class ArtworkProviderBranchTests(PgTestCase):
    """Cover the linked-ID, OSHASH, and title search branches of the lookup."""

    def _poster_ok(self):
        def fake_download(poster_url, artwork_path, video_id):
            Path(artwork_path).write_bytes(b"fake-jpeg")
            return True
        return fake_download

    def _scene(self, source):
        return SimpleNamespace(poster_url="https://example.com/poster.jpg", source=source)

    def test_linked_stashdb_id_fetches_only_stashdb(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _seed_video(self.conn, tmp, match_source="StashDB", external_id="scene-1")
            with patch.object(scene_scraper_text_sources, "fetch_stashdb_scene_by_id",
                              return_value=self._scene("stashdb")) as stash, \
                 patch.object(scene_scraper_text_sources, "fetch_tpdb_scene_by_id") as tpdb, \
                 patch.object(scene_scraper_media_files, "_download_scene_poster",
                              side_effect=self._poster_ok()):
                result = scene_artwork_download.download_artwork_for_video(
                    self.conn, tmp, "telegram:123")
            self.assertEqual(result, {"downloaded": True, "provider": "StashDB"})
            stash.assert_called_once_with("scene-1")
            tpdb.assert_not_called()

    def test_linked_tpdb_id_fetches_only_tpdb(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _seed_video(self.conn, tmp, match_source="theporndb", external_id="42")
            with patch.object(scene_scraper_text_sources, "fetch_stashdb_scene_by_id") as stash, \
                 patch.object(scene_scraper_text_sources, "fetch_tpdb_scene_by_id",
                              return_value=self._scene("theporndb")) as tpdb, \
                 patch.object(scene_scraper_media_files, "_download_scene_poster",
                              side_effect=self._poster_ok()):
                result = scene_artwork_download.download_artwork_for_video(
                    self.conn, tmp, "telegram:123")
            self.assertEqual(result, {"downloaded": True, "provider": "ThePornDB"})
            stash.assert_not_called()
            tpdb.assert_called_once_with("42")

    def test_linked_id_from_unknown_source_consults_both_and_reports_lookup_failure(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _seed_video(self.conn, tmp, match_source="other", external_id="scene-9")
            with patch.object(scene_scraper_text_sources, "fetch_stashdb_scene_by_id",
                              side_effect=RuntimeError("stashdb down")), \
                 patch.object(scene_scraper_text_sources, "fetch_tpdb_scene_by_id",
                              side_effect=RuntimeError("tpdb down")), \
                 patch.object(scene_scraper_text_sources, "search_scene_by_exact_title",
                              return_value=None):
                result = scene_artwork_download.download_artwork_for_video(
                    self.conn, tmp, "telegram:123")
            self.assertEqual(result, {"downloaded": False,
                                      "local_state": "artwork_lookup_failed"})

    def test_oshash_branch_falls_forward_to_second_provider(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _seed_video(self.conn, tmp)
            with self.conn:
                self.conn.execute(
                    "INSERT INTO video_fingerprints (video_id, algorithm, fingerprint) "
                    "VALUES (%s, 'OSHASH', %s)", ("telegram:123", "0123456789abcdef"),
                )
            with patch.object(scene_scraper_fingerprint_sources, "search_stashdb_scene_by_oshash",
                              side_effect=RuntimeError("stashdb down")), \
                 patch.object(scene_scraper_fingerprint_sources, "search_tpdb_scene_by_oshash",
                              return_value=self._scene("theporndb")), \
                 patch.object(scene_scraper_media_files, "_download_scene_poster",
                              side_effect=self._poster_ok()):
                result = scene_artwork_download.download_artwork_for_video(
                    self.conn, tmp, "telegram:123")
            self.assertEqual(result, {"downloaded": True, "provider": "ThePornDB"})

    def test_title_branch_maps_source_to_provider_label(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _seed_video(self.conn, tmp)
            with patch.object(scene_scraper_text_sources, "search_scene_by_exact_title",
                              return_value=self._scene("stashdb")), \
                 patch.object(scene_scraper_media_files, "_download_scene_poster",
                              side_effect=self._poster_ok()):
                result = scene_artwork_download.download_artwork_for_video(
                    self.conn, tmp, "telegram:123")
            self.assertEqual(result, {"downloaded": True, "provider": "StashDB"})

    def test_found_scene_with_failed_download_reports_download_failed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _seed_video(self.conn, tmp)
            with patch.object(scene_scraper_text_sources, "search_scene_by_exact_title",
                              return_value=self._scene("stashdb")), \
                 patch.object(scene_scraper_media_files, "_download_scene_poster",
                              return_value=False):
                result = scene_artwork_download.download_artwork_for_video(
                    self.conn, tmp, "telegram:123")
            self.assertEqual(result, {"downloaded": False,
                                      "local_state": "artwork_download_failed"})

    def test_no_match_and_no_errors_reports_not_found(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            _seed_video(self.conn, tmp)
            with patch.object(scene_scraper_text_sources, "search_scene_by_exact_title",
                              return_value=None):
                result = scene_artwork_download.download_artwork_for_video(
                    self.conn, tmp, "telegram:123")
            self.assertEqual(result, {"downloaded": False,
                                      "local_state": "artwork_not_found"})


class ArtworkJobQueueTests(PgTestCase):
    def test_enqueue_and_claim_artwork_download_job(self):
        from common.job_queue import claim_job, enqueue_job

        job_id = enqueue_job(self.db_url, "artwork_download", {"video_id": "telegram:123"})
        claimed = claim_job(self.db_url, "artwork_download")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], job_id)
        self.assertEqual(
            self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s",
                              (job_id,)).fetchone()[0], "processing")
        import json
        self.assertEqual(json.loads(claimed["payload"]), {"video_id": "telegram:123"})

    def _processing_job(self, payload: dict) -> str:
        import json
        import time
        job_id = f"job-{int(time.time() * 1000)}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs "
                "(id, action, state, payload, result, created_at, updated_at) "
                "VALUES (%s, 'artwork_download', 'processing', %s, NULL, %s, %s)",
                (job_id, json.dumps(payload), int(time.time()), int(time.time())),
            )
        return job_id

    def _services(self, refresh_ok: bool = True) -> "SimpleNamespace":
        return SimpleNamespace(trigger_jellyfin_refresh=lambda: refresh_ok)

    def test_dispatch_records_progress_and_downloaded_state(self):
        import json
        job_id = self._processing_job({"video_id": "telegram:123"})
        import duplicate_job_external_handlers
        with patch.object(scene_artwork_download, "download_artwork_for_video",
                          return_value={"downloaded": True, "provider": "StashDB"}):
            result, committed = duplicate_job_external_handlers.execute_external_operation(
                "artwork_download", self.conn, job_id, {"video_id": "telegram:123"},
                Path("/nonexistent-media-dir"), self._services())
        self.assertTrue(committed)
        self.assertEqual(result["local_state"], "artwork_downloaded")
        self.assertEqual(result["jellyfin_refresh"], "updated")
        row = self.conn.execute(
            "SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        stored = json.loads(row[0])
        self.assertEqual(stored["progress"]["stage"], "refreshing_jellyfin")
        stages = [event["stage"] for event in stored["events"]]
        self.assertEqual(stages, ["downloading_artwork", "refreshing_jellyfin"])
        self.assertEqual(stored["jellyfin_refresh"], "pending")

    def test_dispatch_passes_through_not_found_state(self):
        import json
        job_id = self._processing_job({"video_id": "telegram:123"})
        import duplicate_job_external_handlers
        with patch.object(scene_artwork_download, "download_artwork_for_video",
                          return_value={"downloaded": False,
                                        "local_state": "artwork_not_found"}):
            result, committed = duplicate_job_external_handlers.execute_external_operation(
                "artwork_download", self.conn, job_id, {"video_id": "telegram:123"},
                Path("/nonexistent-media-dir"), self._services())
        self.assertTrue(committed)
        self.assertEqual(result["local_state"], "artwork_not_found")
        self.assertNotIn("jellyfin_refresh", result)
        row = self.conn.execute(
            "SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        stored = json.loads(row[0])
        self.assertEqual([e["stage"] for e in stored["events"]], ["downloading_artwork"])

    def test_dispatch_marks_refresh_failure(self):
        job_id = self._processing_job({"video_id": "telegram:123"})
        import duplicate_job_external_handlers
        with patch.object(scene_artwork_download, "download_artwork_for_video",
                          return_value={"downloaded": True, "provider": "StashDB"}):
            result, _ = duplicate_job_external_handlers.execute_external_operation(
                "artwork_download", self.conn, job_id, {"video_id": "telegram:123"},
                Path("/nonexistent-media-dir"), self._services(refresh_ok=False))
        self.assertEqual(result["local_state"], "artwork_downloaded")
        self.assertEqual(result["jellyfin_refresh"], "failed")

    def test_dispatch_rejects_invalid_video_id(self):
        import duplicate_job_external_handlers
        for payload in ({}, {"video_id": ""}, {"video_id": 5}, {"video_id": "x" * 1001}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                duplicate_job_external_handlers.execute_external_operation(
                    "artwork_download", self.conn, "unused-id", payload,
                    Path("/nonexistent-media-dir"), self._services())


if __name__ == "__main__":
    unittest.main()
