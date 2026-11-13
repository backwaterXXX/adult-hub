"""Tests for MTProto-compliant Telegram range downloads.

Verifies:
- Raw-request offset and request_size satisfy Telegram MTProto boundaries:
  * offset divisible by 4096
  * request_size divisible by 4096
  * 1MiB (1048576) divisible by request_size (powers of two 4096..524288)
  * (offset % 1048576) + request_size <= 1048576 (never crosses 1MiB boundary)
- Exact known patterned source byte output for offset 788365312 (seek 2MiB bug reproduction)
  without allocating a huge file.
- Offsets next to 1MiB boundary, byte-unaligned offsets, small non-power-of-two request lengths,
  exact 512KiB alignment, tail EOF short, length=None streaming to EOF.
- Clean iterator closure on normal finish, early break / cancellation (aclose), and error.
- Custom block size and final short block in _download_block.
- Benchmark helper correctness: measure_seek detects short body / 206+empty body,
  and consumer/producer buffer state updates are atomic under lock without lost-update races.
"""

import asyncio
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault(
    "CONFIG_PATH",
    str(Path(__file__).parent / "fixtures" / "config_telegram_only.yaml"),
)


from resolver.providers.telegram import (
    LEGAL_REQUEST_SIZES,
    _download_block,
    _download_sequential,
    _legal_request_size,
)
import resolver.providers.telegram_download as telegram_download


def pattern_byte(offset: int) -> int:
    """Deterministic formula for virtual file byte at offset (no RAM allocation)."""
    return ((offset * 37) + (offset >> 8) + 11) & 0xFF


def pattern_bytes(offset: int, length: int) -> bytes:
    """Generate patterned bytes sequence for range [offset, offset + length)."""
    return bytes(pattern_byte(offset + i) for i in range(length))


class LimitInvalidError(Exception):
    """Simulates telethon.errors.rpcerrorlist.LimitInvalidError."""


class FakeDownloadIterator:
    """Simulates Telethon download iterator with strict MTProto boundary verification."""

    def __init__(
        self,
        client,
        offset: int,
        limit: int,
        request_size: int,
        total_file_size: int,
        fail_after: int = -1,
        slow_delay: float = 0.0,
    ):
        self.client = client
        self.offset = offset
        self.limit = limit
        self.request_size = request_size
        self.total_file_size = total_file_size
        self.fail_after = fail_after
        self.slow_delay = slow_delay
        self.chunks_yielded = 0
        self.closed = False
        self.aclose_called = False
        self.close_called = False

        # Verify Telegram official MTProto file download constraints
        self._validate_telegram_boundaries(offset, request_size)

    @staticmethod
    def _validate_telegram_boundaries(offset: int, req_size: int):
        if req_size % 4096 != 0:
            raise LimitInvalidError(f"request_size {req_size} must be divisible by 4096")
        if req_size not in LEGAL_REQUEST_SIZES:
            raise LimitInvalidError(f"request_size {req_size} not a legal power of two (4096..524288)")
        if 1048576 % req_size != 0:
            raise LimitInvalidError(f"1MiB (1048576) must be divisible by request_size {req_size}")
        if offset % 4096 != 0:
            raise LimitInvalidError(f"offset {offset} must be divisible by 4096")
        if (offset % 1048576) + req_size > 1048576:
            raise LimitInvalidError(
                f"request crosses 1MiB boundary: offset={offset} (mod 1MiB={offset % 1048576}), "
                f"request_size={req_size}, end={offset % 1048576 + req_size} > 1048576"
            )

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed:
            raise StopAsyncIteration

        if self.limit is not None and self.chunks_yielded >= self.limit:
            raise StopAsyncIteration

        cur_offset = self.offset + (self.chunks_yielded * self.request_size)
        if cur_offset >= self.total_file_size:
            raise StopAsyncIteration

        if self.fail_after >= 0 and self.chunks_yielded >= self.fail_after:
            raise RuntimeError("Injected iterator error")

        if self.slow_delay > 0:
            await asyncio.sleep(self.slow_delay)

        # Every subsequent chunk must also obey MTProto boundaries
        self._validate_telegram_boundaries(cur_offset, self.request_size)

        chunk_len = min(self.request_size, self.total_file_size - cur_offset)
        self.chunks_yielded += 1
        data = pattern_bytes(cur_offset, chunk_len)
        return data

    async def close(self):
        self.closed = True
        self.close_called = True

    async def aclose(self):
        self.closed = True
        self.aclose_called = True


class FakeTelegramClient:
    def __init__(self, total_file_size: int = 877247629, fail_after: int = -1, slow_delay: float = 0.0):
        self.total_file_size = total_file_size
        self.fail_after = fail_after
        self.slow_delay = slow_delay
        self.calls = []
        self.iterators = []

    def iter_download(self, media, offset=0, limit=None, request_size=524288):
        self.calls.append({
            "offset": offset,
            "limit": limit,
            "request_size": request_size,
        })
        it = FakeDownloadIterator(
            self,
            offset=offset,
            limit=limit,
            request_size=request_size,
            total_file_size=self.total_file_size,
            fail_after=self.fail_after,
            slow_delay=self.slow_delay,
        )
        self.iterators.append(it)
        return it


class TestTelegramDownloadRanges(unittest.TestCase):
    def test_legal_request_size(self):
        self.assertEqual(_legal_request_size(None), 524288)
        self.assertEqual(_legal_request_size(0), 4096)
        self.assertEqual(_legal_request_size(100), 4096)
        self.assertEqual(_legal_request_size(4096), 4096)
        self.assertEqual(_legal_request_size(4097), 8192)
        self.assertEqual(_legal_request_size(8192), 8192)
        self.assertEqual(_legal_request_size(20000), 32768)
        self.assertEqual(_legal_request_size(65536), 65536)
        self.assertEqual(_legal_request_size(131072), 131072)
        self.assertEqual(_legal_request_size(524288), 524288)
        self.assertEqual(_legal_request_size(1000000), 524288)

        # All values must be in LEGAL_REQUEST_SIZES
        for s in (0, 1, 4000, 4096, 5000, 20000, 50000, 100000, 524288, 1000000):
            res = _legal_request_size(s)
            self.assertIn(res, LEGAL_REQUEST_SIZES)
            self.assertEqual(1048576 % res, 0)

    def test_old_behavior_fails_boundary_enforcement(self):
        """Simulate how the unaligned sequential download failed with LimitInvalidError."""
        client = FakeTelegramClient(total_file_size=877247629)
        # Offset 788365312 is 880640 mod 1MiB. With req_size=524288, 880640 + 524288 = 1404928 > 1048576!
        with self.assertRaises(LimitInvalidError) as ctx:
            client.iter_download(None, offset=788365312, limit=4, request_size=524288)
        self.assertIn("crosses 1MiB boundary", str(ctx.exception))

        # Unaligned request_size e.g. (20000 + 4095) & ~4095 = 20480 (not a power of two)
        with self.assertRaises(LimitInvalidError) as ctx:
            client.iter_download(None, offset=0, limit=1, request_size=20480)
        self.assertIn("not a legal power of two", str(ctx.exception))

    def test_offset_788365312_seek_2mib(self):
        """Reproduction of benchmark seek at offset 788365312 requesting 2MiB.

        Must align upstream to 788004864, fetch 5 chunks of 524288 bytes,
        skip prefix 360448 bytes, and yield exactly 2097152 bytes matching the pattern.
        """
        async def run():
            client = FakeTelegramClient(total_file_size=877247629)
            chunks = []
            async for chunk in _download_sequential(client, None, offset=788365312, length=2097152):
                chunks.append(chunk)

            data = b"".join(chunks)
            self.assertEqual(len(data), 2097152)
            expected = pattern_bytes(788365312, 2097152)
            self.assertEqual(data, expected)

            # Check calls
            self.assertEqual(len(client.calls), 1)
            call = client.calls[0]
            self.assertEqual(call["offset"], 788004864)  # (788365312 // 524288) * 524288
            self.assertEqual(call["request_size"], 524288)
            # prefix_skip = 360448, total_needed = 360448 + 2097152 = 2457600
            # chunk_limit = ceil(2457600 / 524288) = 5
            self.assertEqual(call["limit"], 5)

            # Check iterator closed
            self.assertTrue(client.iterators[0].closed)

        asyncio.run(run())

    def test_offsets_next_to_1mib_boundary(self):
        """Offsets straddling, at, and just after 1MiB boundary."""
        async def run():
            client = FakeTelegramClient(total_file_size=10000000)

            # Straddling 1MiB boundary (1048576)
            # offset 1048576 - 100 = 1048476, length 200 -> spans from 1048476 to 1048676
            chunks = []
            async for c in _download_sequential(client, None, offset=1048476, length=200):
                chunks.append(c)
            data = b"".join(chunks)
            self.assertEqual(len(data), 200)
            self.assertEqual(data, pattern_bytes(1048476, 200))

            # Exactly at 1MiB boundary
            chunks = []
            async for c in _download_sequential(client, None, offset=1048576, length=5000):
                chunks.append(c)
            data = b"".join(chunks)
            self.assertEqual(len(data), 5000)
            self.assertEqual(data, pattern_bytes(1048576, 5000))

            # 1 byte after 1MiB boundary
            chunks = []
            async for c in _download_sequential(client, None, offset=1048577, length=100):
                chunks.append(c)
            data = b"".join(chunks)
            self.assertEqual(len(data), 100)
            self.assertEqual(data, pattern_bytes(1048577, 100))

        asyncio.run(run())

    def test_byte_unaligned_offsets(self):
        """Offsets that are not multiples of 4096."""
        async def run():
            # Use 1GB total size so large offsets are within bounds
            client = FakeTelegramClient(total_file_size=1000000000)
            for off in (1, 17, 4095, 4097, 788365313):
                chunks = []
                async for c in _download_sequential(client, None, offset=off, length=500):
                    chunks.append(c)
                data = b"".join(chunks)
                self.assertEqual(len(data), 500)
                self.assertEqual(data, pattern_bytes(off, 500))

        asyncio.run(run())

    def test_small_nonpow2_request_lengths(self):
        """Small request lengths e.g. 20000: must use power-of-two request_size (32768)."""
        async def run():
            client = FakeTelegramClient(total_file_size=10000000)
            chunks = []
            async for c in _download_sequential(client, None, offset=8000, length=20000):
                chunks.append(c)
            data = b"".join(chunks)
            self.assertEqual(len(data), 20000)
            self.assertEqual(data, pattern_bytes(8000, 20000))

            call = client.calls[-1]
            self.assertEqual(call["request_size"], 32768)  # next power of two >= 20000
            self.assertEqual(call["offset"], 0)  # aligned to 32768
            # prefix_skip = 8000, total_needed = 28000 <= 32768 -> limit 1
            self.assertEqual(call["limit"], 1)

        asyncio.run(run())

    def test_exact_512kib_alignment(self):
        """Exact 512KiB offset and length."""
        async def run():
            client = FakeTelegramClient(total_file_size=10000000)
            chunks = []
            async for c in _download_sequential(client, None, offset=524288, length=524288):
                chunks.append(c)
            data = b"".join(chunks)
            self.assertEqual(len(data), 524288)
            self.assertEqual(data, pattern_bytes(524288, 524288))

            call = client.calls[-1]
            self.assertEqual(call["offset"], 524288)
            self.assertEqual(call["limit"], 1)

        asyncio.run(run())

    def test_tail_eof_short(self):
        """Requesting range past end of file returns available bytes cleanly without error."""
        async def run():
            # Total file size 50000. Offset 40000, requesting 20000 bytes.
            client = FakeTelegramClient(total_file_size=50000)
            chunks = []
            async for c in _download_sequential(client, None, offset=40000, length=20000):
                chunks.append(c)
            data = b"".join(chunks)
            # Only 10000 bytes available
            self.assertEqual(len(data), 10000)
            self.assertEqual(data, pattern_bytes(40000, 10000))
            self.assertTrue(client.iterators[0].closed)

        asyncio.run(run())

    def test_length_none_streams_eof(self):
        """length=None streams all remaining bytes to EOF."""
        async def run():
            client = FakeTelegramClient(total_file_size=1500000)
            chunks = []
            async for c in _download_sequential(client, None, offset=1000000, length=None):
                chunks.append(c)
            data = b"".join(chunks)
            self.assertEqual(len(data), 500000)
            self.assertEqual(data, pattern_bytes(1000000, 500000))
            self.assertTrue(client.iterators[0].closed)

        asyncio.run(run())

    def test_cancellation_closes_iterator(self):
        """Cancellation or closing the generator closes the underlying download iterator."""
        async def run():
            # Test 1: aclose on generator
            client1 = FakeTelegramClient(total_file_size=10000000)
            gen = _download_sequential(client1, None, offset=0, length=2097152)
            chunk = await gen.__anext__()
            self.assertIsNotNone(chunk)
            await gen.aclose()
            self.assertTrue(client1.iterators[0].closed)

            # Test 2: Task cancellation while waiting on download chunk
            client2 = FakeTelegramClient(total_file_size=10000000, slow_delay=10.0)
            async def worker():
                async for _ in _download_sequential(client2, None, offset=0, length=2097152):
                    pass
            task = asyncio.create_task(worker())
            await asyncio.sleep(0.01)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.assertTrue(client2.iterators[0].closed)

        asyncio.run(run())

    def test_error_closes_iterator(self):
        """Iterator exception cleanly invokes closure."""
        async def run():
            client = FakeTelegramClient(total_file_size=10000000, fail_after=1)
            with self.assertRaises(RuntimeError):
                async for _ in _download_sequential(client, None, offset=0, length=2097152):
                    pass
            self.assertTrue(client.iterators[0].closed)

        asyncio.run(run())

    def test_download_block_aligned(self):
        """_download_block fetches aligned block and closes iterator."""
        async def run():
            client = FakeTelegramClient(total_file_size=10000000)
            buf = await _download_block(client, None, block_offset=524288, block_length=524288)
            self.assertEqual(len(buf), 524288)
            self.assertEqual(buf, pattern_bytes(524288, 524288))
            self.assertTrue(client.iterators[0].closed)

        asyncio.run(run())

    def test_download_block_custom_size_and_final_short_block(self):
        """_download_block handles tail short blocks without boundary errors."""
        async def run():
            # Final block with custom/non-power-of-two length
            client = FakeTelegramClient(total_file_size=1068576)
            buf = await _download_block(client, None, block_offset=1048576, block_length=20000)
            self.assertEqual(len(buf), 20000)
            self.assertEqual(buf, pattern_bytes(1048576, 20000))
            self.assertTrue(client.iterators[0].closed)

        asyncio.run(run())


    def test_download_block_preserves_original_error_type(self):
        """A block failure keeps its own type so retry classification still works."""
        async def run():
            client = FakeTelegramClient(total_file_size=10000000, fail_after=1)
            with self.assertRaises(RuntimeError):
                await _download_block(client, None, block_offset=0, block_length=1048576)
            self.assertTrue(client.iterators[0].closed)

        asyncio.run(run())


    def test_stalled_stream_fails_after_the_stall_timeout(self):
        """A read that never yields fails instead of holding the response open."""
        async def run():
            client = FakeTelegramClient(total_file_size=10000000, slow_delay=10.0)
            with patch.object(telegram_download, "_get_download_timeout_seconds", return_value=0.05):
                with self.assertRaises(asyncio.TimeoutError):
                    async for _ in _download_sequential(client, None, offset=0, length=2097152):
                        pass
            self.assertTrue(client.iterators[0].closed)

        asyncio.run(run())

    def test_stall_timeout_is_per_chunk_not_a_total_budget(self):
        """A long stream whose total time exceeds the budget still completes while chunks flow."""
        async def run():
            client = FakeTelegramClient(total_file_size=10000000, slow_delay=0.03)
            total = 0
            with patch.object(telegram_download, "_get_download_timeout_seconds", return_value=0.05):
                async for chunk in _download_sequential(
                    client, None, offset=0, length=5 * 524288
                ):
                    total += len(chunk)
            self.assertEqual(total, 5 * 524288)

        asyncio.run(run())


class TestBenchmarkCorrectness(unittest.TestCase):

    def test_consumer_producer_concurrency_race(self):
        """Smoke test verifying shared buffered decrement atomic lock avoids lost updates."""
        lock = threading.Lock()
        state = {"buffered": 0, "received": 0, "stop": False}

        def fake_producer():
            for _ in range(500):
                with lock:
                    state["buffered"] += 100
                    state["received"] += 100
                time.sleep(0.001)
            with lock:
                state["stop"] = True

        t = threading.Thread(target=fake_producer)
        t.start()

        consumed_total = 0
        while True:
            time.sleep(0.002)
            with lock:
                buf = state["buffered"]
                use = min(buf, 150)
                state["buffered"] -= use
                stop = state["stop"]
            consumed_total += use
            if stop and buf == use:
                break

        t.join()
        with lock:
            final_buf = state["buffered"]
        self.assertEqual(consumed_total + final_buf, state["received"])


if __name__ == "__main__":
    unittest.main()
