"""Artwork extraction retries and the temporary files it must not leave behind."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sync import duplicate_job_external_handlers as handlers


class ArtworkExtractionRetryTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(handlers, "ARTWORK_RETRY_DELAY_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _extract(self, outcomes):
        calls = []

        def fake_extract(stream_url, dest, *, offset_seconds, timeout):
            calls.append((stream_url, dest, offset_seconds, timeout))
            return outcomes[len(calls) - 1]

        return fake_extract, calls

    def test_a_transient_failure_is_retried_once_and_can_succeed(self):
        # A resolver that just restarted answers 502 for a cold channel, then serves
        # the same stream; the retry must turn that into artwork.
        extract, calls = self._extract([
            (False, "ffmpeg failed to extract a video frame from stream."),
            (True, ""),
        ])

        success, error = handlers._extract_artwork_frame(
            extract, "http://resolver/admin/play/x/stream", "dest.jpg", offset=60, timeout=20,
        )

        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][2:], (60, 20))

    def test_a_persistent_failure_reports_the_last_error_after_two_attempts(self):
        extract, calls = self._extract([
            (False, "first failure"),
            (False, "second failure"),
        ])

        success, error = handlers._extract_artwork_frame(
            extract, "http://resolver/admin/play/x/stream", "dest.jpg", offset=60, timeout=20,
        )

        self.assertFalse(success)
        self.assertEqual(error, "second failure")
        self.assertEqual(len(calls), 2)

    def test_the_number_of_attempts_is_bounded(self):
        extract, calls = self._extract([(False, "nope")] * 5)

        success, _error = handlers._extract_artwork_frame(
            extract, "http://resolver/admin/play/x/stream", "dest.jpg", offset=60, timeout=20,
        )

        self.assertFalse(success)
        self.assertEqual(len(calls), handlers.ARTWORK_EXTRACTION_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
