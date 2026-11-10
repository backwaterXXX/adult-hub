"""Deterministic tests for Milestone D: multi-account streaming concurrency, fairness, and safety."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch
from typing import List, Tuple

from resolver.providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    _MEMORY_CHANNEL_ACCESS,
    set_memory_only_mode,
    upsert_account,
)
from resolver.providers.telegram_router import TelegramAccountRouter
from resolver.telegram_stream import ParallelTelegramStreamManager


async def _all_accounts_authorized(account_ids):
    """Fixture session files are not real Telegram sessions; treat every listed account as authorized."""
    return list(account_ids)


class TestMilestoneDStreaming(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _MEMORY_ACCOUNTS.clear()
        _MEMORY_CHANNEL_ACCESS.clear()
        set_memory_only_mode(True)
        # Fixture sessions are not real Telegram sessions; the authorization filter is tested separately.
        auth_patch = patch(
            "resolver.providers.telegram_router.authorized_account_ids", _all_accounts_authorized
        )
        auth_patch.start()
        self.addCleanup(auth_patch.stop)

    def tearDown(self):
        set_memory_only_mode(False)

    async def test_independent_streams_two_mock_accounts(self):
        """Verify independent concurrent streams on two mock accounts deliver exact bytes without crosstalk."""
        mgr = ParallelTelegramStreamManager(
            workers=3,
            global_workers=6,
            block_size=1024,
            read_ahead_bytes=4096,
            max_cache_bytes=65536,
        )

        acc1_data = bytes(i % 256 for i in range(4096))
        acc2_data = bytes((255 - (i % 256)) for i in range(4096))

        acc1_fetches: List[Tuple[int, int]] = []
        acc2_fetches: List[Tuple[int, int]] = []

        async def fetch_acc1(offset: int, length: int) -> bytes:
            acc1_fetches.append((offset, length))
            await asyncio.sleep(0.005)
            return acc1_data[offset : offset + length]

        async def fetch_acc2(offset: int, length: int) -> bytes:
            acc2_fetches.append((offset, length))
            await asyncio.sleep(0.005)
            return acc2_data[offset : offset + length]

        async def stream_reader(key: str, total: int, fetch_fn, account_id: str) -> bytes:
            chunks = []
            async for chunk in mgr.stream(
                key=key,
                offset=0,
                length=total,
                total=total,
                fetch=fetch_fn,
                job_key=account_id,
                account_id=account_id,
            ):
                chunks.append(chunk)
            return b"".join(chunks)

        t1 = asyncio.create_task(stream_reader("tg:chan1:1:docA:4096", 4096, fetch_acc1, "acc_1"))
        t2 = asyncio.create_task(stream_reader("tg:chan2:2:docB:4096", 4096, fetch_acc2, "acc_2"))

        res1, res2 = await asyncio.gather(t1, t2)

        self.assertEqual(res1, acc1_data)
        self.assertEqual(res2, acc2_data)

        # Confirm each account only handled its own file's offsets
        self.assertEqual(len(acc1_fetches), 4)
        self.assertEqual(len(acc2_fetches), 4)
        for off, length in acc1_fetches:
            self.assertEqual(length, 1024)
        for off, length in acc2_fetches:
            self.assertEqual(length, 1024)

        await mgr.close()

    async def test_account_worker_limits_and_fairness(self):
        """Verify per-account worker limit prevents heavy account from starving light account."""
        # 1 global worker available for competition with account_workers=1
        mgr = ParallelTelegramStreamManager(
            workers=2,
            global_workers=2,
            account_workers=1,
            block_size=1024,
            read_ahead_bytes=2048,
            max_cache_bytes=32768,
        )

        acc1_running = 0
        acc1_max_running = 0
        acc2_running = 0
        acc2_max_running = 0

        async def fetch_acc1(offset: int, length: int) -> bytes:
            nonlocal acc1_running, acc1_max_running
            acc1_running += 1
            acc1_max_running = max(acc1_max_running, acc1_running)
            try:
                await asyncio.sleep(0.02)
                return b"1" * length
            finally:
                acc1_running -= 1

        async def fetch_acc2(offset: int, length: int) -> bytes:
            nonlocal acc2_running, acc2_max_running
            acc2_running += 1
            acc2_max_running = max(acc2_max_running, acc2_running)
            try:
                await asyncio.sleep(0.02)
                return b"2" * length
            finally:
                acc2_running -= 1

        async def reader(key: str, fetch_fn, account_id: str) -> bytes:
            chunks = []
            async for c in mgr.stream(
                key=key,
                offset=0,
                length=2048,
                total=2048,
                fetch=fetch_fn,
                job_key=account_id,
                account_id=account_id,
            ):
                chunks.append(c)
            return b"".join(chunks)

        t1 = asyncio.create_task(reader("tg:c1:1:dA:2048", fetch_acc1, "acc_heavy"))
        t2 = asyncio.create_task(reader("tg:c2:2:dB:2048", fetch_acc2, "acc_light"))

        res1, res2 = await asyncio.gather(t1, t2)
        self.assertEqual(len(res1), 2048)
        self.assertEqual(len(res2), 2048)

        # Neither account must exceed its account_workers bound of 1
        self.assertLessEqual(acc1_max_running, 1)
        self.assertLessEqual(acc2_max_running, 1)

        await mgr.close()

    async def test_playback_priority_demand_over_prefetch(self):
        """Verify priority 0 (demand block needed now) preempts priority 1 (prefetch)."""
        mgr = ParallelTelegramStreamManager(
            workers=1,
            global_workers=1,  # Single execution bottleneck
            block_size=1024,
            read_ahead_bytes=4096,  # Schedules prefetch blocks ahead
            max_cache_bytes=32768,
        )

        execution_order = []

        def make_fetch(stream_tag: str):
            async def fetch(offset: int, length: int) -> bytes:
                execution_order.append((stream_tag, offset))
                await asyncio.sleep(0.01)
                return b"X" * length
            return fetch

        # Reader 1 starts, reads first block (offset 0)
        stream1 = mgr.stream(
            key="tg:vid1:1:doc:8192",
            offset=0,
            length=8192,
            total=8192,
            fetch=make_fetch("stream1"),
            job_key="acc1",
        )
        it1 = stream1.__aiter__()
        chunk0 = await it1.__anext__()
        self.assertEqual(len(chunk0), 1024)

        # Reader 2 jumps in demand for another video at offset 0
        stream2 = mgr.stream(
            key="tg:vid2:2:doc:2048",
            offset=0,
            length=1024,
            total=2048,
            fetch=make_fetch("stream2"),
            job_key="acc1",
        )
        it2 = stream2.__aiter__()
        chunk2 = await it2.__anext__()
        self.assertEqual(len(chunk2), 1024)

        # Drain the stream after the demand read
        remaining_chunks = []
        async for chunk in it1:
            remaining_chunks.append(chunk)
        self.assertEqual(len(remaining_chunks), 7)

        # The demand request for stream2 (offset 0) must execute
        self.assertIn(("stream2", 0), execution_order)
        idx_stream2_demand = execution_order.index(("stream2", 0))

        # Assert that at least one prefetch ran before stream2 demand
        prefetch_before_demand = [
            (tag, offset)
            for tag, offset in execution_order[:idx_stream2_demand]
            if tag == "stream1" and offset > 0
        ]
        self.assertGreater(
            len(prefetch_before_demand),
            0,
            "At least one stream1 prefetch block must run before stream2 demand executes",
        )

        # Assert stream2 demand appears before all relevant stream1 prefetch offsets
        for offset in (2048, 3072, 4096):
            self.assertIn(
                ("stream1", offset),
                execution_order,
                f"stream1 prefetch offset {offset} must have executed",
            )
            self.assertLess(
                idx_stream2_demand,
                execution_order.index(("stream1", offset)),
                f"Demand block for stream2 must execute before stream1 prefetch offset {offset}",
            )
        await mgr.close()

    async def test_cancellation_aborts_inflight_fetch(self):
        """Verify client disconnect / stream cancellation cancels in-flight fetch tasks."""
        mgr = ParallelTelegramStreamManager(
            workers=2,
            global_workers=2,
            block_size=1024,
            max_cache_bytes=32768,
        )

        fetch_started = asyncio.Event()
        fetch_cancelled = False

        async def fetch(offset: int, length: int) -> bytes:
            nonlocal fetch_cancelled
            fetch_started.set()
            try:
                await asyncio.sleep(5.0)  # Long running
                return b"Y" * length
            except asyncio.CancelledError:
                fetch_cancelled = True
                raise

        async def consume():
            async for _ in mgr.stream(
                key="tg:cancel:1:doc:4096",
                offset=0,
                length=4096,
                total=4096,
                fetch=fetch,
            ):
                pass

        task = asyncio.create_task(consume())
        await fetch_started.wait()
        # Cancel consumer task
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        # Allow worker loop to process cancellation
        await asyncio.sleep(0.02)
        self.assertTrue(fetch_cancelled)
        self.assertEqual(len(mgr._jobs), 0)

        await mgr.close()

    async def test_backpressure_memory_pause(self):
        """Verify backpressure pauses upstream fetching when max_cache_bytes is reached."""
        # Exact budget for 2 blocks
        mgr = ParallelTelegramStreamManager(
            workers=2,
            global_workers=2,
            block_size=1024,
            read_ahead_bytes=4096,
            max_cache_bytes=2048,
        )

        fetches = 0

        async def fetch(offset: int, length: int) -> bytes:
            nonlocal fetches
            fetches += 1
            await asyncio.sleep(0.005)
            return b"Z" * length

        stream = mgr.stream(
            key="tg:bp:1:doc:8192",
            offset=0,
            length=8192,
            total=8192,
            fetch=fetch,
        )
        it = stream.__aiter__()

        # Consume chunk 0
        c0 = await it.__anext__()
        self.assertEqual(len(c0), 1024)

        # Allow prefetch to hit 2048 byte limit
        await asyncio.sleep(0.05)
        # In-flight + cached must not exceed max_cache_bytes
        self.assertLessEqual(mgr._current_cache_bytes + mgr._reserved_inflight_bytes, 2048)

        # Assert fetch count stops growing while consumer is paused
        paused_fetches = fetches
        await asyncio.sleep(0.05)
        self.assertEqual(
            fetches,
            paused_fetches,
            "Fetch count must stop growing while consumer is paused under backpressure",
        )

        # Consume remaining
        remaining_chunks = []
        while True:
            try:
                remaining_chunks.append(await it.__anext__())
            except StopAsyncIteration:
                break

        self.assertGreater(fetches, paused_fetches, "Fetches must resume after consumer drains blocks")
        self.assertEqual(len(b"".join([c0] + remaining_chunks)), 8192)

        await mgr.close()

    async def test_router_account_concurrency_limit_and_backpressure(self):
        """Verify router honors max_leases_per_account, routes fairly, and waits safely on affinity."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f1, \
             tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f2:
            f1.write(b"data1")
            f2.write(b"data2")
            p1, p2 = f1.name, f2.name

        try:
            upsert_account("acc1", session_file=p1, enabled=True)
            upsert_account("acc2", session_file=p2, enabled=True)

            def make_mock_client(*args):
                c = AsyncMock()
                c.is_connected = lambda: True
                return c

            router = TelegramAccountRouter(max_leases_per_account=2)
            router._client_factory = make_mock_client

            # acc1 acquires 2 leases (reaching max capacity of 2)
            aid_a, client_a, release_a = await router.acquire_channel_lease("-100100", affinity_account="acc1")
            aid_b, client_b, release_b = await router.acquire_channel_lease("-100100", affinity_account="acc1")
            self.assertEqual(router._active_leases["acc1"], 2)
            self.assertEqual(router._active_leases.get("acc2", 0), 0)

            # select_eligible_account must divert general requests to acc2
            selected = await router.select_eligible_account("-100100")
            self.assertEqual(selected, "acc2")

            # Affinity validation must NOT reject an account at capacity
            valid_id = router._validate_affinity_account("acc1")
            self.assertEqual(valid_id, "acc1")

            # Affinity lease acquisition waits for capacity instead of failing mid-stream
            lease_task = asyncio.create_task(
                router.acquire_channel_lease("-100100", affinity_account="acc1", timeout=1.0)
            )
            await asyncio.sleep(0.02)
            self.assertFalse(lease_task.done())

            # Calling the real returned synchronous release callback unblocks the waiting affinity waiter
            release_a()
            self.assertEqual(router._active_leases["acc1"], 1)

            aid, client, release_c = await asyncio.wait_for(lease_task, timeout=0.5)
            self.assertEqual(aid, "acc1")
            self.assertEqual(router._active_leases["acc1"], 2)

            # Releasing remaining leases brings counter back to zero
            release_b()
            self.assertEqual(router._active_leases["acc1"], 1)
            release_c()
            self.assertEqual(router._active_leases["acc1"], 0)

            # Both accounts at capacity: general selection reports limit
            router._active_leases["acc1"] = 2
            router._active_leases["acc2"] = 2
            with self.assertRaises(RuntimeError) as ctx2:
                await router.select_eligible_account("-100100")
            self.assertIn("concurrency limit", str(ctx2.exception))

            # Affinity request times out if capacity never frees up
            with self.assertRaises(RuntimeError) as ctx_timeout:
                await router.acquire_channel_lease("-100100", affinity_account="acc1", timeout=0.05)
            self.assertIn("concurrency limit", str(ctx_timeout.exception))
        finally:
            os.remove(p1)
            os.remove(p2)

    async def test_close_during_inflight_fetch_preserves_non_negative_counters(self):
        """Regression test for Finding 9: close() while workers run must not leave negative counters."""
        mgr = ParallelTelegramStreamManager(
            workers=2,
            global_workers=2,
            block_size=1024,
            max_cache_bytes=32768,
        )

        fetch_started = asyncio.Event()

        async def slow_fetch(offset: int, length: int) -> bytes:
            fetch_started.set()
            await asyncio.sleep(1.0)
            return b"W" * length

        async def run_stream():
            async for _ in mgr.stream(
                key="tg:close_test:1:doc:4096",
                offset=0,
                length=4096,
                total=4096,
                fetch=slow_fetch,
                account_id="acc_close",
            ):
                pass

        task = asyncio.create_task(run_stream())
        await fetch_started.wait()

        # Close while fetch is running
        await mgr.close()

        with self.assertRaises(asyncio.CancelledError):
            await task

        # Ensure close intentionality and no lingering worker tasks
        self.assertTrue(mgr._closed)
        self.assertTrue(task.done())
        self.assertEqual(len(mgr._worker_tasks), 0)

        # Ensure all counters and maps are bounded and non-negative
        self.assertEqual(mgr._current_cache_bytes, 0)
        self.assertEqual(mgr._reserved_inflight_bytes, 0)
        self.assertEqual(len(mgr._running_jobs_by_key), 0)
        self.assertEqual(len(mgr._running_jobs_by_account), 0)
        self.assertEqual(mgr._active_workers, 0)
        self.assertEqual(len(mgr._jobs), 0)

    async def test_two_account_concurrent_inflight_isolation_and_completed_cache_reuse(self):
        """Verify concurrent in-flight requests remain account-isolated, but completed blocks reuse cache."""
        mgr = ParallelTelegramStreamManager(
            workers=4,
            global_workers=4,
            block_size=1024,
            read_ahead_bytes=0,
            max_cache_bytes=32768,
        )

        canonical_key = "tg:two_acc_test:1:doc:2048"
        upstream_fetch_calls: list[str] = []
        barrier = asyncio.Event()

        async def fetch_acc1(offset: int, length: int) -> bytes:
            upstream_fetch_calls.append("acc1")
            barrier.set()
            await asyncio.sleep(0.02)
            return b"A" * length

        async def fetch_acc2(offset: int, length: int) -> bytes:
            upstream_fetch_calls.append("acc2")
            await barrier.wait()
            return b"B" * length

        # Two different accounts request the same block 0 concurrently
        async def read_acc1():
            async for chunk in mgr.stream(
                key=canonical_key,
                offset=0,
                length=1024,
                total=2048,
                fetch=fetch_acc1,
                job_key="acc1",
                account_id="acc1",
            ):
                return chunk

        async def read_acc2():
            async for chunk in mgr.stream(
                key=canonical_key,
                offset=0,
                length=1024,
                total=2048,
                fetch=fetch_acc2,
                job_key="acc2",
                account_id="acc2",
            ):
                return chunk

        res1, res2 = await asyncio.gather(read_acc1(), read_acc2())
        self.assertEqual(len(res1), 1024)
        self.assertEqual(len(res2), 1024)
        # Because in-flight blocks are account-isolated, both accounts performed upstream fetches
        self.assertIn("acc1", upstream_fetch_calls)
        self.assertIn("acc2", upstream_fetch_calls)

        # Now block 0 is completed in the canonical RAM cache.
        # A subsequent reader from another account reads block 0:
        upstream_calls_before = len(upstream_fetch_calls)

        async def fail_fetch(offset: int, length: int) -> bytes:
            upstream_fetch_calls.append("unexpected")
            return b"Z" * length

        async def read_acc3():
            async for chunk in mgr.stream(
                key=canonical_key,
                offset=0,
                length=1024,
                total=2048,
                fetch=fail_fetch,
                job_key="acc3",
                account_id="acc3",
            ):
                return chunk

        res3 = await read_acc3()
        self.assertEqual(len(res3), 1024)
        # Completed block was reused from shared canonical cache; 0 new upstream fetches
        self.assertEqual(len(upstream_fetch_calls), upstream_calls_before)

        await mgr.close()

    async def test_on_block_fetched_signature_and_types(self):
        """Verify on_block_fetched callback hook receives expected arguments and types."""
        mgr = ParallelTelegramStreamManager(
            workers=2,
            global_workers=2,
            block_size=1024,
            read_ahead_bytes=0,
            max_cache_bytes=16384,
        )

        recorded_events: list[tuple] = []

        def custom_hook(key: str, block_index: int, size_bytes: int, duration_seconds: float) -> None:
            recorded_events.append((key, block_index, size_bytes, duration_seconds))

        mgr.on_block_fetched = custom_hook

        async def mock_fetch(offset: int, length: int) -> bytes:
            await asyncio.sleep(0.005)
            return b"M" * length

        async for _ in mgr.stream(
            key="tg:hook_test:1:doc:1024",
            offset=0,
            length=1024,
            total=1024,
            fetch=mock_fetch,
        ):
            pass

        self.assertEqual(len(recorded_events), 1)
        k, b_idx, size, duration = recorded_events[0]
        self.assertIsInstance(k, str)
        self.assertEqual(k, "tg:hook_test:1:doc:1024")
        self.assertIsInstance(b_idx, int)
        self.assertEqual(b_idx, 0)
        self.assertIsInstance(size, int)
        self.assertEqual(size, 1024)
        self.assertIsInstance(duration, float)
        self.assertGreater(duration, 0.0)

        await mgr.close()

    async def test_close_on_timeout_cancels_and_awaits_all_workers(self):
        """Verify that when close encounters a timeout waiting for workers, all workers are explicitly cancelled and awaited."""
        mgr = ParallelTelegramStreamManager(
            workers=2,
            global_workers=2,
            block_size=1024,
            max_cache_bytes=16384,
        )

        started = asyncio.Event()

        async def slow_fetch(offset: int, length: int) -> bytes:
            started.set()
            await asyncio.sleep(10.0)
            return b"S" * length

        task = asyncio.create_task(
            mgr._acquire_block(
                key="tg:timeout_test:1:doc:2048",
                job_key="acc_t",
                cur_blk=0,
                total=2048,
                fetch=slow_fetch,
                consumer_token=object(),
                registered_jobs={},
                last_block=0,
                read_ahead_blocks=0,
            )
        )
        await started.wait()
        self.assertGreater(len(mgr._worker_tasks), 0)

        # Force _wait_for_workers timeout behavior by monkeypatching asyncio.wait_for with zero timeout
        async def fast_timeout(fut, timeout):
            raise asyncio.TimeoutError()

        with patch("asyncio.wait_for", side_effect=fast_timeout):
            await mgr.close()

        # All worker tasks must be cancelled and done, with active worker count zero
        for t in mgr._worker_tasks:
            self.assertTrue(t.done())
        self.assertEqual(mgr._active_workers, 0)
        self.assertTrue(mgr._closed)

        with self.assertRaises((asyncio.CancelledError, RuntimeError)):
            await task

    def test_account_workers_constructor_validation_and_env_fallback(self):
        """Verify account_workers rejects nonpositive values in constructor and normalizes in env fallback."""
        with self.assertRaises(ValueError):
            ParallelTelegramStreamManager(account_workers=0)
        with self.assertRaises(ValueError):
            ParallelTelegramStreamManager(account_workers=-1)

        from resolver.providers.telegram_download import _get_stream_manager_config
        with patch.dict(os.environ, {"TELEGRAM_DOWNLOAD_ACCOUNT_WORKERS": "0"}):
            cfg = _get_stream_manager_config()
            self.assertIsNone(cfg["account_workers"])

        with patch.dict(os.environ, {"TELEGRAM_DOWNLOAD_ACCOUNT_WORKERS": "-5"}):
            cfg = _get_stream_manager_config()
            self.assertIsNone(cfg["account_workers"])

        with patch.dict(os.environ, {"TELEGRAM_DOWNLOAD_ACCOUNT_WORKERS": "invalid"}):
            cfg = _get_stream_manager_config()
            self.assertIsNone(cfg["account_workers"])

        with patch.dict(os.environ, {"TELEGRAM_DOWNLOAD_ACCOUNT_WORKERS": "4"}):
            cfg = _get_stream_manager_config()
            self.assertEqual(cfg["account_workers"], 4)

    def test_router_max_leases_validation_fallback(self):
        """Finding 10: router validates max_leases_per_account and rejects 0 or negative values."""
        with self.assertRaises(ValueError):
            TelegramAccountRouter(max_leases_per_account=0)
        with self.assertRaises(ValueError):
            TelegramAccountRouter(max_leases_per_account=-5)

        # Positive explicit value is honored
        r = TelegramAccountRouter(max_leases_per_account=5)
        self.assertEqual(r._max_leases_per_account, 5)


if __name__ == "__main__":
    unittest.main()
