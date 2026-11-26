"""Focused tests for lazily generated artwork derivatives."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from resolver import artwork_derivatives as derivatives


def _png(width: int = 800, height: int = 1200, color: tuple[int, int, int] = (120, 40, 200)) -> bytes:
    image = Image.new("RGB", (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


class ArtworkDerivativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {
                "ARTWORK_DERIVATIVE_DIR": self.tmp.name,
                "ARTWORK_DERIVATIVE_CACHE_MB": "0",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_smaller_variant_is_jpeg_at_requested_width(self) -> None:
        encoded = derivatives.get_derivative(_png(800, 1200), 400)

        self.assertIsNotNone(encoded)
        assert encoded is not None
        self.assertEqual(encoded[:2], b"\xff\xd8")
        with Image.open(io.BytesIO(encoded)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (400, 600))

    def test_cache_hit_skips_encoding(self) -> None:
        source = _png()
        derivatives.get_derivative(source, 240)

        with mock.patch.object(
            derivatives, "_encode", side_effect=AssertionError("encoded twice")
        ):
            again = derivatives.get_derivative(source, 240)

        self.assertIsNotNone(again)

    def test_small_source_is_not_upscaled(self) -> None:
        encoded = derivatives.get_derivative(_png(320, 480), 480)

        self.assertIsNotNone(encoded)
        assert encoded is not None
        with Image.open(io.BytesIO(encoded)) as image:
            self.assertEqual(image.size, (320, 480))

    def test_unsupported_width_is_rejected(self) -> None:
        self.assertIsNone(derivatives.get_derivative(_png(), 401))

    def test_undecodable_source_is_rejected(self) -> None:
        self.assertIsNone(derivatives.get_derivative(b"not an image", 320))

    def test_changed_source_gets_a_new_variant(self) -> None:
        first = derivatives.get_derivative(_png(800, 1200), 240)
        second = derivatives.get_derivative(_png(800, 1200, color=(10, 200, 30)), 240)

        self.assertNotEqual(first, second)

    def test_prewarm_file_generates_then_reuses(self) -> None:
        poster = Path(self.tmp.name) / "poster.jpg"
        poster.write_bytes(_png(800, 1200))

        self.assertEqual(derivatives.prewarm_file(poster, (240, 480)), 2)
        self.assertEqual(derivatives.prewarm_file(poster, (240, 480)), 0)

        source = poster.read_bytes()
        for width in (240, 480):
            self.assertTrue(derivatives.derivative_path(source, width).is_file())

    def test_prewarm_directory_is_bounded_and_incremental(self) -> None:
        root = Path(self.tmp.name) / "media"
        root.mkdir()
        for index in range(3):
            (root / f"poster-{index}.jpg").write_bytes(_png(400 + index, 600))

        first = derivatives.prewarm_directory(root, widths=(240,), limit=2)
        self.assertEqual(first["generated_files"], 2)

        second = derivatives.prewarm_directory(root, widths=(240,), limit=2)
        self.assertEqual(second["generated_files"], 1)

        third = derivatives.prewarm_directory(root, widths=(240,), limit=2)
        self.assertEqual(third["generated_files"], 0)
        self.assertGreaterEqual(third["skipped"], 3)


if __name__ == "__main__":
    unittest.main()
