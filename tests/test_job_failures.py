"""Safe operator-facing job failure classifications."""

import unittest

from common.job_failures import classify_job_failure


class TestJobFailureClassification(unittest.TestCase):
    def test_artwork_failures_map_to_safe_actionable_reasons(self):
        cases = (
            ("Target media file not found in library.", "artwork_source_missing"),
            ("Artwork destination must not be a symlink.", "artwork_destination_unsafe"),
            ("Failed to generate artwork: HTTP 503 from https://private.invalid/token", "artwork_source_unavailable"),
            ("Failed to generate artwork: invalid frame at /private/library/video.mp4", "artwork_frame_extraction_failed"),
        )
        for error, expected in cases:
            with self.subTest(error=error):
                self.assertEqual(classify_job_failure("artwork_generate", {"error": error}), expected)

    def test_known_codes_are_allowlisted_and_unknown_text_is_not_returned(self):
        self.assertEqual(
            classify_job_failure("artwork_generate", {"failure_code": "artwork_frame_extraction_failed"}),
            "artwork_frame_extraction_failed",
        )
        self.assertIsNone(classify_job_failure("artwork_generate", {"failure_code": "private-token"}))
        self.assertIsNone(classify_job_failure("performer_merge", {"error": "Unclassified private-token error"}))


if __name__ == "__main__":
    unittest.main()
