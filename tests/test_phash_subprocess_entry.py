"""Tests for resolver.phash_subprocess_entry."""

from __future__ import annotations

import base64
import io
import json
import runpy
import unittest
from unittest.mock import patch

import resolver.phash_subprocess_entry as phash_subprocess_entry
from common.telegram_transfer_budget import (
    DailyTelegramTransferBudgetExceeded,
    DailyTelegramTransferBudgetUnavailable,
)
from sync.video_fingerprints import FingerprintBudgetExceeded


class PhashSubprocessEntryTest(unittest.TestCase):
    def setUp(self):
        self.base_payload = {
            "video_id": "telegram:-100:1:7",
            "duration_us": 120_000_000,
            "byte_budget": 50_000_000,
            "timeout_seconds": 30,
            "cache_dir": "/tmp/cache",
            "cache_id": "cache-123",
            "job_kind": "combined",
            "playback_bytes_per_second": 500_000,
            "idle_bytes_per_second": 2_000_000,
        }

    def test_main_combined_success(self):
        thumb_bytes = b"fake-jpeg-data"
        fingerprint = "0123456789abcdef" * 4

        stdin_data = json.dumps(self.base_payload)
        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("resolver.phash_subprocess_entry.telegram_phash") as mock_phash, \
             patch("resolver.phash_subprocess_entry.has_active_playback") as mock_playback:
            mock_phash.return_value = (fingerprint, 12345, thumb_bytes)
            mock_playback.return_value = True

            phash_subprocess_entry.main()

            rate_cb = mock_phash.call_args[1]["max_bytes_per_second"]
            self.assertEqual(rate_cb(), 500_000)

            mock_playback.return_value = False
            self.assertEqual(rate_cb(), 2_000_000)

            result = json.loads(mock_stdout.getvalue())
            self.assertTrue(result["ok"])
            self.assertEqual(result["fingerprint"], fingerprint)
            self.assertEqual(result["bytes_read"], 12345)
            self.assertEqual(base64.b64decode(result["thumbnail"]), thumb_bytes)

    def test_main_phash_only_success(self):
        self.base_payload["job_kind"] = "phash"
        fingerprint = "abcdef0123456789" * 4

        stdin_data = json.dumps(self.base_payload)
        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("resolver.phash_subprocess_entry.telegram_phash") as mock_phash:
            mock_phash.return_value = (fingerprint, 98765, None)

            phash_subprocess_entry.main()

            self.assertFalse(mock_phash.call_args[1]["include_thumbnail"])
            self.assertFalse(mock_phash.call_args[1]["thumbnail_only"])

            result = json.loads(mock_stdout.getvalue())
            self.assertTrue(result["ok"])
            self.assertEqual(result["fingerprint"], fingerprint)
            self.assertEqual(result["bytes_read"], 98765)
            self.assertIsNone(result["thumbnail"])

    def test_main_thumbnail_only_success(self):
        self.base_payload["job_kind"] = "thumbnail"
        thumb_bytes = b"thumbnail-only-data"

        stdin_data = json.dumps(self.base_payload)
        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("resolver.phash_subprocess_entry.telegram_phash") as mock_phash:
            mock_phash.return_value = (None, 4321, thumb_bytes)

            phash_subprocess_entry.main()

            self.assertTrue(mock_phash.call_args[1]["include_thumbnail"])
            self.assertTrue(mock_phash.call_args[1]["thumbnail_only"])

            result = json.loads(mock_stdout.getvalue())
            self.assertTrue(result["ok"])
            self.assertIsNone(result["fingerprint"])
            self.assertEqual(result["bytes_read"], 4321)
            self.assertEqual(base64.b64decode(result["thumbnail"]), thumb_bytes)

    def test_main_combined_missing_thumbnail_raises_value_error(self):
        self.base_payload["job_kind"] = "combined"

        stdin_data = json.dumps(self.base_payload)
        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("resolver.phash_subprocess_entry.telegram_phash") as mock_phash:
            mock_phash.return_value = ("some_fp", 1000, None)

            phash_subprocess_entry.main()

            result = json.loads(mock_stdout.getvalue())
            self.assertEqual(result, {"ok": False, "error_code": "invalid_video", "retryable": False})

    def test_main_handled_exceptions(self):
        cases = [
            (DailyTelegramTransferBudgetExceeded(), "daily_transfer_budget_exhausted", False),
            (DailyTelegramTransferBudgetUnavailable(), "transfer_budget_unavailable", False),
            (FingerprintBudgetExceeded(), "byte_budget_exhausted", False),
            (TimeoutError(), "timeout", True),
            (OSError("disk read failed"), "source_unavailable", True),
            (ValueError("corrupt mp4"), "invalid_video", False),
        ]
        for exc, expected_code, retryable in cases:
            with self.subTest(exc=type(exc).__name__):
                stdin_data = json.dumps(self.base_payload)
                with patch("sys.stdin", io.StringIO(stdin_data)), \
                     patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
                     patch("resolver.phash_subprocess_entry.telegram_phash") as mock_phash:
                    mock_phash.side_effect = exc

                    phash_subprocess_entry.main()

                    result = json.loads(mock_stdout.getvalue())
                    self.assertEqual(
                        result,
                        {"ok": False, "error_code": expected_code, "retryable": retryable},
                    )

    def test_main_unhandled_exception(self):
        stdin_data = json.dumps(self.base_payload)
        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("resolver.phash_subprocess_entry.telegram_phash") as mock_phash:
            mock_phash.side_effect = RuntimeError("unexpected native crash")

            phash_subprocess_entry.main()

            result = json.loads(mock_stdout.getvalue())
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "generation_failed")
            self.assertTrue(result["retryable"])
            self.assertIn("RuntimeError: unexpected native crash", result["error_detail"])

    def test_module_executed_as_main(self):
        stdin_data = json.dumps(self.base_payload)
        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_stdout, \
             patch("sync.video_fingerprints.telegram_phash") as mock_phash:
            mock_phash.return_value = ("fp123", 50, b"thumb")

            runpy.run_module("resolver.phash_subprocess_entry", run_name="__main__")

            result = json.loads(mock_stdout.getvalue())
            self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
