"""Bounded parallel Telegram streaming manager.

Exports ParallelTelegramStreamManager providing bounded, fair, deduplicated,
and prefetching parallel block downloads with LRU caching and strict RAM bounds.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict
import logging
import time
from typing import (
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    Optional,
    Set,
    Tuple,
)

logger = logging.getLogger(__name__)


class _CacheEntry:
    __slots__ = ("data", "size", "expires_at", "pinned_count")

    def __init__(self, data: bytes, expires_at: float):
        self.data: bytes = data
        self.size: int = len(data)
        self.expires_at: float = expires_at
        self.pinned_count: int = 0


class _Job:
    __slots__ = (
        "key",
        "cache_key",
        "block_index",
        "offset",
        "length",
        "fetch_fn",
        "priority",
        "created_at",
        "future",
        "consumers",
        "demand_consumers",
        "fetch_task",
        "reserved_bytes",
        "started",
        "account_id",
    )

    def __init__(
        self,
        key: str,
        block_index: int,
        offset: int,
        length: int,
        fetch_fn: Callable[[int, int], Awaitable[bytes]],
        priority: int,
        cache_key: Optional[str] = None,
        account_id: Optional[str] = None,
    ):
        self.key: str = key
        self.cache_key: str = cache_key or key
        self.block_index: int = block_index
        self.offset: int = offset
        self.length: int = length
        self.fetch_fn = fetch_fn
        self.priority: int = priority  # 0: DEMAND, 1: PREFETCH
        self.created_at: float = time.monotonic()
        # Readiness signal only: never holds block bytes, so completed jobs
        # retained in registered_jobs cannot keep evicted blocks alive.
        self.future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.future.add_done_callback(self._consume_future_exception)
        self.consumers: Set[object] = set()
        self.demand_consumers: Set[object] = set()
        self.fetch_task: Optional[asyncio.Task] = None
        self.reserved_bytes: int = 0
        self.started: bool = False
        self.account_id: Optional[str] = account_id

    @staticmethod
    def _consume_future_exception(fut: asyncio.Future) -> None:
        if not fut.cancelled():
            fut.exception()


def _validate_stream_request(
    key: str, offset: int, length: Optional[int], total: int,
) -> None:
    if not key or not isinstance(key, str):
        raise ValueError(f"Invalid stream key: {key!r}")
    if offset < 0:
        raise ValueError(f"offset must be non-negative, got {offset}")
    if length is not None and length < 0:
        raise ValueError(f"length must be non-negative, got {length}")
    if total < 0:
        raise ValueError(f"total must be non-negative, got {total}")


def _validate_fetched_block(job: _Job, data: object) -> bytes:
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"fetch must return bytes, got {type(data).__name__}")
    data = bytes(data)
    if len(data) != job.length:
        raise RuntimeError(
            f"Exact fetch length mismatch for {job.key} block {job.block_index} "
            f"(offset {job.offset}): expected {job.length} bytes, got {len(data)} bytes"
        )
    return data


class ParallelTelegramStreamManager:
    """Bounded parallel streaming manager with LRU cache, prefetch, and fairness."""

    def __init__(
        self,
        *,
        workers: int = 3,
        global_workers: int = 6,
        block_size: int = 524288,
        read_ahead_bytes: int = 33554432,
        max_cache_bytes: int = 134217728,
        cache_ttl_seconds: float = 120,
        account_workers: Optional[int] = None,
    ):
        if workers <= 0:
            raise ValueError(f"workers must be > 0, got {workers}")
        if global_workers <= 0:
            raise ValueError(f"global_workers must be > 0, got {global_workers}")
        if block_size <= 0:
            raise ValueError(f"block_size must be > 0, got {block_size}")
        if read_ahead_bytes < 0:
            raise ValueError(f"read_ahead_bytes must be >= 0, got {read_ahead_bytes}")
        if max_cache_bytes < block_size:
            raise ValueError(
                f"max_cache_bytes ({max_cache_bytes}) must be >= block_size ({block_size})"
            )
        if cache_ttl_seconds <= 0:
            raise ValueError(f"cache_ttl_seconds must be > 0, got {cache_ttl_seconds}")
        if account_workers is not None and account_workers <= 0:
            raise ValueError(f"account_workers must be > 0, got {account_workers}")

        self.workers: int = min(workers, global_workers)
        self.global_workers: int = global_workers
        self.account_workers: Optional[int] = (
            min(account_workers, global_workers) if account_workers is not None else None
        )
        self.block_size: int = block_size
        self.max_cache_bytes: int = max_cache_bytes
        # Cap read ahead to cache budget
        self.read_ahead_bytes: int = min(read_ahead_bytes, max_cache_bytes)
        self.cache_ttl_seconds: float = float(cache_ttl_seconds)

        self._cache: OrderedDict[Tuple[str, int], _CacheEntry] = OrderedDict()
        self._current_cache_bytes: int = 0
        self._reserved_inflight_bytes: int = 0

        self._lock: asyncio.Lock = asyncio.Lock()
        self._mem_cond: asyncio.Condition = asyncio.Condition(self._lock)

        self._jobs: Dict[Tuple[str, int], _Job] = {}
        self._running_jobs_by_key: Dict[str, int] = defaultdict(int)
        self._running_jobs_by_account: Dict[str, int] = defaultdict(int)
        self._active_workers: int = 0
        self._worker_tasks: Set[asyncio.Task] = set()
        self._closed: bool = False

    def _evict_unpinned_locked(self, needed_bytes: int) -> None:
        """Evict unpinned entries until we can accommodate needed_bytes."""
        now = time.monotonic()
        # 1. Clean expired unpinned entries
        for k in list(self._cache.keys()):
            entry = self._cache[k]
            if entry.pinned_count == 0 and entry.expires_at <= now:
                self._cache.pop(k)
                self._current_cache_bytes -= entry.size
                self.on_eviction(k[0], k[1], entry.size)

        # 2. Evict LRU unpinned entries until under budget
        for k in list(self._cache.keys()):
            if (
                self._current_cache_bytes
                + self._reserved_inflight_bytes
                + needed_bytes
                <= self.max_cache_bytes
            ):
                break
            entry = self._cache[k]
            if entry.pinned_count == 0:
                self._cache.pop(k)
                self._current_cache_bytes -= entry.size
                self.on_eviction(k[0], k[1], entry.size)

    def _maybe_start_workers(self) -> None:
        if self._closed:
            return
        while self._active_workers < self.global_workers:
            unstarted = [
                j for j in self._jobs.values()
                if not j.started and len(j.consumers) > 0
            ]
            if not unstarted:
                break
            can_run = any(
                self._running_jobs_by_key[j.key] < self.workers
                and (
                    self.account_workers is None
                    or not j.account_id
                    or self._running_jobs_by_account[j.account_id] < self.account_workers
                )
                for j in unstarted
            )
            if not can_run:
                break
            self._active_workers += 1
            task = asyncio.create_task(self._worker_loop())
            self._worker_tasks.add(task)
            task.add_done_callback(self._worker_tasks.discard)

    async def _worker_loop(self) -> None:
        try:
            while not self._closed:
                job_to_run: Optional[_Job] = None
                async with self._lock:
                    candidates = [
                        j for j in self._jobs.values()
                        if not j.started
                        and len(j.consumers) > 0
                        and self._running_jobs_by_key[j.key] < self.workers
                        and (
                            self.account_workers is None
                            or not j.account_id
                            or self._running_jobs_by_account[j.account_id] < self.account_workers
                        )
                    ]
                    if not candidates:
                        # No runnable unstarted jobs
                        break

                    # Demand (0) first, then fewer active jobs by account for fairness,
                    # then fewer active jobs by stream, then created_at
                    candidates.sort(
                        key=lambda j: (
                            j.priority,
                            self._running_jobs_by_account[j.account_id] if j.account_id else 0,
                            self._running_jobs_by_key[j.key],
                            j.created_at,
                        )
                    )

                    for cand in candidates:
                        needed = cand.length
                        self._evict_unpinned_locked(needed)
                        if (
                            self._current_cache_bytes
                            + self._reserved_inflight_bytes
                            + needed
                            <= self.max_cache_bytes
                        ):
                            self._reserved_inflight_bytes += needed
                            cand.reserved_bytes = needed
                            cand.started = True
                            self._running_jobs_by_key[cand.key] += 1
                            if cand.account_id:
                                self._running_jobs_by_account[cand.account_id] += 1
                            job_to_run = cand
                            break

                    if job_to_run is None:
                        # Memory constrained, wait for release
                        try:
                            await asyncio.wait_for(self._mem_cond.wait(), timeout=0.1)
                        except asyncio.TimeoutError:
                            pass
                        continue

                await self._execute_job(job_to_run)
        finally:
            async with self._lock:
                self._active_workers -= 1
                if not self._closed:
                    self._maybe_start_workers()

    async def _execute_job(self, job: _Job) -> None:
        fetch_task = asyncio.create_task(job.fetch_fn(job.offset, job.length))
        job.fetch_task = fetch_task
        data: Optional[bytes] = None
        exc: Optional[BaseException] = None
        start_time = time.monotonic()
        try:
            data = await fetch_task
            data = _validate_fetched_block(job, data)
        except asyncio.CancelledError:
            exc = asyncio.CancelledError()
        except BaseException as e:
            exc = e
        finally:
            job.fetch_task = None
            async with self._lock:
                self._release_job_locked(job)
                if exc is None and data is not None:
                    self._store_fetched_block_locked(job, data, start_time)
                else:
                    self._fail_job_locked(job, exc)
                self._mem_cond.notify_all()
                self._maybe_start_workers()

    def _release_job_locked(self, job: _Job) -> None:
        if job.key in self._running_jobs_by_key:
            self._running_jobs_by_key[job.key] -= 1
            if self._running_jobs_by_key[job.key] <= 0:
                self._running_jobs_by_key.pop(job.key, None)

        if job.account_id and job.account_id in self._running_jobs_by_account:
            self._running_jobs_by_account[job.account_id] -= 1
            if self._running_jobs_by_account[job.account_id] <= 0:
                self._running_jobs_by_account.pop(job.account_id, None)

        if job.reserved_bytes > 0:
            self._reserved_inflight_bytes = max(0, self._reserved_inflight_bytes - job.reserved_bytes)
            job.reserved_bytes = 0

        self._jobs.pop((job.key, job.block_index), None)

    def _store_fetched_block_locked(self, job: _Job, data: bytes, start_time: float) -> None:
        cache_key = (job.cache_key, job.block_index)
        now = time.monotonic()
        expires_at = now + self.cache_ttl_seconds
        existing = self._cache.get(cache_key)

        if existing is not None:
            # Account-specific job completed a block already verified and cached.
            # Preserve existing canonical bytes, add demand pins for awaiting readers,
            # and refresh TTL without double-counting RAM budget.
            existing.pinned_count += len(job.demand_consumers)
            existing.expires_at = max(existing.expires_at, expires_at)
            self._cache.move_to_end(cache_key)
        else:
            actual_size = len(data)
            entry = _CacheEntry(data=data, expires_at=expires_at)
            # Pinned for all awaiting demand readers so other workers cannot evict
            entry.pinned_count = len(job.demand_consumers)
            self._cache[cache_key] = entry
            self._current_cache_bytes += actual_size
            self.on_block_fetched(
                job.cache_key,
                job.block_index,
                actual_size,
                now - start_time,
            )

        if not job.future.done():
            # Signal only; the bytes live exclusively in the cache
            # entry (pinned for demand readers above).
            job.future.set_result(None)

    def _fail_job_locked(self, job: _Job, exc: Optional[BaseException]) -> None:
        if not job.future.done():
            if isinstance(exc, asyncio.CancelledError):
                job.future.cancel()
            else:
                job.future.set_exception(
                    exc or RuntimeError("Block fetch failed")
                )

    async def stream(
        self,
        key: str,
        offset: int,
        length: Optional[int],
        total: int,
        fetch: Callable[[int, int], Awaitable[bytes]],
        job_key: Optional[str] = None,
        account_id: Optional[str] = None,
    ) -> AsyncIterator[bytes]:
        """Stream byte range with bounded parallel fetching and prefetching."""
        if self._closed:
            raise RuntimeError("ParallelTelegramStreamManager is closed")
        _validate_stream_request(key, offset, length, total)
        if offset >= total or length == 0:
            return

        effective_account_id = account_id or job_key
        effective_job_key = f"{key}::{job_key}" if job_key else key

        start_offset = offset
        end_offset = total if length is None else min(total, offset + length)
        if start_offset >= end_offset:
            return

        B = self.block_size
        start_block = start_offset // B
        last_block = (end_offset - 1) // B
        read_ahead_blocks = self.read_ahead_bytes // B

        consumer_token = object()
        registered_jobs: Dict[Tuple[str, int], _Job] = {}
        current_pinned: Optional[_CacheEntry] = None

        try:
            for cur_blk in range(start_block, last_block + 1):
                if self._closed:
                    raise RuntimeError("ParallelTelegramStreamManager is closed")

                entry, job = await self._acquire_block(
                    key, effective_job_key, cur_blk, total, fetch, consumer_token, registered_jobs,
                    last_block, read_ahead_blocks, account_id=effective_account_id,
                )
                current_pinned = entry

                # Remove completed registered jobs and consumer tokens to avoid retaining entire movie
                async with self._lock:
                    if job is not None:
                        job.consumers.discard(consumer_token)
                        job.demand_consumers.discard(consumer_token)
                    registered_jobs.pop((effective_job_key, cur_blk), None)

                # Slice chunk
                blk_start = cur_blk * B
                s_start = max(0, start_offset - blk_start)
                s_end = min(len(entry.data), end_offset - blk_start)
                chunk = entry.data[s_start:s_end]

                try:
                    if chunk:
                        yield chunk
                finally:
                    # On resume from yield release previous pin BEFORE acquiring next block
                    if current_pinned is not None:
                        await self._unpin(current_pinned)
                        current_pinned = None

        finally:
            if current_pinned is not None:
                await self._unpin(current_pinned)
                current_pinned = None

            await self._release_registered_jobs(registered_jobs, consumer_token)

    async def _unpin(self, entry: _CacheEntry) -> None:
        async with self._lock:
            entry.pinned_count -= 1
            self._mem_cond.notify_all()

    async def _acquire_block(
        self,
        key: str,
        job_key: str,
        cur_blk: int,
        total: int,
        fetch: Callable[[int, int], Awaitable[bytes]],
        consumer_token: object,
        registered_jobs: Dict[Tuple[str, int], _Job],
        last_block: int,
        read_ahead_blocks: int,
        account_id: Optional[str] = None,
    ) -> Tuple[_CacheEntry, Optional[_Job]]:
        """Pin the cache entry for one block, waiting for its fetch if needed."""
        cache_key = (key, cur_blk)
        entry: Optional[_CacheEntry] = None
        job: Optional[_Job] = None

        # Retry if a prefetch entry was evicted before we pinned it:
        # re-acquiring attaches demand (or creates a fresh demand job
        # pinned at completion), so the second pass is guaranteed.
        while entry is None:
            async with self._lock:
                now = time.monotonic()
                # 1. Early demand block scheduling MUST precede prefetch start
                entry = self._attach_cached_locked(key, cache_key, cur_blk, now)
                if entry is None:
                    job = self._attach_demand_locked(
                        key, job_key, cur_blk, total, fetch, consumer_token, registered_jobs,
                        account_id=account_id,
                    )

                # 2. Prefetch blocks ahead within window
                self._schedule_prefetch_locked(
                    key, job_key, cur_blk, last_block, read_ahead_blocks, total, fetch,
                    consumer_token, registered_jobs, now, account_id=account_id,
                )

                self._maybe_start_workers()

            # Await job completion if not cached. The future is a
            # readiness signal only; bytes are read from the pinned
            # cache entry (demand completions pin for their readers,
            # so the entry cannot be evicted under us).
            if entry is None:
                if job is None:
                    raise RuntimeError(f"Failed to acquire block {cur_blk} for key {key}")
                await asyncio.shield(job.future)

                async with self._lock:
                    entry = self._cache.get(cache_key)
                    if entry is not None:
                        self._cache.move_to_end(cache_key)
                    # else: evicted before pinning; loop re-acquires.

        return entry, job

    def _attach_cached_locked(
        self, key: str, cache_key: Tuple[str, int], cur_blk: int, now: float,
    ) -> Optional[_CacheEntry]:
        """Pin a live cache entry for the reader. Returns None on a miss."""
        e = self._cache.get(cache_key)
        if e is not None and e.expires_at > now:
            self._cache.move_to_end(cache_key)
            e.pinned_count += 1
            self.on_cache_hit(key, cur_blk)
            return e
        return None

    def _attach_demand_locked(
        self,
        key: str,
        job_key: str,
        cur_blk: int,
        total: int,
        fetch: Callable[[int, int], Awaitable[bytes]],
        consumer_token: object,
        registered_jobs: Dict[Tuple[str, int], _Job],
        account_id: Optional[str] = None,
    ) -> _Job:
        """Attach the reader to an existing job or create a demand job for the block."""
        job_map_key = (job_key, cur_blk)
        self.on_cache_miss(key, cur_blk)
        blk_off = cur_blk * self.block_size
        blk_len = min(self.block_size, total - blk_off)
        job = self._jobs.get(job_map_key)
        if job is not None:
            job.consumers.add(consumer_token)
            job.demand_consumers.add(consumer_token)
            registered_jobs[job_map_key] = job
            if job.priority > 0:
                job.priority = 0  # Promote to demand
        else:
            job = _Job(
                job_key, cur_blk, blk_off, blk_len, fetch, priority=0,
                cache_key=key, account_id=account_id,
            )
            job.consumers.add(consumer_token)
            job.demand_consumers.add(consumer_token)
            self._jobs[job_map_key] = job
            registered_jobs[job_map_key] = job
        return job

    def _schedule_prefetch_locked(
        self,
        key: str,
        job_key: str,
        cur_blk: int,
        last_block: int,
        read_ahead_blocks: int,
        total: int,
        fetch: Callable[[int, int], Awaitable[bytes]],
        consumer_token: object,
        registered_jobs: Dict[Tuple[str, int], _Job],
        now: float,
        account_id: Optional[str] = None,
    ) -> None:
        prefetch_end = min(last_block + 1, cur_blk + 1 + read_ahead_blocks)
        for ahead_blk in range(cur_blk + 1, prefetch_end):
            self._schedule_prefetch_block_locked(
                key, job_key, ahead_blk, total, fetch, consumer_token, registered_jobs, now,
                account_id=account_id,
            )

    def _schedule_prefetch_block_locked(
        self,
        key: str,
        job_key: str,
        ahead_blk: int,
        total: int,
        fetch: Callable[[int, int], Awaitable[bytes]],
        consumer_token: object,
        registered_jobs: Dict[Tuple[str, int], _Job],
        now: float,
        account_id: Optional[str] = None,
    ) -> None:
        ahead_key = (key, ahead_blk)
        cached_entry = self._cache.get(ahead_key)
        if cached_entry is not None and cached_entry.expires_at > now:
            return

        blk_off = ahead_blk * self.block_size
        blk_len = min(self.block_size, total - blk_off)
        if blk_len <= 0:
            return

        ahead_job_key = (job_key, ahead_blk)
        existing_job = self._jobs.get(ahead_job_key)
        if existing_job is not None:
            existing_job.consumers.add(consumer_token)
            registered_jobs[ahead_job_key] = existing_job
        else:
            new_job = _Job(
                job_key, ahead_blk, blk_off, blk_len, fetch, priority=1,
                cache_key=key, account_id=account_id,
            )
            new_job.consumers.add(consumer_token)
            self._jobs[ahead_job_key] = new_job
            registered_jobs[ahead_job_key] = new_job
            self.on_prefetch_scheduled(key, ahead_blk)

    async def _release_registered_jobs(
        self,
        registered_jobs: Dict[Tuple[str, int], _Job],
        consumer_token: object,
    ) -> None:
        async with self._lock:
            for j in list(registered_jobs.values()):
                self._release_consumer_locked(j, consumer_token)
            registered_jobs.clear()
            self._mem_cond.notify_all()
            self._maybe_start_workers()

    def _release_consumer_locked(self, j: _Job, consumer_token: object) -> None:
        was_demand = consumer_token in j.demand_consumers
        j.consumers.discard(consumer_token)
        j.demand_consumers.discard(consumer_token)
        if was_demand and j.future.done() and not j.future.cancelled() and j.future.exception() is None:
            cached = self._cache.get((j.cache_key, j.block_index))
            if cached is not None and cached.pinned_count > 0:
                cached.pinned_count -= 1

        if len(j.consumers) == 0:
            if not j.started:
                self._jobs.pop((j.key, j.block_index), None)
                if not j.future.done():
                    j.future.cancel()
            elif not j.future.done() and j.fetch_task and not j.fetch_task.done():
                j.fetch_task.cancel()

    @staticmethod
    async def _wait_for_workers(tasks_to_wait: list[asyncio.Task]) -> None:
        if not tasks_to_wait:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks_to_wait, return_exceptions=True),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            for t in tasks_to_wait:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)

    async def close(self) -> None:
        """Close manager, cancelling in-flight work and clearing cache."""
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            for t in list(self._worker_tasks):
                t.cancel()
            for job in list(self._jobs.values()):
                if job.fetch_task and not job.fetch_task.done():
                    job.fetch_task.cancel()
                if not job.future.done():
                    job.future.cancel()
            self._mem_cond.notify_all()
            tasks_to_wait = list(self._worker_tasks)

        await self._wait_for_workers(tasks_to_wait)

        async with self._lock:
            for job in list(self._jobs.values()):
                if not job.future.done():
                    job.future.cancel()
            self._jobs.clear()
            self._cache.clear()
            self._current_cache_bytes = 0
            self._reserved_inflight_bytes = 0
            self._running_jobs_by_key.clear()
            self._running_jobs_by_account.clear()
            self._active_workers = 0
            self._worker_tasks.clear()
            self._mem_cond.notify_all()

    # Clear extension points for metrics
    def on_cache_hit(self, key: str, block_index: int) -> None:
        """Extension point for cache hits."""

    def on_cache_miss(self, key: str, block_index: int) -> None:
        """Extension point for cache misses."""

    def on_block_fetched(
        self, key: str, block_index: int, size: int, duration: float
    ) -> None:
        """Extension point when a block is fetched from upstream."""

    def on_prefetch_scheduled(self, key: str, block_index: int) -> None:
        """Extension point when prefetch is scheduled for a block."""

    def on_eviction(self, key: str, block_index: int, size: int) -> None:
        """Extension point when a block is evicted from cache."""
