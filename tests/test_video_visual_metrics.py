"""Pure visual comparison helpers: distances, sections, offsets, and anchors."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sync import video_visual


def _frame(value: int) -> bytes:
    return bytes([value]) * (160 * 90)


class VisualMetricTests(unittest.TestCase):
    def test_source_key_changes_with_each_identity_field(self):
        base = {"id": "a", "duration": 100, "file_size_bytes": 200, "oshash": "abc"}
        self.assertEqual(len(video_visual.source_key(base)), 64)
        for key, value in (("id", "b"), ("duration", 101), ("file_size_bytes", 201), ("oshash", "def")):
            changed = {**base, key: value}
            self.assertNotEqual(video_visual.source_key(base), video_visual.source_key(changed))

    def test_distance_handles_identical_different_and_invalid_values(self):
        self.assertEqual(video_visual._distance("00ff", "00ff"), 0)
        self.assertEqual(video_visual._distance("0000", "0001"), 1)
        self.assertEqual(video_visual._distance("ffff", "0000"), 16)
        self.assertEqual(video_visual._distance("not-hex", "0000"), 64)

    def test_ssim_accepts_identical_frames_and_rejects_wrong_sizes(self):
        frame = _frame(64)
        self.assertAlmostEqual(video_visual._ssim(frame, frame), 1.0, places=6)
        self.assertEqual(video_visual._ssim(frame, b"short"), 0.0)

    def test_section_coverage_marks_loose_and_strict_thirds(self):
        loose = [
            {"left_t": 10, "match": True},
            {"left_t": 50, "match": False},
            {"left_t": 90, "match": True},
        ]
        thirds, strict = video_visual._section_coverage(loose, 100)
        self.assertEqual(thirds, {"beginning": True, "middle": True, "end": True})
        self.assertEqual(strict, {"beginning": True, "middle": False, "end": True})

    def test_offset_fit_returns_slope_residual_and_predictions(self):
        slope, intercept, residual, predicted = video_visual._offset_fit(
            [(10, 1.0), (20, 1.5), (30, 2.0)], [10, 20, 30],
        )
        self.assertAlmostEqual(slope, 0.05)
        self.assertAlmostEqual(intercept, 0.5)
        self.assertLess(residual, 1e-9)
        self.assertEqual(len(predicted), 3)
        self.assertEqual(video_visual._offset_fit([], [10]), (None, None, 99.0, []))

    def test_identity_is_deterministic_and_order_sensitive(self):
        self.assertEqual(video_visual._identity("a", "b"), video_visual._identity("a", "b"))
        self.assertNotEqual(video_visual._identity("a", "b"), video_visual._identity("b", "a"))
        self.assertEqual(len(video_visual._identity("a", "b")), 20)

    def test_best_coarse_offset_prefers_the_matching_delta(self):
        left = [_frame(index + 1) for index in range(8)]
        anchors = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]
        offsets = [-1.0, 0.0, 1.0]
        indices = (0, 2, 5, 7)
        by_time = {}
        for delta in offsets:
            for index in indices:
                by_time[round(anchors[index] + delta, 3)] = (
                    left[index] if delta == 1.0 else _frame(250)
                )
        with patch.object(video_visual, "_ssim", side_effect=lambda a, b: 1.0 if a == b else 0.0):
            self.assertEqual(
                video_visual._best_coarse_offset(left, anchors, offsets, indices, by_time), 1.0,
            )

    def test_anchor_matches_scores_offsets_and_tracks_local_fit(self):
        left = [_frame(index + 1) for index in range(2)]
        anchors = [10.0, 20.0]
        offsets = [0.0, 1.0]
        right = [_frame(index + 1) for index in range(2)]
        by_time = {
            round(anchors[0] + 0.0, 3): right[0],
            round(anchors[0] + 1.0, 3): _frame(250),
            round(anchors[1] + 0.0, 3): _frame(250),
            round(anchors[1] + 1.0, 3): right[1],
        }
        with patch.object(video_visual, "_frame_hashes", return_value=("0" * 16, "0" * 16, True)), \
             patch.object(video_visual, "_ssim", side_effect=lambda a, b: 1.0 if a == b else 0.0):
            results, local = video_visual._anchor_matches(left, anchors, offsets, by_time)
        self.assertEqual([item["offset"] for item in results], [0.0, 1.0])
        self.assertTrue(all(item["match"] and item["loose"] for item in results))
        self.assertEqual(local, [(10.0, 0.0), (20.0, 1.0)])


if __name__ == "__main__":
    unittest.main()
