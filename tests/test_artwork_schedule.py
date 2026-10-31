"""Bounded selection for the daily artwork generation schedule."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from common.image_utils import compute_dhash
from sync.artwork_schedule import (
    MAX_ARTWORK_GENERATION_ITEMS,
    artwork_needs_generation,
    build_artwork_generation_jobs,
)


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""

    def execute(self, sql, _params=None):
        self.sql = sql
        return self

    def fetchall(self):
        return self.rows


class ArtworkScheduleTests(unittest.TestCase):
    def test_missing_black_and_known_studio_images_are_selected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir)
            rows = []
            for video_id, name in (("telegram:1", "missing"), ("telegram:2", "black"),
                                   ("telegram:3", "studio"), ("telegram:4", "valid")):
                (media / f"{name}.strm").write_text("stream", encoding="utf-8")
                rows.append((video_id, 600, f"{name}.strm"))

            Image.new("RGB", (80, 80), (0, 0, 0)).save(media / "black.jpg")
            studio_path = media / "studio.jpg"
            Image.new("RGB", (80, 80), (40, 100, 180)).save(studio_path)
            studio_hash = int(compute_dhash(studio_path), 16)
            valid = Image.new("RGB", (80, 80))
            for x in range(80):
                for y in range(80):
                    value = 255 - x * 3
                    valid.putpixel((x, y), (value, value, value))
            valid.save(media / "valid.jpg")

            jobs = build_artwork_generation_jobs(
                FakeConnection(rows), media, bumper_hashes={studio_hash},
            )

        self.assertEqual([payload["video_id"] for _action, payload in jobs],
                         ["telegram:1", "telegram:2", "telegram:3"])
        self.assertTrue(all(action == "artwork_generate" for action, _payload in jobs))
        self.assertTrue(all(payload["only_if_artwork_invalid"] for _action, payload in jobs))

    def test_limit_is_hard_capped_at_one_hundred(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir)
            rows = []
            for index in range(MAX_ARTWORK_GENERATION_ITEMS + 7):
                name = f"item-{index}"
                (media / f"{name}.strm").write_text("stream", encoding="utf-8")
                rows.append((f"telegram:{index}", 600, f"{name}.strm"))
            jobs = build_artwork_generation_jobs(FakeConnection(rows), media, limit=1000,
                                                  bumper_hashes=set())

        self.assertEqual(len(jobs), MAX_ARTWORK_GENERATION_ITEMS)

    def test_artwork_detector_does_not_classify_a_symlink_as_replaceable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir)
            target = media / "valid.jpg"
            Image.new("RGB", (80, 80), (120, 80, 35)).save(target)
            link = media / "linked.jpg"
            link.symlink_to(target)
            self.assertFalse(artwork_needs_generation(link, bumper_hashes=set()))


if __name__ == "__main__":
    unittest.main()
