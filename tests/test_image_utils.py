import io
import unittest
from PIL import Image

from common.image_utils import (
    compute_dhash,
    hamming_distance,
    is_black_image,
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


if __name__ == "__main__":
    unittest.main()
