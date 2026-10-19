import io
import unittest
from PIL import Image

from pathlib import Path
import tempfile

from common.image_utils import (
    compute_dhash,
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


if __name__ == "__main__":
    unittest.main()
