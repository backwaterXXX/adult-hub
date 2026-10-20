"""Tests for bounded parallel Telegram streaming (v1).

Covers ``resolver/telegram_stream.py::ParallelTelegramStreamManager`` using a
fake async fetch callable only -- no Telegram/network access.

Contract under test (implemented in ``resolver/telegram_stream.py``):
    ParallelTelegramStreamManager(
        workers=3, global_workers=6, block_size=524288,
        read_ahead_bytes=33554432, max_cache_bytes=134217728,
        cache_ttl_seconds=120)
    async generator stream(key, offset, length, total, fetch)
        fetch: async callable(offset:int, length:int) -> bytes
    async close()

Also covers narrow resolver/app.py Telegram HEAD/range truthfulness:
suffix Range bytes=-N, 416 Content-Range bytes */total, HEAD parity
without consuming the download iterator.
"""

import asyncio
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path

# PostgreSQL-only: resolver cache/diagnostics read DATABASE_URL (no SQLite
# files). Point it at the test database BEFORE importing the resolver app
# (its cache/diagnostics singletons resolve the URL at import).
sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import TEST_URL

if TEST_URL:
    os.environ["DATABASE_URL"] = TEST_URL
else:
    os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault(
    "CONFIG_PATH",
    str(Path(__file__).parent / "fixtures" / "config_telegram_only.yaml"))

sys.path.insert(0, str(Path(__file__).parent.parent / "resolver"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from telegram_stream import ParallelTelegramStreamManager  # type: ignore
    STREAM_AVAILABLE = True
    _STREAM_IMPORT_ERROR = None
except Exception as exc:  # module may not exist yet in some checkouts
    ParallelTelegramStreamManager = None
    STREAM_AVAILABLE = False
    _STREAM_IMPORT_ERROR = exc

try:
    import app as resolver_app
    RESOLVER_AVAILABLE = True
    _RESOLVER_IMPORT_ERROR = None
except Exception as exc:
    resolver_app = None
    RESOLVER_AVAILABLE = False
    _RESOLVER_IMPORT_ERROR = exc


def _require_stream(testcase):
    if not STREAM_AVAILABLE:
        testcase.skipTest(
            f"telegram_stream not available yet: {_STREAM_IMPORT_ERROR}")


def _require_resolver(testcase):
    if not RESOLVER_AVAILABLE:
        testcase.skipTest(
            f"resolver app not available: {_RESOLVER_IMPORT_ERROR}")


def make_source(total_bytes, delay_per_block=None, failures=None):
    """Deterministic fake Telegram file: byte i == i % 256."""
    data = bytes(i % 256 for i in range(total_bytes))
    calls = []
    in_flight = {"n": 0, "max": 0}
    lock = None  # created lazily inside loop

    async def fetch(offset, length):
        nonlocal lock
        if lock is None:
            lock = asyncio.Lock()
        async with lock:
            in_flight["n"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["n"])
        try:
            calls.append((offset, length))
            if failures and (offset, length) in failures:
                raise IOError("injected fetch failure")
            delay = 0
            if delay_per_block:
                delay = delay_per_block(offset, length)
            if delay:
                await asyncio.sleep(delay)
            if offset >= len(data):
                return b""
            end = len(data) if length is None else min(len(data), offset + length)
            return data[offset:end]
        finally:
            async with lock:
                in_flight["n"] -= 1

    return data, fetch, calls, in_flight


async def collect(stream_agen):
    out = bytearray()
    async for chunk in stream_agen:
        out += chunk
    return bytes(out)


class TestStreamManagerContract(unittest.IsolatedAsyncioTestCase):
    async def test_constructor_defaults(self):
        _require_stream(self)
        sig = inspect.signature(ParallelTelegramStreamManager.__init__)
        params = sig.parameters
        expected = {
            "workers": 3,
            "global_workers": 6,
            "block_size": 524288,
            "read_ahead_bytes": 33554432,
            "max_cache_bytes": 134217728,
            "cache_ttl_seconds": 120,
        }
        for name, default in expected.items():
            self.assertIn(name, params, f"missing ctor kwarg {name}")
            self.assertEqual(params[name].default, default,
                             f"ctor default for {name}")
        mgr = ParallelTelegramStreamManager()
        await mgr.close()
        mgr2 = ParallelTelegramStreamManager(
            workers=2, global_workers=2, block_size=1024,
            read_ahead_bytes=4096, max_cache_bytes=1 << 20,
            cache_ttl_seconds=60)
        await mgr2.close()

    async def test_stream_method_signature(self):
        _require_stream(self)
        self.assertTrue(hasattr(ParallelTelegramStreamManager, "stream"))
        self.assertTrue(hasattr(ParallelTelegramStreamManager, "close"))
        sig = inspect.signature(ParallelTelegramStreamManager.stream)
        for name in ("key", "offset", "length", "total", "fetch"):
            self.assertIn(name, sig.parameters, f"stream() missing {name}")
        self.assertTrue(inspect.isasyncgenfunction(
            ParallelTelegramStreamManager.stream) or inspect.iscoroutinefunction(
            ParallelTelegramStreamManager.stream),
            "stream must be async generator function or async function "
            "returning an async iterator")


class TestParallelStreamBehavior(unittest.IsolatedAsyncioTestCase):
    async def _mgr(self, **kw):
        _require_stream(self)
        kw.setdefault("workers", 3)
        kw.setdefault("global_workers", 6)
        kw.setdefault("block_size", 65536)
        kw.setdefault("read_ahead_bytes", 256 * 1024)
        kw.setdefault("max_cache_bytes", 8 * 1024 * 1024)
        kw.setdefault("cache_ttl_seconds", 120)
        return ParallelTelegramStreamManager(**kw)

    async def test_exact_bytes_arbitrary_ranges(self):
        mgr = await self._mgr()
        try:
            total = 300_000
            data, fetch, _, _ = make_source(total)
            for offset, length in [(0, None), (0, 10), (1, 1),
                                   (65535, 2), (65536, 65536),
                                   (100, 200_000), (total - 100, 100),
                                   (total - 1, 1), (12345, 99999)]:
                expected = data[offset:offset + length] if length is not None else data[offset:]
                got = await collect(mgr.stream("k1", offset, length, total, fetch))
                self.assertEqual(got, expected,
                                 f"mismatch at offset={offset} length={length}")
        finally:
            await mgr.close()

    async def test_reordered_completion_still_ordered(self):
        mgr = await self._mgr(block_size=16384, read_ahead_bytes=128 * 1024)
        try:
            total = 128 * 1024
            data, _, _, _ = make_source(total)

            def delay(offset, length):
                # even blocks slow, odd blocks fast -> completions reorder
                return 0.05 if (offset // 16384) % 2 == 0 else 0.0

            _, fetch, _, _ = make_source(total, delay_per_block=delay)
            got = await collect(mgr.stream("reorder", 0, None, total, fetch))
            self.assertEqual(got, data)
        finally:
            await mgr.close()

    async def test_missing_next_block_wait(self):
        mgr = await self._mgr(block_size=8192, read_ahead_bytes=64 * 1024)
        try:
            total = 32 * 1024
            data, _, _, _ = make_source(total)
            gated = asyncio.Event()

            async def fetch(offset, length):
                if offset == 0:
                    await asyncio.wait_for(gated.wait(), timeout=5.0)
                await asyncio.sleep(0)
                end = min(len(data), offset + length)
                return data[offset:end]

            async def releaser():
                await asyncio.sleep(0.1)
                gated.set()

            task = asyncio.create_task(collect(mgr.stream("wait", 0, None, total, fetch)))
            rel = asyncio.create_task(releaser())
            got = await asyncio.wait_for(task, timeout=10.0)
            await rel
            self.assertEqual(got, data)
        finally:
            await mgr.close()

    async def test_truncated_source_fails_explicitly(self):
        # Declared total exceeding the fetchable bytes is a contract
        # violation: the manager requires exact block lengths, so the
        # stream must FAIL explicitly rather than emit a truncated body
        # that would disagree with the advertised Content-Length.
        mgr = await self._mgr(block_size=8192)
        try:
            total = 20_000  # declared total larger than real file
            real = 10_000
            _, fetch, _, _ = make_source(real)
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(
                    collect(mgr.stream("trunc", 0, None, total, fetch)),
                    timeout=10.0)
        finally:
            await mgr.close()

    async def test_eof_boundary_exact(self):
        # Ranges ending exactly at EOF stream fully; ranges starting at or
        # past EOF yield empty without hanging.
        mgr = await self._mgr(block_size=8192)
        try:
            total = 20_000
            data, fetch, _, _ = make_source(total)
            got = await asyncio.wait_for(
                collect(mgr.stream("eof", 0, None, total, fetch)), timeout=10.0)
            self.assertEqual(got, data)
            got2 = await asyncio.wait_for(
                collect(mgr.stream("eof", total, 100, total, fetch)),
                timeout=10.0)
            self.assertEqual(got2, b"")
            got3 = await asyncio.wait_for(
                collect(mgr.stream("eof", total + 100, 100, total + 1000, fetch)),
                timeout=10.0)
            self.assertEqual(got3, b"")
        finally:
            await mgr.close()

    async def test_overlapping_readers_deduplicate(self):
        mgr = await self._mgr(block_size=16384, read_ahead_bytes=64 * 1024)
        try:
            total = 128 * 1024
            data, fetch, calls, _ = make_source(total)
            a = asyncio.create_task(collect(mgr.stream("same", 0, None, total, fetch)))
            b = asyncio.create_task(collect(mgr.stream("same", 0, None, total, fetch)))
            ra, rb = await asyncio.wait_for(asyncio.gather(a, b), timeout=15.0)
            self.assertEqual(ra, data)
            self.assertEqual(rb, data)
            # naive would fetch every block twice; dedup must do strictly less
            naive = (total + 16384 - 1) // 16384 * 2
            self.assertLess(len(calls), naive,
                            f"no dedup: {len(calls)} fetches vs naive {naive}")
        finally:
            await mgr.close()

    async def test_cancel_one_sharing_block_preserves_another(self):
        # Both readers await the SAME shared block future; cancelling one
        # must not break the other (shared futures must be shielded and
        # cleanup must not cancel work a live consumer still needs).
        mgr = await self._mgr(workers=1, block_size=8192,
                              read_ahead_bytes=0)
        try:
            total = 8192  # single block: both readers share one future
            data, _, _, _ = make_source(total)
            gate = asyncio.Event()
            fetch_started = asyncio.Event()

            async def fetch(offset, length):
                fetch_started.set()
                await asyncio.wait_for(gate.wait(), timeout=10.0)
                end = min(len(data), offset + length)
                return data[offset:end]

            a = asyncio.create_task(
                collect(mgr.stream("shared", 0, None, total, fetch)))
            b = asyncio.create_task(
                collect(mgr.stream("shared", 0, None, total, fetch)))
            # Wait until the shared fetch is in flight, then give the
            # second reader a chance to attach to the same future.
            await asyncio.wait_for(fetch_started.wait(), timeout=10.0)
            await asyncio.sleep(0.1)
            a.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await a
            gate.set()
            rb = await asyncio.wait_for(b, timeout=10.0)
            self.assertEqual(rb, data)
        finally:
            await mgr.close()

    async def test_tight_budget_single_block_cache_no_deadlock(self):
        # max_cache_bytes == block_size: the previous block must be
        # released before the next block's memory is reserved, otherwise
        # the stream deadlocks holding a pinned block while waiting.
        for read_ahead in (0, 4096):
            mgr = await self._mgr(workers=1, block_size=4096,
                                  read_ahead_bytes=read_ahead,
                                  max_cache_bytes=4096)
            try:
                total = 4 * 4096
                data, fetch, _, _ = make_source(total)
                try:
                    got = await asyncio.wait_for(
                        collect(mgr.stream("tight", 0, None, total, fetch)),
                        timeout=15.0)
                except asyncio.TimeoutError:
                    self.fail("deadlock under max_cache_bytes == block_size "
                              f"(read_ahead={read_ahead})")
                self.assertEqual(got, data)
            finally:
                await mgr.close()

    async def test_close_cleans_pending_tasks(self):
        _require_stream(self)
        mgr = ParallelTelegramStreamManager(
            workers=2, global_workers=2, block_size=8192,
            read_ahead_bytes=32 * 1024, max_cache_bytes=1 << 20,
            cache_ttl_seconds=120)
        total = 256 * 1024
        _, fetch, _, _ = make_source(
            total, delay_per_block=lambda o, ln: 0.2)
        before = set(asyncio.all_tasks())
        readers = [asyncio.create_task(collect(
            mgr.stream(f"k{i}", 0, None, total, fetch))) for i in range(3)]
        await asyncio.sleep(0.05)
        await asyncio.wait_for(mgr.close(), timeout=10.0)
        for r in readers:
            if not r.done():
                r.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await asyncio.sleep(0)
        leaked = [t for t in asyncio.all_tasks()
                  if t not in before and not t.done()]
        self.assertEqual(leaked, [], f"leaked tasks after close: {leaked}")

    async def test_bounded_fetch_window_and_cache(self):
        mgr = await self._mgr(workers=2, block_size=8192,
                              read_ahead_bytes=2 * 8192)
        try:
            total = 256 * 1024
            data, fetch, calls, in_flight = make_source(total)
            got = await asyncio.wait_for(
                collect(mgr.stream("bounded", 0, None, total, fetch)), timeout=20.0)
            self.assertEqual(got, data)
            # window bound: concurrency must stay small (workers + slack)
            self.assertLessEqual(in_flight["max"], 4,
                                 f"fetch window unbounded: {in_flight['max']}")
            # cache bound: total fetched bytes must not wildly exceed file
            fetched = sum(ln for _, ln in calls if ln)
            self.assertLessEqual(fetched, total + 4 * 8192,
                                 f"over-fetch {fetched} for total {total}")
        finally:
            await mgr.close()

    async def test_global_worker_limit(self):
        mgr = await self._mgr(workers=4, global_workers=2, block_size=8192,
                              read_ahead_bytes=64 * 1024)
        try:
            total = 64 * 1024
            data, fetch, _, in_flight = make_source(
                total, delay_per_block=lambda o, ln: 0.03)
            tasks = [asyncio.create_task(collect(
                mgr.stream(f"g{i}", 0, None, total, fetch))) for i in range(4)]
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20.0)
            # Fairness: every key completes with exact bytes (no starvation
            # or cross-key corruption under the global cap).
            for got in results:
                self.assertEqual(got, data)
            self.assertLessEqual(in_flight["max"], 3,
                                 f"global limit exceeded: {in_flight['max']}")
        finally:
            await mgr.close()

    async def test_early_exit_releases_inflight(self):
        # Abandoning a stream must release jobs, memory reservations and
        # pins so the manager stays reusable and bounded.
        mgr = await self._mgr(workers=2, block_size=8192,
                              read_ahead_bytes=64 * 1024,
                              max_cache_bytes=256 * 1024)
        try:
            total = 256 * 1024
            data, fetch, _, _ = make_source(
                total, delay_per_block=lambda o, ln: 0.01)
            gen = mgr.stream("exit", 0, None, total, fetch)
            got = 0
            async for chunk in gen:
                got += len(chunk)
                if got >= 3 * 8192:
                    break
            await gen.aclose()
            await asyncio.sleep(0.05)
            self.assertEqual(dict(mgr._jobs), {})
            self.assertEqual(mgr._reserved_inflight_bytes, 0)
            # Manager remains usable for a fresh full stream afterwards.
            data2, fetch2, _, _ = make_source(total)
            rest = await asyncio.wait_for(
                collect(mgr.stream("exit2", 0, None, total, fetch2)),
                timeout=15.0)
            self.assertEqual(rest, data2)
        finally:
            await mgr.close()

    async def test_actual_memory_stays_within_bound(self):
        # Actual live payload bytes (cache + in-flight reservations +
        # completed-job futures, including blocks already LRU-evicted but
        # still retained by futures) must not exceed the configured budget.
        # The reader's own yielded output is a separate bounded working
        # buffer and is not counted here (this test retains no output).
        # No process-RSS bound is claimed.
        try:
            from telegram_stream import _Job  # internal; skip if refactored
        except ImportError:
            self.skipTest("telegram_stream internals refactored")
        import gc as _gc
        mgr = await self._mgr(workers=3, block_size=8192,
                              read_ahead_bytes=16 * 8192,
                              max_cache_bytes=4 * 8192)
        try:
            total = 60 * 8192
            data, _, _, _ = make_source(total)
            # Slow the reader so prefetch runs ahead and evictions happen.

            async def slow_fetch(o, ln):
                await asyncio.sleep(0.002)
                return data[o:o + ln]

            gen = mgr.stream("membound", 0, None, total, slow_fetch)
            got = 0
            async for chunk in gen:
                got += len(chunk)
                await asyncio.sleep(0.005)
                if got >= 20 * 8192:
                    break
            _gc.collect()
            cached_ids = {id(e.data) for e in mgr._cache.values()}
            fut_only = 0
            for obj in _gc.get_objects():
                if isinstance(obj, _Job):
                    f = obj.future
                    if f.done() and not f.cancelled():
                        try:
                            if f.exception() is None:
                                d = f.result()
                                if d is not None and id(d) not in cached_ids:
                                    fut_only += len(d)
                        except Exception:
                            pass
            actual = mgr._current_cache_bytes + mgr._reserved_inflight_bytes + fut_only
            self.assertLessEqual(
                actual, mgr.max_cache_bytes,
                f"actual live {actual} exceeds budget {mgr.max_cache_bytes} "
                f"(cache={mgr._current_cache_bytes} "
                f"reserved={mgr._reserved_inflight_bytes} "
                f"evicted-but-retained={fut_only})")
            await gen.aclose()
        finally:
            await mgr.close()

    async def test_memory_bound_several_slow_streams_fast_fetch(self):
        # Memory pressure: several slow readers share one manager while
        # fetch completes instantly; accounted + retained payload bytes must
        # stay within the configured budget throughout, and every reader
        # still receives exact bytes.
        try:
            from telegram_stream import _Job  # internal; skip if refactored
        except ImportError:
            self.skipTest("telegram_stream internals refactored")
        import gc as _gc
        mgr = await self._mgr(workers=3, global_workers=6, block_size=8192,
                              read_ahead_bytes=8 * 8192,
                              max_cache_bytes=8 * 8192)
        try:
            total = 40 * 8192
            data, _, _, _ = make_source(total)

            async def fast_fetch(o, ln):
                return data[o:o + ln]

            async def slow_reader(key, bound):
                got = 0
                async for chunk in mgr.stream(key, 0, None, total, fast_fetch):
                    got += len(chunk)
                    self.assertLessEqual(got, bound)
                    await asyncio.sleep(0.01)
                return got

            readers = [asyncio.create_task(slow_reader(f"slow{i}", total))
                       for i in range(3)]
            await asyncio.sleep(0.15)  # let pressure build mid-stream
            _gc.collect()
            cached_ids = {id(e.data) for e in mgr._cache.values()}
            fut_only = 0
            for obj in _gc.get_objects():
                if isinstance(obj, _Job):
                    f = obj.future
                    if f.done() and not f.cancelled():
                        try:
                            if f.exception() is None:
                                d = f.result()
                                if d is not None and id(d) not in cached_ids:
                                    fut_only += len(d)
                        except Exception:
                            pass
            actual = mgr._current_cache_bytes + mgr._reserved_inflight_bytes + fut_only
            self.assertLessEqual(
                actual, mgr.max_cache_bytes,
                f"actual live {actual} exceeds budget {mgr.max_cache_bytes} "
                f"(cache={mgr._current_cache_bytes} "
                f"reserved={mgr._reserved_inflight_bytes} "
                f"evicted-but-retained={fut_only})")
            results = await asyncio.wait_for(asyncio.gather(*readers), timeout=30.0)
            for got in results:
                self.assertEqual(got, total)
        finally:
            await mgr.close()

    async def test_demand_priority_no_starvation(self):
        mgr = await self._mgr(workers=2, block_size=8192,
                              read_ahead_bytes=64 * 1024)
        try:
            total = 128 * 1024
            data, fetch, _, _ = make_source(
                total, delay_per_block=lambda o, ln: 0.02)
            first = asyncio.create_task(
                collect(mgr.stream("seek", 0, None, total, fetch)))
            await asyncio.sleep(0.02)
            # a seek to the tail must still complete promptly (demand
            # priority over stale read-ahead), and the first reader too
            tail = await asyncio.wait_for(
                collect(mgr.stream("seek", total - 8192, 8192, total, fetch)),
                timeout=15.0)
            self.assertEqual(tail, data[total - 8192:])
            head = await asyncio.wait_for(first, timeout=15.0)
            self.assertEqual(head, data)
        finally:
            await mgr.close()


class TestResolverShutdown(unittest.IsolatedAsyncioTestCase):
    """App shutdown closes provider resources without creating a manager."""

    async def test_shutdown_awaits_provider_close(self):
        _require_resolver(self)
        from unittest.mock import patch
        closed = []

        class FakeProvider:
            async def close(self):
                closed.append(True)

        prov = FakeProvider()
        with patch.object(resolver_app, "get_provider", return_value=prov), \
             patch.object(resolver_app, "aclose_shared_client"), \
             patch.object(resolver_app.cache, "close"):
            # aclose_shared_client is auto-patched with AsyncMock.
            await resolver_app._on_shutdown()
        self.assertEqual(closed, [True])

    async def test_shutdown_does_not_create_manager(self):
        _require_resolver(self)
        from unittest.mock import patch
        accessed = []

        class FakeProvider:
            def __getattribute__(self, name):
                if name.startswith("__") or name == "close":
                    return object.__getattribute__(self, name)
                accessed.append(name)
                raise AttributeError(name)

            async def close(self):
                return None

        with patch.object(resolver_app, "get_provider",
                          return_value=FakeProvider()), \
             patch.object(resolver_app, "aclose_shared_client"), \
             patch.object(resolver_app.cache, "close"):
            await resolver_app._on_shutdown()
        # Only provider.close() may be touched: no stream_manager (or any
        # other attribute) access that could lazily create a manager.
        self.assertEqual(accessed, [])

    async def test_shutdown_tolerates_missing_or_failing_close(self):
        _require_resolver(self)
        from unittest.mock import patch

        class NoCloseProvider:
            pass

        class FailingProvider:
            async def close(self):
                raise RuntimeError("boom")

        for prov in (NoCloseProvider(), FailingProvider()):
            with patch.object(resolver_app, "get_provider",
                              return_value=prov), \
                 patch.object(resolver_app, "aclose_shared_client"), \
                 patch.object(resolver_app.cache, "close"):
                await resolver_app._on_shutdown()  # must never raise


class TestDeployedImportLayout(unittest.TestCase):
    """Deployment layout: resolver/ contents are the app root.

    Runs a subprocess with cwd=resolver/ and a PYTHONPATH that exposes
    only resolver/ (plus the shared common/ package, as installed in the
    images) so ``import resolver...`` cannot work. The provider must still
    build and close the parallel stream manager with no Telegram access.
    """

    def test_global_stream_manager_in_deployed_layout(self):
        import subprocess
        resolver_dir = str(Path(__file__).parent.parent / "resolver")
        repo_dir = str(Path(__file__).parent.parent)
        with tempfile.TemporaryDirectory(prefix="deploy_layout_") as tmp:
            # Simulate site-packages common without exposing the repo root
            # (which would also make the ``resolver`` package importable).
            os.symlink(os.path.join(repo_dir, "common"),
                       os.path.join(tmp, "common"))
            env = dict(os.environ)
            env["PYTHONPATH"] = resolver_dir + os.pathsep + tmp
            for var in ("CONFIG_PATH", "DATABASE_URL"):
                env.pop(var, None)
            script = (
                "import asyncio, sys\n"
                "assert not any('adult-hub' in p and 'resolver' not in p and '.venv' not in p "
                "for p in sys.path), sys.path\n"
                "try:\n"
                "    import resolver\n"
                "except ModuleNotFoundError:\n"
                "    pass\n"
                "else:\n"
                "    raise SystemExit('repo-root leak: resolver importable')\n"
                "from providers.telegram import _get_global_stream_manager\n"
                "m = _get_global_stream_manager()\n"
                "assert type(m).__name__ == 'ParallelTelegramStreamManager', type(m)\n"
                "asyncio.run(m.close())\n"
                "print('DEPLOYED_LAYOUT_OK')\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=resolver_dir, env=env, capture_output=True, text=True,
                timeout=30)
        self.assertEqual(
            proc.returncode, 0,
            f"deployed-layout import failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        self.assertIn("DEPLOYED_LAYOUT_OK", proc.stdout)


class FakeRefProvider:
    """Minimal telegram provider double for app.py HEAD/range tests."""

    _USE_PAYLOAD = object()

    def __init__(self, payload, size=_USE_PAYLOAD):
        self.payload = payload
        # size=None simulates unknown file size (file_size -> None).
        self.size = len(payload) if size is FakeRefProvider._USE_PAYLOAD else size
        self.download_calls = []

    async def file_size(self, ref):
        return self.size

    async def download_range(self, ref, offset, length):
        self.download_calls.append((ref, offset, length))

        async def chunks():
            end = len(self.payload) if length is None else offset + length
            yield self.payload[offset:end]

        return chunks()


def _fake_request(method="GET", range_header=None):
    class Req:
        pass
    r = Req()
    r.method = method
    r.headers = {}
    if range_header is not None:
        r.headers["range"] = range_header
    return r


async def response_body(resp):
    iterator = getattr(resp, "body_iterator", None)
    if iterator is None:
        body = getattr(resp, "body", b"")
        return bytes(body) if body else b""
    out = bytearray()
    async for chunk in iterator:
        if isinstance(chunk, (bytes, bytearray)):
            out += chunk
        elif isinstance(chunk, str):
            out += chunk.encode()
    return bytes(out)


class TestTelegramHeadAndRange(unittest.IsolatedAsyncioTestCase):
    """Focused app.py Telegram HEAD/range tests (new file only)."""

    async def test_unauthorized_channel_range_fails_before_streaming(self):
        _require_resolver(self)
        from fastapi import HTTPException
        from unittest.mock import patch

        class UnauthorizedProvider(FakeRefProvider):
            def is_channel_authorized(self, channel_ref):
                return False

            async def file_size(self, ref):
                raise AssertionError("must reject before metadata lookup")

            async def download_range(self, ref, offset, length):
                raise AssertionError("must not start a media stream")

        provider = UnauthorizedProvider(b"payload")
        with patch.object(resolver_app, "get_provider", return_value=provider):
            with self.assertRaises(HTTPException) as raised:
                await resolver_app._proxy_telegram_response(
                    _fake_request("GET", "bytes=0-65535"),
                    "tg://-1001234567890:42:999",
                )
        self.assertEqual(raised.exception.status_code, 403)

    async def test_head_matches_get_headers_without_consuming_iterator(self):
        _require_resolver(self)
        payload = bytes(i % 251 for i in range(100_000))
        for method, rng, want_status in [
            ("GET", None, 200),
            ("HEAD", None, 200),
            ("GET", "bytes=10-99", 206),
            ("HEAD", "bytes=10-99", 206),
        ]:
            prov = FakeRefProvider(payload)
            from unittest.mock import patch
            with patch.object(resolver_app, "get_provider", return_value=prov):
                resp = await resolver_app._proxy_telegram_response(
                    _fake_request(method, rng), "tg://ref")
            self.assertEqual(resp.status_code, want_status)
            if method == "HEAD":
                # must not create/consume the download iterator
                self.assertEqual(prov.download_calls, [],
                                 "HEAD must not call download_range")
                body = await response_body(resp)
                self.assertEqual(body, b"")
                # full HEAD advertises the known size truthfully
                if rng is None:
                    self.assertEqual(dict(resp.headers).get("content-length"),
                                     str(len(payload)))

    async def test_head_unknown_size_omits_content_length(self):
        _require_resolver(self)
        payload = bytes(i % 251 for i in range(10_000))
        prov = FakeRefProvider(payload, size=None)  # file_size -> None
        from unittest.mock import patch
        with patch.object(resolver_app, "get_provider", return_value=prov):
            resp = await resolver_app._proxy_telegram_response(
                _fake_request("HEAD", None), "tg://ref")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(prov.download_calls, [])
        # Unknown size: no Content-Length at all, never a synthetic 0.
        self.assertNotIn("content-length", dict(resp.headers))
        body = await response_body(resp)
        self.assertEqual(body, b"")

    async def test_head_range_parity_with_get(self):
        _require_resolver(self)
        payload = bytes((i * 7) % 256 for i in range(50_000))
        from unittest.mock import patch
        prov_get = FakeRefProvider(payload)
        with patch.object(resolver_app, "get_provider", return_value=prov_get):
            resp_get = await resolver_app._proxy_telegram_response(
                _fake_request("GET", "bytes=100-199"), "tg://ref")
        prov_head = FakeRefProvider(payload)
        with patch.object(resolver_app, "get_provider", return_value=prov_head):
            resp_head = await resolver_app._proxy_telegram_response(
                _fake_request("HEAD", "bytes=100-199"), "tg://ref")
        self.assertEqual(resp_head.status_code, resp_get.status_code)
        get_h = dict(resp_get.headers)
        head_h = dict(resp_head.headers)
        for key in ("content-range", "content-length", "accept-ranges"):
            self.assertEqual(head_h.get(key), get_h.get(key),
                             f"HEAD/GET header mismatch for {key}")

    async def test_suffix_range_serves_last_n_bytes(self):
        _require_resolver(self)
        payload = bytes(i % 256 for i in range(10_000))
        prov = FakeRefProvider(payload)
        from unittest.mock import patch
        with patch.object(resolver_app, "get_provider", return_value=prov):
            resp = await resolver_app._proxy_telegram_response(
                _fake_request("GET", "bytes=-500"), "tg://ref")
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(dict(resp.headers).get("content-range"),
                         f"bytes {len(payload) - 500}-{len(payload) - 1}/{len(payload)}")
        body = await response_body(resp)
        self.assertEqual(body, payload[-500:])
        self.assertEqual(prov.download_calls[0][1:], (len(payload) - 500, 500))

    async def test_suffix_range_head_parity(self):
        _require_resolver(self)
        payload = bytes(i % 256 for i in range(10_000))
        from unittest.mock import patch
        with patch.object(resolver_app, "get_provider",
                          return_value=FakeRefProvider(payload)):
            resp_get = await resolver_app._proxy_telegram_response(
                _fake_request("GET", "bytes=-100"), "tg://ref")
        prov_head = FakeRefProvider(payload)
        with patch.object(resolver_app, "get_provider", return_value=prov_head):
            resp_head = await resolver_app._proxy_telegram_response(
                _fake_request("HEAD", "bytes=-100"), "tg://ref")
        self.assertEqual(resp_head.status_code, 206)
        self.assertEqual(dict(resp_head.headers).get("content-range"),
                         dict(resp_get.headers).get("content-range"))
        self.assertEqual(prov_head.download_calls, [])

    async def test_unsatisfiable_range_416_with_content_range(self):
        _require_resolver(self)
        payload = bytes(i % 256 for i in range(1000))
        prov = FakeRefProvider(payload)
        from unittest.mock import patch
        from fastapi import HTTPException
        with patch.object(resolver_app, "get_provider", return_value=prov):
            with self.assertRaises(HTTPException) as ctx:
                await resolver_app._proxy_telegram_response(
                    _fake_request("GET", "bytes=5000-6000"), "tg://ref")
        self.assertEqual(ctx.exception.status_code, 416)
        headers = {k.lower(): v for k, v in
                   dict(getattr(ctx.exception, "headers", {}) or {}).items()}
        self.assertEqual(headers.get("content-range"), "bytes */1000")

    async def test_no_multipart_response(self):
        _require_resolver(self)
        payload = bytes(i % 256 for i in range(5000))
        prov = FakeRefProvider(payload)
        from unittest.mock import patch
        with patch.object(resolver_app, "get_provider", return_value=prov):
            resp = await resolver_app._proxy_telegram_response(
                _fake_request("GET", "bytes=0-99, 200-299"), "tg://ref")
        # multi-range is not supported: honest 200 full response, never
        # multipart/byteranges
        self.assertEqual(resp.status_code, 200)
        ctype = dict(resp.headers).get("content-type", "")
        self.assertNotIn("multipart", ctype.lower())


if __name__ == "__main__":
    unittest.main()
