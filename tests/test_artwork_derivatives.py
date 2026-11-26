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

    def test_an_unmounted_cache_dir_is_not_the_derivative_store(self) -> None:
        # The image ships an empty /cache; only a mount carries the shared files.
        cache_root = Path(self.tmp.name) / "cache"
        cache_root.mkdir()
        data_root = Path(self.tmp.name) / "data"
        data_root.mkdir()
        with mock.patch.dict(os.environ, {"ARTWORK_DERIVATIVE_DIR": ""}), \
                mock.patch.object(derivatives, "_CACHE_ROOT", cache_root), \
                mock.patch.object(derivatives, "_DATA_ROOT", data_root), \
                mock.patch("resolver.artwork_derivatives.os.path.ismount", return_value=False):
            self.assertEqual(derivatives.derivatives_dir(),
                             data_root / "cache" / "artwork-derivatives")

    def test_a_mounted_cache_volume_wins_over_the_data_root(self) -> None:
        cache_root = Path(self.tmp.name) / "cache"
        cache_root.mkdir()
        data_root = Path(self.tmp.name) / "data"
        data_root.mkdir()
        with mock.patch.dict(os.environ, {"ARTWORK_DERIVATIVE_DIR": ""}), \
                mock.patch.object(derivatives, "_CACHE_ROOT", cache_root), \
                mock.patch.object(derivatives, "_DATA_ROOT", data_root), \
                mock.patch("resolver.artwork_derivatives.os.path.ismount", return_value=True):
            self.assertEqual(derivatives.derivatives_dir(),
                             cache_root / "artwork-derivatives")

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

    def test_prune_keeps_the_state_file_and_partial_writes(self) -> None:
        # _prune throttles itself; a fresh CI machine may have a monotonic clock
        # below the interval, so the throttle is what the test switches off.
        throttled = mock.patch.object(derivatives, "_PRUNE_INTERVAL_SECONDS", 0.0)
        throttled.start()
        self.addCleanup(throttled.stop)

        state = derivatives._state_path()
        state.write_text('{"files": {}}')
        derivative = Path(self.tmp.name) / "old-derivative.jpg"
        derivative.write_bytes(b"x" * 4096)
        temporary = Path(self.tmp.name) / "other.jpg.123.tmp"
        temporary.write_bytes(b"x" * 4096)

        derivatives._prune(limit_bytes=1024)

        # Evicting the bookkeeping file would force a full re-read of the media
        # tree on the next prewarm, and a *.tmp file belongs to another process.
        self.assertTrue(state.is_file(), "the prewarm state must never be evicted")
        self.assertTrue(temporary.is_file(), "a partial write belongs to another process")
        self.assertFalse(derivative.is_file(), "derivatives are still evicted")

    def test_prewarm_merges_state_written_by_another_process(self) -> None:
        root = Path(self.tmp.name) / "media"
        root.mkdir()
        (root / "poster.jpg").write_bytes(_png(400, 600))

        derivatives.prewarm_directory(root, widths=(240,), limit=1)
        state = derivatives._load_prewarm_state()
        state["files"]["foreign/poster.jpg"] = "1:1"
        derivatives._save_prewarm_state(state)

        derivatives.prewarm_directory(root, widths=(240,), limit=1)

        # Encoding happens outside the state lock, so the second run must merge
        # into the current file instead of overwriting it with its own snapshot.
        self.assertIn("foreign/poster.jpg", derivatives._load_prewarm_state()["files"])

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
