"""Bounded selection for the daily artwork generation schedule."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from pg_helper import PgTestCase

from common.image_utils import compute_dhash
from sync.artwork_schedule import (
    ARTWORK_FRAME_FAILURE_LIMIT,
    MAX_ARTWORK_GENERATION_ITEMS,
    artwork_needs_generation,
    build_artwork_generation_jobs,
)


class FakeConnection:
    def __init__(self, rows, tables=("videos", "synced_files")):
        self.rows = rows
        self.tables = tables
        self.sql = ""
        self._last_sql = ""

    def execute(self, sql, _params=None):
        self.sql = sql
        self._last_sql = sql
        return self

    def fetchall(self):
        if "pg_catalog.pg_tables" in self._last_sql:
            return [(name,) for name in self.tables]
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

    def test_limit_is_hard_capped_at_the_batch_size(self):
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


    def test_dead_and_unplayable_videos_are_skipped(self):
        """Denylisted and unplayable videos must not consume a generation slot."""
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir)
            (media / "missing.strm").write_text("stream", encoding="utf-8")
            conn = FakeConnection(
                [("telegram:1", 600, "missing.strm")],
                tables=("videos", "synced_files", "dead_streams"),
            )

            jobs = build_artwork_generation_jobs(conn, media, limit=5, bumper_hashes=set())

        self.assertIn("COALESCE(v.playable, 1) != 0", conn.sql)
        self.assertIn("NOT EXISTS (SELECT 1 FROM dead_streams", conn.sql)
        self.assertEqual(len(jobs), 1)

    def test_dead_stream_filter_is_optional_for_older_schemas(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir)
            (media / "missing.strm").write_text("stream", encoding="utf-8")
            conn = FakeConnection([("telegram:1", 600, "missing.strm")], tables=("videos",))

            build_artwork_generation_jobs(conn, media, limit=5, bumper_hashes=set())

        self.assertIn("COALESCE(v.playable, 1) != 0", conn.sql)
        self.assertNotIn("dead_streams", conn.sql)

    def test_repeated_frame_failures_are_excluded_from_the_batch(self):
        """A stream ffmpeg cannot decode must not be re-queued forever."""
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir)
            (media / "missing.strm").write_text("stream", encoding="utf-8")
            conn = FakeConnection(
                [("telegram:1", 600, "missing.strm")],
                tables=("videos", "synced_files", "video_duplicate_jobs"),
            )

            build_artwork_generation_jobs(conn, media, limit=5, bumper_hashes=set())

        self.assertIn("artwork_frame_extraction_failed", conn.sql)
        self.assertIn(f"HAVING COUNT(*) >= {ARTWORK_FRAME_FAILURE_LIMIT}", conn.sql)

    def test_frame_failure_filter_is_optional_for_older_schemas(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir)
            (media / "missing.strm").write_text("stream", encoding="utf-8")
            conn = FakeConnection([("telegram:1", 600, "missing.strm")], tables=("videos",))

            build_artwork_generation_jobs(conn, media, limit=5, bumper_hashes=set())

        self.assertNotIn("artwork_frame_extraction_failed", conn.sql)

class ArtworkScheduleDatabaseTests(PgTestCase):
    """Selection against a real schema, where the failure history lives."""

    def _seed(self, media: Path, video_id: str, failures: int,
              failure_code: str = "artwork_frame_extraction_failed") -> None:
        name = video_id.replace(":", "-")
        (media / f"{name}.strm").write_text("stream", encoding="utf-8")
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, title, duration, playable) VALUES (%s, %s, 600, 1)",
                (video_id, video_id),
            )
            self.conn.execute(
                "INSERT INTO synced_files (file_path, video_id) VALUES (%s, %s)",
                (f"{name}.strm", video_id),
            )
            for index in range(failures):
                payload = json.dumps({"video_id": video_id, "offset_seconds": 60})
                result = json.dumps({"error": "boom", "failure_code": failure_code})
                self.conn.execute(
                    "INSERT INTO video_duplicate_jobs "
                    "(id, action, state, payload, result, created_at, updated_at) "
                    "VALUES (%s, 'artwork_generate', 'failed', %s, %s, now(), now())",
                    (f"job-{name}-{failure_code}-{index}", payload, result),
                )

    def test_video_with_the_full_failure_budget_is_parked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir)
            self._seed(media, "telegram:parked", ARTWORK_FRAME_FAILURE_LIMIT)

            jobs = build_artwork_generation_jobs(self.conn, media, limit=5, bumper_hashes=set())

        self.assertEqual(jobs, [])

    def test_one_failure_below_the_budget_keeps_the_video(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir)
            self._seed(media, "telegram:trying", ARTWORK_FRAME_FAILURE_LIMIT - 1)

            jobs = build_artwork_generation_jobs(self.conn, media, limit=5, bumper_hashes=set())

        self.assertEqual([payload["video_id"] for _action, payload in jobs], ["telegram:trying"])

    def test_transient_failures_never_park_a_video(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir)
            self._seed(media, "telegram:transient", ARTWORK_FRAME_FAILURE_LIMIT + 2,
                       failure_code="artwork_stream_unavailable")

            jobs = build_artwork_generation_jobs(self.conn, media, limit=5, bumper_hashes=set())

        self.assertEqual([payload["video_id"] for _action, payload in jobs], ["telegram:transient"])


if __name__ == "__main__":
    unittest.main()
