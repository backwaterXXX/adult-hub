import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from common.image_utils import (
    DEFAULT_ARTWORK_EXTRACTION_TIMEOUT,
    DEFAULT_FRAME_EXTRACTION_TIMEOUT,
    compute_dhash,
    extract_video_frame,
    find_bumper_clusters,
    hamming_distance,
    is_black_image,
    is_bumper_hash,
    load_bumper_hashes,
    save_bumper_hashes,
)


class TestImageUtils(unittest.TestCase):
    def test_pure_black_image_is_detected(self):
        # Create a pure black image (RGB 0,0,0)
        img = Image.new("RGB", (320, 180), color=(0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        data = buf.getvalue()

        self.assertTrue(is_black_image(img))
        self.assertTrue(is_black_image(data))

    def test_near_black_image_is_detected(self):
        # Create a nearly black image with slight compression noise (RGB 3,3,3)
        img = Image.new("RGB", (100, 100), color=(3, 3, 3))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        data = buf.getvalue()

        self.assertTrue(is_black_image(data))

    def test_normal_image_is_not_black(self):
        # Create a colorful / normal image (RGB 128, 64, 200)
        img = Image.new("RGB", (320, 180), color=(128, 64, 200))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        data = buf.getvalue()

        self.assertFalse(is_black_image(img))
        self.assertFalse(is_black_image(data))

    def test_grayscale_black_and_white(self):
        black_l = Image.new("L", (100, 100), color=0)
        white_l = Image.new("L", (100, 100), color=255)
        self.assertTrue(is_black_image(black_l))
        self.assertFalse(is_black_image(white_l))

    def test_corrupted_data_returns_false_or_true_safely(self):
        # Empty or non-image bytes return False without throwing exception
        self.assertFalse(is_black_image(b""))
        self.assertFalse(is_black_image(b"x" * 50))
        self.assertFalse(is_black_image(b"x" * 500))

    def test_dhash_computation_and_distance(self):
        img1 = Image.new("RGB", (64, 64), color=(50, 50, 50))
        img2 = Image.new("RGB", (64, 64), color=(50, 50, 50))
        # Draw something in img3
        img3 = Image.new("RGB", (64, 64), color=(0, 0, 0))
        for x in range(32):
            for y in range(64):
                img3.putpixel((x, y), (255, 255, 255))

        h1 = compute_dhash(img1)
        h2 = compute_dhash(img2)
        h3 = compute_dhash(img3)

        self.assertEqual(len(h1), 16)
        self.assertEqual(h1, h2)
        self.assertEqual(hamming_distance(h1, h2), 0)
        self.assertGreater(hamming_distance(h1, h3), 0)

    def test_find_bumper_clusters_exact_and_fuzzy(self):
        # Test clustering where hashes differ by <= 4 bits
        # Base hash: c0d861060f809ccf
        # Variant 1 (dist 1): c0d861060f809cce (differs by last bit)
        # Variant 2 (dist 4): c0d869161f849ccf (real world case for target 2)
        # Unrelated hash (dist >> 4): 0030307070301000
        file_hashes = {
            Path("file1.jpg"): "c0d861060f809ccf",
            Path("file2.jpg"): "c0d861060f809ccf",
            Path("file3.jpg"): "c0d861060f809cce",
            Path("file4.jpg"): "c0d869161f849ccf",
            Path("file5.jpg"): "c0d869161f849ccf",
            Path("unrelated.jpg"): "0030307070301000",
        }

        # With min_count=5 and max_distance=4:
        # The 5 files in the c0d8... family should form one single cluster
        clusters = find_bumper_clusters(file_hashes, max_distance=4, min_count=5)
        self.assertEqual(len(clusters), 1)
        cluster = clusters[0]
        self.assertEqual(cluster["count"], 5)
        self.assertIn(Path("file4.jpg"), cluster["files"])
        self.assertIn("c0d869161f849ccf", cluster["hashes"])
        self.assertIn("c0d861060f809ccf", cluster["hashes"])
        self.assertNotIn(Path("unrelated.jpg"), cluster["files"])

        # With min_count=6, no cluster should be returned
        self.assertEqual(len(find_bumper_clusters(file_hashes, max_distance=4, min_count=6)), 0)

        # With max_distance=0, variants are separated, neither has count >= 5
        self.assertEqual(len(find_bumper_clusters(file_hashes, max_distance=0, min_count=5)), 0)

    def test_is_bumper_hash(self):
        bumper_set = {int("c0d861060f809ccf", 16)}
        # Exact match
        self.assertTrue(is_bumper_hash("c0d861060f809ccf", bumper_set, max_distance=4))
        # Within distance 4
        self.assertTrue(is_bumper_hash("c0d869161f849ccf", bumper_set, max_distance=4))
        # Distance 0 check with different hash fails
        self.assertFalse(is_bumper_hash("c0d869161f849ccf", bumper_set, max_distance=0))
        # Far hash fails
        self.assertFalse(is_bumper_hash("0030307070301000", bumper_set, max_distance=4))
        # String bumper list works
        self.assertTrue(is_bumper_hash("c0d869161f849ccf", ["c0d861060f809ccf"], max_distance=4))

    def test_save_and_load_bumper_hashes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_file = Path(tmp_dir) / "sub" / "bumper_hashes.json"
            hashes = {"0030307070301000", "c0d861060f809ccf"}
            save_bumper_hashes(cache_file, hashes, metadata={"threshold": 5})

            self.assertTrue(cache_file.is_file())
            loaded = load_bumper_hashes(cache_file)
            self.assertEqual(len(loaded), 2)
            self.assertIn(int("0030307070301000", 16), loaded)
            self.assertIn(int("c0d861060f809ccf", 16), loaded)

            # Test loading non-existent file
            self.assertEqual(load_bumper_hashes(Path(tmp_dir) / "does_not_exist.json"), set())

    def _write_dummy_jpeg(self, path, color=(128, 64, 200)):
        img = Image.new("RGB", (64, 64), color=color)
        img.save(path, format="JPEG")

    @patch("subprocess.run")
    def test_extract_video_frame_default_timeout_is_20_seconds(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "output.jpg"

            def fake_run(cmd, **kwargs):
                self._write_dummy_jpeg(cmd[-1])
                return subprocess.CompletedProcess(cmd, 0)

            mock_run.side_effect = fake_run

            success, err = extract_video_frame("http://example.test/stream", dest)
            self.assertTrue(success)
            self.assertEqual(err, "")
            self.assertTrue(dest.exists())
            mock_run.assert_called_once()
            self.assertEqual(mock_run.call_args.kwargs.get("timeout"), DEFAULT_FRAME_EXTRACTION_TIMEOUT)
            self.assertEqual(mock_run.call_args.kwargs.get("timeout"), 20.0)

    @patch("subprocess.run")
    def test_extract_video_frame_forwards_custom_90s_timeout(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "output.jpg"

            def fake_run(cmd, **kwargs):
                self._write_dummy_jpeg(cmd[-1])
                return subprocess.CompletedProcess(cmd, 0)

            mock_run.side_effect = fake_run

            success, err = extract_video_frame(
                "http://example.test/stream", dest, timeout=DEFAULT_ARTWORK_EXTRACTION_TIMEOUT
            )
            self.assertTrue(success)
            self.assertEqual(err, "")
            self.assertTrue(dest.exists())
            mock_run.assert_called_once()
            self.assertEqual(mock_run.call_args.kwargs.get("timeout"), 90.0)

    @patch("subprocess.run")
    def test_extract_video_frame_fast_seek_timeout_sequential_fallback_with_same_timeout(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "output.jpg"

            def fake_run(cmd, **kwargs):
                if mock_run.call_count == 1:
                    # Fast seek timed out at 90s
                    raise subprocess.TimeoutExpired(cmd, 90.0)
                # Fallback sequential seek succeeds
                self._write_dummy_jpeg(cmd[-1])
                return subprocess.CompletedProcess(cmd, 0)

            mock_run.side_effect = fake_run

            success, err = extract_video_frame("http://example.test/stream", dest, timeout=90.0)
            self.assertTrue(success)
            self.assertEqual(err, "")
            self.assertTrue(dest.exists())
            self.assertEqual(mock_run.call_count, 2)

            # Both fast seek and sequential fallback attempts received timeout=90.0
            call1, call2 = mock_run.call_args_list
            self.assertEqual(call1.kwargs.get("timeout"), 90.0)
            self.assertEqual(call2.kwargs.get("timeout"), 90.0)

            # Verify call1 was fast input seeking (-ss before -i)
            cmd1 = call1.args[0]
            self.assertIn("-ss", cmd1)
            self.assertLess(cmd1.index("-ss"), cmd1.index("-i"))

            # Verify call2 was sequential fallback seeking (-i before -ss)
            cmd2 = call2.args[0]
            self.assertIn("-ss", cmd2)
            self.assertLess(cmd2.index("-i"), cmd2.index("-ss"))

    @patch("subprocess.run")
    def test_extract_video_frame_both_attempts_timeout_returns_error(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "output.jpg"
            mock_run.side_effect = subprocess.TimeoutExpired(["ffmpeg"], 90.0)

            success, err = extract_video_frame("http://example.test/stream", dest, timeout=90.0)
            self.assertFalse(success)
            self.assertEqual(err, "ffmpeg failed to extract a video frame from stream.")
            self.assertFalse(dest.exists())
            self.assertEqual(mock_run.call_count, 2)
            for c in mock_run.call_args_list:
                self.assertEqual(c.kwargs.get("timeout"), 90.0)

    @patch("subprocess.run")
    def test_extract_video_frame_black_image_retry_passes_timeout(self, mock_run):
        with tempfile.TemporaryDirectory() as tmp_dir:
            dest = Path(tmp_dir) / "output.jpg"

            def fake_run(cmd, **kwargs):
                if mock_run.call_count == 1:
                    # First attempt produces pure black image
                    self._write_dummy_jpeg(cmd[-1], color=(0, 0, 0))
                else:
                    # Retry with offset + 30s produces colorful image
                    self._write_dummy_jpeg(cmd[-1], color=(100, 150, 200))
                return subprocess.CompletedProcess(cmd, 0)

            mock_run.side_effect = fake_run

            success, err = extract_video_frame("http://example.test/stream", dest, timeout=90.0)
            self.assertTrue(success)
            self.assertEqual(err, "")
            self.assertTrue(dest.exists())
            # Fast seek, then retry due to black image
            self.assertEqual(mock_run.call_count, 2)
            call1, call2 = mock_run.call_args_list
            self.assertEqual(call1.kwargs.get("timeout"), 90.0)
            self.assertEqual(call2.kwargs.get("timeout"), 90.0)


if __name__ == "__main__":
    unittest.main()
