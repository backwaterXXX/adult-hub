"""Tests for optimal quality variant selection (480p AV01) for pHash and artwork."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from common.video_variants import (
    fetch_and_select_best_variant,
    fetch_and_select_best_variant_with_meta,
    score_variant,
    select_optimal_variant,
)


class TestVideoVariantsSelection(unittest.TestCase):
    def test_score_variant_prefers_480p_av01(self):
        v_480_av1 = {"id": "alt_480", "is_original": False, "width": 854, "height": 480, "codec": "av01", "file_size": 150_000_000}
        v_360_av1 = {"id": "alt_360", "is_original": False, "width": 640, "height": 360, "codec": "av01", "file_size": 90_000_000}
        v_720_av1 = {"id": "alt_720", "is_original": False, "width": 1280, "height": 720, "codec": "av01", "file_size": 400_000_000}
        v_480_h264 = {"id": "alt_480_h264", "is_original": False, "width": 854, "height": 480, "codec": "avc1", "file_size": 220_000_000}
        v_orig = {"id": "original", "is_original": True, "width": 1920, "height": 1080, "codec": "h264", "file_size": 2_500_000_000}

        score_480_av1 = score_variant(v_480_av1)
        score_360_av1 = score_variant(v_360_av1)
        score_720_av1 = score_variant(v_720_av1)
        score_480_h264 = score_variant(v_480_h264)
        score_orig = score_variant(v_orig)

        self.assertEqual(score_orig, 0, "Original stream should have baseline score 0")
        self.assertGreater(score_480_av1, score_360_av1, "480p AV01 should score higher than 360p")
        self.assertGreater(score_480_av1, score_720_av1, "480p AV01 should score higher than 720p")
        self.assertGreater(score_480_av1, score_480_h264, "AV01 codec should score higher than AVC1 at 480p")

    def test_select_optimal_variant_picks_480p_av01(self):
        variants = [
            {"id": "original", "is_original": True, "width": 1920, "height": 1080, "codec": "h264", "file_size": 2_000_000_000},
            {"id": "alt_1080", "is_original": False, "width": 1920, "height": 1080, "codec": "av01", "file_size": 900_000_000},
            {"id": "alt_720", "is_original": False, "width": 1280, "height": 720, "codec": "av01", "file_size": 450_000_000},
            {"id": "alt_480", "is_original": False, "width": 854, "height": 480, "codec": "av01", "file_size": 180_000_000},
            {"id": "alt_360", "is_original": False, "width": 640, "height": 360, "codec": "av01", "file_size": 100_000_000},
        ]
        best = select_optimal_variant(variants, purpose="artwork")
        self.assertIsNotNone(best)
        self.assertEqual(best["id"], "alt_480")

    def test_select_optimal_variant_fallback_to_360p_or_720p(self):
        # When 480p is missing, picks 360p or 720p rather than None
        variants_no_480 = [
            {"id": "original", "is_original": True, "width": 1920, "height": 1080, "codec": "h264", "file_size": 2_000_000_000},
            {"id": "alt_720", "is_original": False, "width": 1280, "height": 720, "codec": "av01", "file_size": 450_000_000},
            {"id": "alt_360", "is_original": False, "width": 640, "height": 360, "codec": "av01", "file_size": 100_000_000},
        ]
        best = select_optimal_variant(variants_no_480, purpose="phash")
        self.assertIsNotNone(best)
        self.assertEqual(best["id"], "alt_360")

    def test_select_optimal_variant_returns_none_if_only_original(self):
        variants = [
            {"id": "original", "is_original": True, "width": 1920, "height": 1080, "codec": "h264", "file_size": 2_000_000_000},
        ]
        self.assertIsNone(select_optimal_variant(variants))
        self.assertIsNone(select_optimal_variant([]))
        self.assertIsNone(select_optimal_variant(None))

    def test_select_optimal_variant_disqualifies_too_small_for_artwork(self):
        variants = [
            {"id": "original", "is_original": True, "width": 1920, "height": 1080, "codec": "h264"},
            {"id": "alt_144", "is_original": False, "width": 256, "height": 144, "codec": "av01"},
        ]
        self.assertIsNone(select_optimal_variant(variants, purpose="artwork"))

    def test_fetch_and_select_best_variant_with_meta_success(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "ready",
            "has_alternatives": True,
            "variants": [
                {"id": "original", "is_original": True, "width": 1920, "height": 1080, "codec": "h264", "file_size": 2_500_000_000},
                {"id": "alt_5002", "is_original": False, "width": 854, "height": 480, "codec": "av01", "file_size": 180_000_000},
            ],
            "video_id": "telegram:1:2:3",
        }
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        best_id, orig_size = fetch_and_select_best_variant_with_meta(
            "http://127.0.0.1:8090/admin", "telegram:1:2:3", purpose="phash", http_client=mock_client,
        )
        self.assertEqual(best_id, "alt_5002")
        self.assertEqual(orig_size, 2_500_000_000)

    def test_fetch_and_select_best_variant_original_only(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "status": "original_only",
            "has_alternatives": False,
            "variants": [
                {"id": "original", "is_original": True, "width": 1920, "height": 1080, "codec": "h264", "file_size": 1_200_000_000},
            ],
        }
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response

        best_id, orig_size = fetch_and_select_best_variant_with_meta(
            "http://127.0.0.1:8090/admin", "telegram:1:2:3", http_client=mock_client,
        )
        self.assertIsNone(best_id)
        self.assertEqual(orig_size, 1_200_000_000)

        # Convenience helper
        self.assertIsNone(fetch_and_select_best_variant("http://127.0.0.1:8090/admin", "telegram:1:2:3", http_client=mock_client))

    def test_fetch_and_select_best_variant_network_error_fails_safe(self):
        mock_client = MagicMock()
        mock_client.get.side_effect = RuntimeError("Connection refused")

        best_id, orig_size = fetch_and_select_best_variant_with_meta(
            "http://127.0.0.1:8090/admin", "telegram:1:2:3", http_client=mock_client,
        )
        self.assertIsNone(best_id)
        self.assertIsNone(orig_size)


class TestFingerprintBudgetVariantHandling(unittest.TestCase):
    def test_fingerprint_budget_stream_url_and_cache_dir_with_variant(self):
        from sync.video_fingerprints import FingerprintBudget

        budget_orig = FingerprintBudget("telegram:1:2:3", resolver_url="http://test:8090/admin")
        self.assertEqual(
            budget_orig.stream_url,
            "http://test:8090/admin/play/telegram:1:2:3/stream?mode=proxy",
        )
        self.assertIsNone(budget_orig.variant)

        budget_alt = FingerprintBudget("telegram:1:2:3", resolver_url="http://test:8090/admin", variant="alt_5002")
        self.assertEqual(
            budget_alt.stream_url,
            "http://test:8090/admin/play/telegram:1:2:3/stream?mode=proxy&variant=alt_5002",
        )
        self.assertEqual(budget_alt.variant, "alt_5002")

    @patch("sync.video_fingerprints.FingerprintBudget")
    @patch("sync.video_fingerprints._FaststartMP4")
    @patch("sync.video_fingerprints.http.server.ThreadingHTTPServer")
    @patch("sync.video_fingerprints.threading.Thread")
    @patch("sync.video_fingerprints.subprocess.run")
    @patch("PIL.Image.open")
    @patch("stash_phash.video.combine_images")
    @patch("sync.video_fingerprints._stash_phash", return_value="deadbeef12345678")
    @patch("common.video_variants.fetch_and_select_best_variant_with_meta")
    def test_telegram_phash_uses_optimal_variant_and_preserves_size_sink(
        self, mock_variants, mock_stash_phash, mock_combine, mock_img, mock_subproc, mock_th, mock_srv, mock_mp4, mock_budget_cls,
    ):
        from sync.video_fingerprints import telegram_phash

        mock_variants.return_value = ("alt_5002", 3_000_000_000)
        mock_budget = MagicMock()
        mock_budget.bytes_read = 450_000
        mock_budget.total_size = 180_000_000  # Size of the 480p variant
        mock_budget_cls.return_value = mock_budget

        mock_subproc.return_value = MagicMock(returncode=0, stdout=b"fake-bmp")
        fake_frame = MagicMock()
        fake_frame.width = 160
        mock_img.return_value.convert.return_value = fake_frame

        recorded_sizes = []
        result = telegram_phash(
            "telegram:1:2:3",
            120,
            size_sink=lambda s: recorded_sizes.append(s),
        )

        self.assertEqual(result, ("deadbeef12345678", 450_000))
        # Ensure FingerprintBudget was initialized with the chosen variant
        mock_budget_cls.assert_called_once()
        self.assertEqual(mock_budget_cls.call_args[1]["variant"], "alt_5002")
        # Ensure size_sink recorded the 3GB original size, NOT the 180MB variant size!
        self.assertEqual(recorded_sizes, [3_000_000_000])


class TestArtworkGenerateVariantHandling(unittest.TestCase):
    @patch("common.image_utils.extract_video_frame", return_value=(True, None))
    @patch("sync.media_files.resolve_media_path", return_value=Path("/tmp/test.mp4"))
    @patch("common.video_variants.fetch_and_select_best_variant", return_value="alt_480_av1")
    @patch("sync.duplicate_job_external_handlers._core._refresh_jellyfin_with_progress", return_value=True)
    def test_artwork_generate_auto_selects_variant(
        self, mock_refresh, mock_fetch_variant, mock_resolve_path, mock_extract,
    ):
        from sync.duplicate_job_external_handlers import execute_external_operation

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("test.mp4",)]
        mock_services = MagicMock()

        payload = {"video_id": "telegram:10:20:30", "offset_seconds": 60, "variant": "auto"}
        result, ok = execute_external_operation(
            "artwork_generate", mock_conn, 101, payload, "/tmp/media", mock_services,
        )

        self.assertTrue(ok)
        self.assertEqual(result["local_state"], "artwork_generated")
        self.assertEqual(result["variant"], "alt_480_av1")

        mock_fetch_variant.assert_called_once()
        self.assertEqual(mock_fetch_variant.call_args[0][1], "telegram:10:20:30")
        self.assertEqual(mock_fetch_variant.call_args[1]["purpose"], "artwork")

        # extract_video_frame must have received stream URL with ?variant=alt_480_av1
        called_url = mock_extract.call_args[0][0]
        self.assertIn("?variant=alt_480_av1", called_url)
        self.assertEqual(called_url.split("/play/")[0], "http://resolver:8090/admin")
        self.assertEqual(mock_extract.call_args.kwargs.get("timeout"), 90.0)

    @patch("common.image_utils.extract_video_frame", return_value=(True, None))
    @patch("sync.media_files.resolve_media_path", return_value=Path("/tmp/test.mp4"))
    @patch("common.video_variants.fetch_and_select_best_variant")
    @patch("sync.duplicate_job_external_handlers._core._refresh_jellyfin_with_progress", return_value=True)
    def test_artwork_generate_respects_explicit_original(
        self, mock_refresh, mock_fetch_variant, mock_resolve_path, mock_extract,
    ):
        from sync.duplicate_job_external_handlers import execute_external_operation

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("test.mp4",)]
        mock_services = MagicMock()

        payload = {"video_id": "telegram:10:20:30", "offset_seconds": 60, "variant": "original"}
        result, ok = execute_external_operation(
            "artwork_generate", mock_conn, 102, payload, "/tmp/media", mock_services,
        )

        self.assertTrue(ok)
        self.assertEqual(result["variant"], "original")
        mock_fetch_variant.assert_not_called()
        called_url = mock_extract.call_args[0][0]
        self.assertNotIn("?variant=", called_url)
        self.assertEqual(mock_extract.call_args.kwargs.get("timeout"), 90.0)

    @patch("common.image_utils.extract_video_frame", return_value=(True, None))
    @patch("sync.media_files.resolve_media_path", return_value=Path("/tmp/test.mp4"))
    @patch("sync.duplicate_job_external_handlers._core._refresh_jellyfin_with_progress", return_value=True)
    def test_artwork_generate_forwards_custom_timeout(
        self, mock_refresh, mock_resolve_path, mock_extract,
    ):
        from sync.duplicate_job_external_handlers import execute_external_operation

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("test.mp4",)]
        mock_services = MagicMock()

        payload = {"video_id": "telegram:10:20:30", "offset_seconds": 60, "variant": "original", "timeout": 120.0}
        result, ok = execute_external_operation(
            "artwork_generate", mock_conn, 105, payload, "/tmp/media", mock_services,
        )

        self.assertTrue(ok)
        self.assertEqual(mock_extract.call_args.kwargs.get("timeout"), 120.0)

    def test_artwork_generate_rejects_invalid_timeout(self):
        from sync.duplicate_job_external_handlers import execute_external_operation

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("test.mp4",)]
        mock_services = MagicMock()

        for bad_timeout in [0, -5, "90", False, float("nan"), float("inf"), float("-inf")]:
            payload = {"video_id": "telegram:10:20:30", "offset_seconds": 60, "timeout": bad_timeout}
            with self.assertRaises(ValueError, msg=f"Should reject timeout={bad_timeout!r}"):
                execute_external_operation(
                    "artwork_generate", mock_conn, 106, payload, "/tmp/media", mock_services,
                )

    def test_admin_base_is_not_nested_twice(self):
        # Import the handler the flat way the sync image does, so this does not
        # depend on which module identity the suite happened to load first.
        try:
            import duplicate_job_external_handlers
            import media_files
        except ModuleNotFoundError:
            from sync import duplicate_job_external_handlers
            from sync import media_files

        handlers = duplicate_job_external_handlers
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("test.mp4",)]
        with patch.object(media_files, "resolve_media_path", return_value=Path("/tmp/test.mp4")), \
                patch.object(handlers._core, "_refresh_jellyfin_with_progress", return_value=True), \
                patch("common.video_variants.fetch_and_select_best_variant", return_value="alt_480_av1"), \
                patch("common.image_utils.extract_video_frame", return_value=(True, None)) as mock_extract, \
                patch.dict(os.environ, {"RESOLVER_API_URL": "http://resolver:8090/admin/"}):
            handlers.execute_external_operation(
                "artwork_generate", mock_conn, 103,
                {"video_id": "telegram:10:20:30", "offset_seconds": 60, "variant": "auto"},
                "/tmp/media", MagicMock(),
            )
        called_url = mock_extract.call_args[0][0]
        self.assertIn("/admin/play/", called_url)
        self.assertNotIn("/admin/admin", called_url)

    def test_scheduled_generation_skips_when_artwork_was_repaired_after_selection(self):
        from sync.duplicate_job_external_handlers import execute_external_operation

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("test.mp4",)]
        with patch("sync.media_files.resolve_media_path", return_value=Path("/tmp/test.mp4")), \
                patch("sync.artwork_schedule.artwork_needs_generation", return_value=False), \
                patch("common.image_utils.extract_video_frame") as mock_extract, \
                patch("sync.duplicate_job_external_handlers._core._refresh_jellyfin_with_progress") as mock_refresh:
            result, ok = execute_external_operation(
                "artwork_generate", mock_conn, 104,
                {"video_id": "telegram:10:20:30", "offset_seconds": 60,
                 "only_if_artwork_invalid": True, "file_path": "test.mp4"},
                "/tmp/media", MagicMock(),
            )

        self.assertTrue(ok)
        self.assertEqual(result, {"local_state": "artwork_already_valid", "skipped": True})
        mock_extract.assert_not_called()
        mock_refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
