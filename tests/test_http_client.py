import errno
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import Mock


from sync.http_client import fetch_json_with_retry, is_retryable_error, parse_retry_after, retry_after_from_error


class TestHttpClientRetryClassification(unittest.TestCase):
    def test_parses_retry_after_http_date_and_caps_delay(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        delay = parse_retry_after(format_datetime(future, usegmt=True), 3, cap=60)
        self.assertGreater(delay, 0)
        self.assertLessEqual(delay, 60)
        self.assertEqual(parse_retry_after("12", 3, cap=60), 12)

    def test_past_or_invalid_retry_after_uses_backoff_default(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        self.assertEqual(parse_retry_after(format_datetime(past, usegmt=True), 7), 7)
        self.assertEqual(parse_retry_after("not a date", 7), 7)

    def test_http_date_header_controls_retry_delay(self):
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=20)
        error = RuntimeError("429 Too Many Requests")
        error.response = SimpleNamespace(headers={
            "retry-after": format_datetime(retry_at, usegmt=True),
        })
        calls = []
        sleeps = []

        def get(_url, *, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise error
            return b'{"ok": true}'

        result = fetch_json_with_retry(
            "https://example.invalid/status",
            http_get_fn=get,
            logger=Mock(),
            sleep_fn=sleeps.append,
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertGreater(sleeps[0], 0)
        self.assertLessEqual(sleeps[0], 20)

    def test_urllib_http_error_retry_after_header_is_honored(self):
        error = HTTPError(
            "https://example.invalid/status",
            429,
            "Too Many Requests",
            {"Retry-After": "17"},
            None,
        )
        self.assertEqual(retry_after_from_error(error, 3), 17)
        self.assertTrue(is_retryable_error(error))

        sleeps = []
        calls = []

        def get(_url, *, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise error
            return b'{"ok": true}'

        result = fetch_json_with_retry(
            "https://example.invalid/status",
            http_get_fn=get,
            logger=Mock(),
            sleep_fn=sleeps.append,
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(sleeps, [17])

    def test_detects_transient_errno_through_wrapped_exception(self):
        try:
            raise OSError(errno.ECONNREFUSED, "Connection refused")
        except OSError as cause:
            try:
                raise RuntimeError("request failed") from cause
            except RuntimeError as error:
                self.assertTrue(is_retryable_error(error))

    def test_does_not_retry_permanent_os_errors(self):
        self.assertFalse(is_retryable_error(OSError(errno.EINVAL, "Invalid argument")))

    def test_retries_wrapped_connection_refusal_then_succeeds(self):
        calls = []
        sleeps = []

        def get(_url, *, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                try:
                    raise OSError(errno.ECONNREFUSED, "Connection refused")
                except OSError as cause:
                    raise RuntimeError("request failed") from cause
            return b'{"ok": true}'

        result = fetch_json_with_retry(
            "https://example.invalid/status",
            max_retries=3,
            http_get_fn=get,
            logger=Mock(),
            sleep_fn=sleeps.append,
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [20.0, 20.0])
        self.assertEqual(sleeps, [3])

    def test_stops_after_max_attempts_for_nonretryable_error(self):
        calls = []

        def get(_url, *, timeout):
            calls.append(timeout)
            raise OSError(errno.EINVAL, "Invalid argument")

        with self.assertRaises(OSError):
            fetch_json_with_retry(
                "https://example.invalid/status",
                max_retries=3,
                http_get_fn=get,
                logger=Mock(),
                sleep_fn=Mock(),
            )
        self.assertEqual(calls, [20.0])


if __name__ == "__main__":
    unittest.main()
