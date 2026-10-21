"""Unit tests for bounded Telegram retry helpers (no telethon required)."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resolver" / "providers"))

import telegram_retry


class FloodWaitError(Exception):
    def __init__(self, seconds):
        super().__init__(f"A wait of {seconds} seconds is required")
        self.seconds = seconds


class FileReferenceExpiredError(Exception):
    pass


class _NoSleep:
    def __init__(self):
        self.waits = []

    async def sleep(self, seconds):
        self.waits.append(seconds)


class RetryStreamTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def setUp(self):
        self._real_sleep = telegram_retry.asyncio.sleep
        self.sleeper = _NoSleep()
        telegram_retry.asyncio.sleep = self.sleeper.sleep
        self.addCleanup(lambda: setattr(telegram_retry.asyncio, "sleep", self._real_sleep))

    async def _collect(self, factory, **kwargs):
        return [chunk async for chunk in telegram_retry.retry_stream(factory, **kwargs)]

    def test_retries_and_yields_full_sequence_when_nothing_sent_yet(self):
        attempts = []

        async def factory():
            attempts.append(1)
            if len(attempts) < 2:
                raise ConnectionResetError("connection reset by peer")
            for chunk in (b"a", b"b"):
                await asyncio.sleep(0)
                yield chunk

        chunks = self.run_async(self._collect(factory))
        self.assertEqual(chunks, [b"a", b"b"])
        self.assertEqual(len(attempts), 2)

    def test_no_retry_after_first_byte_delivered(self):
        attempts = []

        async def factory():
            attempts.append(1)
            yield b"first"
            raise ConnectionResetError("mid-stream reset")

        with self.assertRaises(ConnectionResetError):
            self.run_async(self._collect(factory))
        self.assertEqual(len(attempts), 1)

    def test_flood_wait_is_respected_and_logged(self):
        attempts = []

        async def factory():
            attempts.append(1)
            if len(attempts) < 3:
                raise FloodWaitError(2)
            yield b"ok"

        chunks = self.run_async(self._collect(factory))
        self.assertEqual(chunks, [b"ok"])
        self.assertEqual(self.sleeper.waits[:2], [2.0, 2.0])

    def test_flood_wait_beyond_cap_is_not_retried(self):
        async def factory():
            raise FloodWaitError(int(telegram_retry.MAX_FLOOD_WAIT_SECONDS) + 1)
            yield b"never"

        with self.assertRaises(FloodWaitError):
            self.run_async(self._collect(factory))
        self.assertEqual(self.sleeper.waits, [])

    def test_unknown_errors_propagate_immediately(self):
        async def factory():
            raise RuntimeError("session not authorized")
            yield b"never"

        with self.assertRaises(RuntimeError):
            self.run_async(self._collect(factory))

    def test_attempt_limit_stops_persistent_failures(self):
        attempts = []

        async def factory():
            attempts.append(1)
            raise ConnectionResetError("always down")
            yield b"never"

        with self.assertRaises(ConnectionResetError):
            self.run_async(self._collect(factory))
        self.assertEqual(len(attempts), telegram_retry.MAX_ATTEMPTS)

    def test_on_retry_hook_runs_between_attempts(self):
        seen = []

        async def factory():
            if not seen:
                raise FileReferenceExpiredError("expired")
            yield b"ok"

        chunks = self.run_async(self._collect(
            factory, on_retry=lambda exc, attempt: seen.append(type(exc).__name__)))
        self.assertEqual(chunks, [b"ok"])
        self.assertEqual(seen, ["FileReferenceExpiredError"])

    def test_total_wait_budget_blocks_slow_flood(self):
        async def factory():
            raise FloodWaitError(int(telegram_retry.MAX_TOTAL_WAIT_SECONDS))
            yield b"never"

        with self.assertRaises(FloodWaitError):
            self.run_async(self._collect(factory))


class RetryCallTests(unittest.TestCase):
    def setUp(self):
        self._real_sleep = telegram_retry.asyncio.sleep
        self.sleeper = _NoSleep()
        telegram_retry.asyncio.sleep = self.sleeper.sleep
        self.addCleanup(lambda: setattr(telegram_retry.asyncio, "sleep", self._real_sleep))

    def test_call_retries_then_returns(self):
        calls = []

        async def func():
            calls.append(1)
            if len(calls) < 2:
                raise asyncio.TimeoutError()
            return b"data"

        result = asyncio.run(telegram_retry.retry_call(func))
        self.assertEqual(result, b"data")
        self.assertEqual(len(calls), 2)

    def test_retryable_filter_can_block_file_reference_retries(self):
        calls = []

        async def func():
            calls.append(1)
            raise FileReferenceExpiredError("stale media object")

        with self.assertRaises(FileReferenceExpiredError):
            asyncio.run(telegram_retry.retry_call(
                func, retryable=lambda exc: not isinstance(exc, FileReferenceExpiredError)))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
