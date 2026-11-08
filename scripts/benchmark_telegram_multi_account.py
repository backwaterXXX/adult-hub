#!/usr/bin/env python3
"""Synthetic benchmark and validation harness for Telegram multi-account routing & streaming.

Simulates concurrent streaming and metadata workloads across multiple accounts
to measure:
- TTFF (Time to First Frame / Chunk) and initial buffer latency
- Stream throughput (MB/s) and transferred bytes
- Global / per-account concurrency, affinity, and fairness
- Error count and FloodWait cooldown adherence
- Same-video throughput vs independent concurrent streams (shared completed-block cache reuse)
- Memory RSS and CPU time where practical

AUTHENTICATION NOTICE:
No authenticated Admin browser or session is available in this computer environment.
No real Telegram network requests were made and no real-account comparisons were run.
All measurements are synthetic execution metrics from mocked MTProto clients under
simulated network delays.

Usage:
    python3 scripts/benchmark_telegram_multi_account.py [--accounts N] [--streams M] [--chunks C] [--simulate-floodwait] [--scenario {all,independent,shared}]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure repo root is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from resolver.providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    _MEMORY_CHANNEL_ACCESS,
    set_memory_only_mode,
    upsert_account,
)
from resolver.providers.telegram_router import TelegramAccountRouter
from resolver.telegram_stream import ParallelTelegramStreamManager


def get_peak_rss_mb() -> Optional[float]:
    """Return peak RSS in MB portably across Linux, macOS, and Windows."""
    try:
        import resource
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        # On Darwin/macOS ru_maxrss is in bytes; on Linux it is in KiB.
        if sys.platform == "darwin":
            return round(rusage.ru_maxrss / (1024.0 * 1024.0), 2)
        elif sys.platform.startswith("linux"):
            return round(rusage.ru_maxrss / 1024.0, 2)
        else:
            return round(rusage.ru_maxrss / 1024.0, 2)
    except (ImportError, AttributeError, ValueError, OSError):
        return None


class MockMTProtoClient:
    """Mock client simulating MTProto byte downloading with controllable delays."""

    def __init__(self, account_id: str, chunk_latency_ms: float = 15.0):
        self.account_id = account_id
        self.chunk_latency_ms = chunk_latency_ms
        self.downloaded_chunks = 0
        self.downloaded_bytes = 0

    def is_connected(self) -> bool:
        return True

    async def is_user_authorized(self) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def iter_download(self, *args, offset: int = 0, request_size: int = 1048576, **kwargs):
        """Simulate chunk delivery with offset-dependent deterministic bytes."""
        await asyncio.sleep(self.chunk_latency_ms / 1000.0)
        self.downloaded_chunks += 1
        self.downloaded_bytes += request_size
        yield bytes(((offset + i) % 256) for i in range(request_size))


def _setup_synthetic_accounts(num_accounts: int) -> Tuple[List[str], Dict[str, MockMTProtoClient]]:
    temp_files: List[str] = []
    clients: Dict[str, MockMTProtoClient] = {}
    for i in range(1, num_accounts + 1):
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            f.write(f"synthetic-session-{i}".encode("utf-8"))
            temp_files.append(f.name)
        a_id = f"sim_acc_{i}"
        upsert_account(a_id, label=f"Simulated Account {i}", session_file=f.name, enabled=True)
        clients[a_id] = MockMTProtoClient(a_id)
    return temp_files, clients


async def _verify_cooling_and_affinity(
    router: TelegramAccountRouter, flood_account: Optional[str]
) -> Tuple[int, bool]:
    if not flood_account:
        return 0, True

    cooling_leases_received = 0
    for _ in range(5):
        selected = await router.select_eligible_account("-1001000")
        if selected == flood_account:
            cooling_leases_received += 1

    affinity_refused = False
    try:
        await router.acquire_channel_lease("-1001000", affinity_account=flood_account)
    except RuntimeError:
        affinity_refused = True

    return cooling_leases_received, affinity_refused


def _count_leases_during_cooldown(
    lease_log: List[Tuple[float, int, str]],
    flood_account: Optional[str],
    flood_started_at: float,
    flood_seconds: int,
) -> int:
    if not flood_account:
        return 0
    cooldown_deadline = flood_started_at + flood_seconds
    count = 0
    for l_time, _s_idx, a_id in lease_log:
        if flood_started_at < l_time < cooldown_deadline and a_id == flood_account:
            count += 1
    return count


def _verify_distribution_fairness(
    clients: Dict[str, MockMTProtoClient],
    simulate_floodwait: bool,
    flood_account: Optional[str],
) -> bool:
    if simulate_floodwait and flood_account:
        other_chunks = [c.downloaded_chunks for aid, c in clients.items() if aid != flood_account]
        return (max(other_chunks) - min(other_chunks) <= 5) if other_chunks else True
    chunk_counts = [c.downloaded_chunks for c in clients.values()]
    return (max(chunk_counts) - min(chunk_counts) <= 5) if chunk_counts else True


async def _download_stream_chunks(
    client: MockMTProtoClient,
    router: TelegramAccountRouter,
    initial_account: str,
    stream_idx: int,
    channel_id: str,
    chunks_per_stream: int,
    simulate_floodwait: bool,
    flood_ctx: Dict[str, Any],
    lease_log: List[Tuple[float, int, str]],
    stream_account_mapping: Dict[int, Set[str]],
    stream_start: float,
    ttff_latencies: List[float],
    initial_buffer_latencies: List[float],
) -> int:
    bytes_received = 0
    first_chunk_at: Optional[float] = None
    initial_buffer_at: Optional[float] = None

    for chunk_idx in range(chunks_per_stream):
        if simulate_floodwait and stream_idx == 1 and chunk_idx == 2:
            flood_ctx["account"] = initial_account
            flood_ctx["started_at"] = time.monotonic()
            flood_ctx["errors"] += 1

            class FloodWaitError(Exception):
                seconds = flood_ctx["seconds"]

            router.handle_request_exception(initial_account, channel_id, FloodWaitError())

        try:
            affinity_account, _, chunk_release = await router.acquire_channel_lease(
                channel_id, affinity_account=initial_account
            )
            lease_log.append((time.monotonic(), stream_idx, affinity_account))
            try:
                stream_account_mapping[stream_idx].add(affinity_account)
            finally:
                chunk_release()
        except RuntimeError as lease_err:
            if "FloodWait cooldown" in str(lease_err):
                break
            raise

        chunk_offset = chunk_idx * 1048576
        async for chunk in client.iter_download(offset=chunk_offset, request_size=1048576):
            chunk_len = len(chunk)
            expected_sample = bytes(((chunk_offset + i) % 256) for i in range(min(64, chunk_len)))
            if chunk[:min(64, chunk_len)] != expected_sample:
                raise RuntimeError(
                    f"Chunk byte validation mismatch at offset {chunk_offset}"
                )
            bytes_received += chunk_len
            router.record_bytes(initial_account, chunk_len)

            if first_chunk_at is None:
                first_chunk_at = time.monotonic()
                ttff = first_chunk_at - stream_start
                ttff_latencies.append(ttff)
                router.record_buffer_init_latency(initial_account, ttff * 1000.0)

            if bytes_received >= 1048576 and initial_buffer_at is None:
                initial_buffer_at = time.monotonic()
                initial_buffer_latencies.append(initial_buffer_at - stream_start)

    return bytes_received


async def run_independent_streams_benchmark(
    num_accounts: int = 3,
    num_streams: int = 12,
    chunks_per_stream: int = 5,
    simulate_floodwait: bool = False,
) -> Dict[str, Any]:
    """Benchmark Scenario 1: Independent concurrent streams across multiple accounts."""
    set_memory_only_mode(True)
    _MEMORY_ACCOUNTS.clear()
    _MEMORY_CHANNEL_ACCESS.clear()

    temp_files, clients = _setup_synthetic_accounts(num_accounts)

    cpu_start = time.process_time()
    start_time = time.monotonic()

    try:
        router = TelegramAccountRouter(clock=time.monotonic, max_leases_per_account=20)
        router._client_factory = lambda path, aid, ahash: clients[
            [acc.account_id for acc in _MEMORY_ACCOUNTS.values() if acc.session_file == path][0]
        ]

        stream_account_mapping: Dict[int, Set[str]] = {i: set() for i in range(num_streams)}
        stream_latencies: List[float] = []
        ttff_latencies: List[float] = []
        initial_buffer_latencies: List[float] = []
        lease_log: List[Tuple[float, int, str]] = []
        flood_ctx: Dict[str, Any] = {"account": None, "started_at": 0.0, "seconds": 5, "errors": 0}

        async def _simulate_single_stream(stream_idx: int, channel_id: str):
            stream_start = time.monotonic()
            initial_account, client, release = await router.acquire_channel_lease(channel_id)
            lease_log.append((time.monotonic(), stream_idx, initial_account))
            stream_account_mapping[stream_idx].add(initial_account)

            try:
                bytes_received = await _download_stream_chunks(
                    client=client,
                    router=router,
                    initial_account=initial_account,
                    stream_idx=stream_idx,
                    channel_id=channel_id,
                    chunks_per_stream=chunks_per_stream,
                    simulate_floodwait=simulate_floodwait,
                    flood_ctx=flood_ctx,
                    lease_log=lease_log,
                    stream_account_mapping=stream_account_mapping,
                    stream_start=stream_start,
                    ttff_latencies=ttff_latencies,
                    initial_buffer_latencies=initial_buffer_latencies,
                )
            finally:
                release()

            duration = time.monotonic() - stream_start
            stream_latencies.append(duration)
            return bytes_received

        tasks = [
            _simulate_single_stream(i, f"-100{1000 + (i % num_accounts)}")
            for i in range(num_streams)
        ]
        results = await asyncio.gather(tasks[0], *tasks[1:])

        total_elapsed = time.monotonic() - start_time
        cpu_elapsed = time.process_time() - cpu_start
        total_bytes = sum(results)
        total_mb = total_bytes / (1024 * 1024)
        throughput_mb_s = total_mb / total_elapsed if total_elapsed > 0 else 0.0

        affinity_violations = [
            idx for idx, accs in stream_account_mapping.items() if len(accs) > 1
        ]
        per_account_chunks = {aid: c.downloaded_chunks for aid, c in clients.items()}
        per_account_bytes = {aid: c.downloaded_bytes for aid, c in clients.items()}

        flood_account = flood_ctx["account"]
        cooling_leases_received, affinity_refused = await _verify_cooling_and_affinity(router, flood_account)
        leases_during_cooldown_to_flood_acc = _count_leases_during_cooldown(
            lease_log, flood_account, flood_ctx["started_at"], flood_ctx["seconds"]
        )
        distribution_fair = _verify_distribution_fairness(clients, simulate_floodwait, flood_account)

        peak_rss_mb = get_peak_rss_mb()

        return {
            "num_accounts": num_accounts,
            "num_streams": num_streams,
            "chunks_per_stream": chunks_per_stream,
            "simulate_floodwait": simulate_floodwait,
            "synthetic_elapsed_seconds": round(total_elapsed, 3),
            "cpu_process_seconds": round(cpu_elapsed, 4),
            "memory_peak_rss_mb": peak_rss_mb,
            "synthetic_transferred_mb": round(total_mb, 2),
            "synthetic_throughput_mb_s": round(throughput_mb_s, 2),
            "synthetic_avg_ttff_ms": round(
                (sum(ttff_latencies) / len(ttff_latencies) * 1000.0), 2
            ) if ttff_latencies else 0.0,
            "synthetic_min_ttff_ms": round(min(ttff_latencies) * 1000.0, 2) if ttff_latencies else 0.0,
            "synthetic_max_ttff_ms": round(max(ttff_latencies) * 1000.0, 2) if ttff_latencies else 0.0,
            "synthetic_avg_initial_buffer_ms": round(
                (sum(initial_buffer_latencies) / len(initial_buffer_latencies) * 1000.0), 2
            ) if initial_buffer_latencies else 0.0,
            "synthetic_avg_stream_latency_seconds": round(
                sum(stream_latencies) / len(stream_latencies), 3
            ) if stream_latencies else 0.0,
            "synthetic_affinity_violations_count": len(affinity_violations),
            "synthetic_errors_count": flood_ctx["errors"],
            "synthetic_per_account_chunks": per_account_chunks,
            "synthetic_per_account_bytes": per_account_bytes,
            "synthetic_distribution_fairness_verified": distribution_fair,
            "synthetic_floodwait_cooldown_honored": (
                router.is_account_cooling_down(flood_account) if flood_account else True
            ),
            "synthetic_cooling_leases_during_cooldown_count": leases_during_cooldown_to_flood_acc,
            "synthetic_cooling_accounts_received_no_leases": (
                cooling_leases_received == 0
                and affinity_refused
                and leases_during_cooldown_to_flood_acc == 0
            ),
            "router_metrics": router.get_all_metrics(),
        }
    finally:
        set_memory_only_mode(False)
        for p in temp_files:
            try:
                os.remove(p)
            except OSError:
                pass


async def run_shared_cache_benchmark(
    num_readers: int = 4,
    blocks: int = 8,
    block_size: int = 524288,
) -> Dict[str, Any]:
    """Benchmark Scenario 2: Same-video concurrent throughput with completed-block cache reuse."""
    total_file_size = blocks * block_size
    canonical_key = f"tg:-1001000:42:doc1:{total_file_size}"
    upstream_fetch_count = 0
    upstream_bytes_fetched = 0

    cache_cap = max(block_size * 2, (blocks // 2) * block_size)
    mgr = ParallelTelegramStreamManager(
        workers=3,
        global_workers=6,
        block_size=block_size,
        read_ahead_bytes=block_size * 2,
        max_cache_bytes=cache_cap,
        cache_ttl_seconds=60,
    )

    peak_cache_bytes = 0
    orig_on_block_fetched = mgr.on_block_fetched

    def track_block_fetched(k, b_idx, size, duration):
        nonlocal peak_cache_bytes
        peak_cache_bytes = max(peak_cache_bytes, mgr._current_cache_bytes)
        orig_on_block_fetched(k, b_idx, size, duration)

    mgr.on_block_fetched = track_block_fetched

    async def mock_fetch(offset: int, length: int) -> bytes:
        nonlocal upstream_fetch_count, upstream_bytes_fetched
        await asyncio.sleep(0.015)  # 15ms simulated MTProto chunk fetch
        upstream_fetch_count += 1
        upstream_bytes_fetched += length
        return bytes(((offset + i) % 256) for i in range(length))

    cpu_start = time.process_time()
    start_time = time.monotonic()
    reader_ttff: List[float] = []
    reader_bytes: List[int] = []

    async def _read_stream(reader_idx: int, account_id: str):
        t0 = time.monotonic()
        received = 0
        first_chunk = True
        if reader_idx > 0:
            await asyncio.sleep(0.005 * reader_idx)

        async for chunk in mgr.stream(
            key=canonical_key,
            offset=0,
            length=total_file_size,
            total=total_file_size,
            fetch=mock_fetch,
            job_key=account_id,
            account_id=account_id,
        ):
            if first_chunk:
                first_chunk = False
                reader_ttff.append(time.monotonic() - t0)
            expected = bytes(((received + i) % 256) for i in range(len(chunk)))
            if chunk != expected:
                raise RuntimeError(
                    f"Reader {reader_idx} received corrupted bytes at offset {received}"
                )
            received += len(chunk)
        reader_bytes.append(received)

    try:
        tasks = [
            _read_stream(i, f"sim_acc_{1 + (i % 2)}")
            for i in range(num_readers)
        ]
        await asyncio.gather(*tasks)

        total_elapsed = time.monotonic() - start_time
        cpu_elapsed = time.process_time() - cpu_start
        total_delivered_bytes = sum(reader_bytes)
        total_delivered_mb = total_delivered_bytes / (1024 * 1024)
        throughput_mb_s = total_delivered_mb / total_elapsed if total_elapsed > 0 else 0.0

        delivered_ratio = round(total_delivered_bytes / upstream_bytes_fetched, 2) if upstream_bytes_fetched > 0 else 1.0
        cache_bounded = (
            0 < peak_cache_bytes <= mgr.max_cache_bytes
            and mgr._current_cache_bytes <= mgr.max_cache_bytes
        )

        return {
            "num_readers": num_readers,
            "blocks_per_reader": blocks,
            "block_size_bytes": block_size,
            "total_file_size_bytes": total_file_size,
            "synthetic_elapsed_seconds": round(total_elapsed, 3),
            "cpu_process_seconds": round(cpu_elapsed, 4),
            "upstream_fetch_calls": upstream_fetch_count,
            "upstream_bytes_fetched": upstream_bytes_fetched,
            "total_delivered_bytes": total_delivered_bytes,
            "total_delivered_mb": round(total_delivered_mb, 2),
            "delivered_to_upstream_bytes_ratio": delivered_ratio,
            "shared_cache_dedup_ratio": delivered_ratio,
            "synthetic_throughput_mb_s": round(throughput_mb_s, 2),
            "first_reader_upstream_ttff_ms": round(reader_ttff[0] * 1000.0, 2) if reader_ttff else 0.0,
            "subsequent_reader_cache_ttff_avg_ms": round(
                (sum(reader_ttff[1:]) / len(reader_ttff[1:]) * 1000.0), 2
            ) if len(reader_ttff) > 1 else 0.0,
            "cache_peak_bytes": peak_cache_bytes,
            "cache_current_bytes": mgr._current_cache_bytes,
            "cache_max_bytes": mgr.max_cache_bytes,
            "cache_bounded": cache_bounded,
        }
    finally:
        await mgr.close()


async def run_benchmark(
    num_accounts: int = 3,
    num_streams: int = 12,
    chunks_per_stream: int = 5,
    simulate_floodwait: bool = False,
    scenario: str = "all",
) -> Dict[str, Any]:
    """Execute complete benchmark suite with authentic reporting."""
    combined_report: Dict[str, Any] = {
        "admin_auth_status": "Blocked: No authenticated Admin browser or session is available in this computer environment.",
        "real_account_comparisons_run": False,
        "live_performance_note": (
            "Synthetic benchmark timings only; live MTProto network performance unverified. "
            "No real Telegram network requests were made."
        ),
    }

    if scenario in ("all", "independent"):
        combined_report["independent_streams_scenario"] = await run_independent_streams_benchmark(
            num_accounts=num_accounts,
            num_streams=num_streams,
            chunks_per_stream=chunks_per_stream,
            simulate_floodwait=simulate_floodwait,
        )

    if scenario in ("all", "shared"):
        combined_report["same_video_shared_cache_scenario"] = await run_shared_cache_benchmark(
            num_readers=max(2, num_streams // 3),
            blocks=chunks_per_stream * 2,
            block_size=524288,
        )

    return combined_report


def main():
    parser = argparse.ArgumentParser(description="Telegram Multi-Account Routing & Streaming Synthetic Benchmark Harness")
    parser.add_argument("--accounts", type=int, default=3, help="Number of simulated accounts (default: 3)")
    parser.add_argument("--streams", type=int, default=12, help="Number of concurrent streams (default: 12)")
    parser.add_argument("--chunks", type=int, default=5, help="Number of 1MB chunks per stream (default: 5)")
    parser.add_argument("--simulate-floodwait", action="store_true", help="Simulate FloodWait on one account")
    parser.add_argument("--scenario", choices=["all", "independent", "shared"], default="all", help="Scenario to run")
    args = parser.parse_args()

    print("=================================================================")
    print("  Telegram Multi-Account Synthetic Benchmark Harness (Milestones D/E)")
    print("=================================================================")
    print(f"Scenario: {args.scenario} | Accounts: {args.accounts} | Streams: {args.streams} | Chunks: {args.chunks}")
    print(f"Simulate FloodWait: {args.simulate_floodwait}")
    print("AUTHENTICATION NOTICE: No authenticated Admin browser session present.")
    print("All results are synthetic simulation timings against mocked MTProto delays.")
    print("-----------------------------------------------------------------")

    report = asyncio.run(run_benchmark(
        num_accounts=args.accounts,
        num_streams=args.streams,
        chunks_per_stream=args.chunks,
        simulate_floodwait=args.simulate_floodwait,
        scenario=args.scenario,
    ))

    print(json.dumps(report, indent=2))
    print("-----------------------------------------------------------------")
    print("Synthetic Benchmark Summary:")

    indep = report.get("independent_streams_scenario")
    if indep:
        print("  [Scenario 1: Independent Concurrent Streams]")
        print(f"    Synthetic Elapsed:     {indep['synthetic_elapsed_seconds']}s (CPU: {indep['cpu_process_seconds']}s)")
        print(f"    Peak Memory RSS:       {indep['memory_peak_rss_mb']} MB")
        print(f"    Transferred:           {indep['synthetic_transferred_mb']} MB (Synthetic)")
        print(f"    Throughput:            {indep['synthetic_throughput_mb_s']} MB/s (Synthetic)")
        print(f"    Avg TTFF:              {indep['synthetic_avg_ttff_ms']} ms")
        print(f"    Avg Initial Buffer:    {indep['synthetic_avg_initial_buffer_ms']} ms")
        print(f"    Affinity Violations:   {indep['synthetic_affinity_violations_count']} (Expected: 0)")
        print(f"    Distribution Fair:     {indep['synthetic_distribution_fairness_verified']}")
        print(f"    Errors Recorded:       {indep['synthetic_errors_count']}")
        if args.simulate_floodwait:
            print(f"    FloodWait Honored:     {indep['synthetic_floodwait_cooldown_honored']}")
            print(f"    Cooling Excluded:      {indep['synthetic_cooling_accounts_received_no_leases']}")

    shared = report.get("same_video_shared_cache_scenario")
    if shared:
        print("  [Scenario 2: Same-Video Shared Cache Throughput]")
        print(f"    Readers:               {shared['num_readers']}")
        print(f"    Delivered to Readers:  {shared['total_delivered_mb']} MB")
        print(f"    Upstream Fetched:      {round(shared['upstream_bytes_fetched'] / (1024*1024), 2)} MB")
        print(f"    Delivered/Upstream Ratio: {shared['delivered_to_upstream_bytes_ratio']}x (completed-block cache reuse; note: concurrent in-flight blocks are account-isolated, not cross-account deduped)")
        print(f"    Upstream TTFF:         {shared['first_reader_upstream_ttff_ms']} ms")
        print(f"    Cache Hit TTFF Avg:    {shared['subsequent_reader_cache_ttff_avg_ms']} ms")
        print(f"    Cache Bound Enforced:  {shared['cache_bounded']} ({shared['cache_current_bytes']} <= {shared['cache_max_bytes']} bytes)")

    print("=================================================================")

    if indep and indep["synthetic_affinity_violations_count"] > 0:
        print("ERROR: Stream affinity violated! A stream switched accounts mid-transfer.")
        sys.exit(1)
    if indep and not indep["synthetic_distribution_fairness_verified"]:
        print("ERROR: Stream lease distribution was not fairly balanced.")
        sys.exit(1)
    if indep and args.simulate_floodwait and not indep["synthetic_cooling_accounts_received_no_leases"]:
        print("ERROR: Cooling accounts received new leases or affinity was not refused.")
        sys.exit(1)
    if shared and not shared["cache_bounded"]:
        print("ERROR: Cache exceeded memory bounds!")
        sys.exit(1)


if __name__ == "__main__":
    main()
