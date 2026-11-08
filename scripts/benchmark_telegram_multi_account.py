#!/usr/bin/env python3
"""Synthetic benchmark and validation harness for Telegram multi-account routing.

Simulates concurrent streaming and metadata workloads across multiple accounts
to measure request distribution, verify stream account affinity, and test
FloodWait cooldown adherence without requiring live Telegram network connections.

NOTE: All timing and throughput metrics are synthetic benchmark figures under
simulated delays, NOT live network measurements. Production MTProto throughput
remains unverified.

Usage:
    python3 scripts/benchmark_telegram_multi_account.py [--accounts N] [--streams M] [--chunks C] [--simulate-floodwait]
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
        """Simulate chunk delivery."""
        await asyncio.sleep(self.chunk_latency_ms / 1000.0)
        self.downloaded_chunks += 1
        self.downloaded_bytes += request_size
        yield b"\x00" * request_size


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


async def run_benchmark(
    num_accounts: int = 3,
    num_streams: int = 12,
    chunks_per_stream: int = 5,
    simulate_floodwait: bool = False,
) -> Dict[str, Any]:
    # Force an isolated memory store so sim_acc_* accounts are NEVER written to PostgreSQL
    set_memory_only_mode(True)
    _MEMORY_ACCOUNTS.clear()
    _MEMORY_CHANNEL_ACCESS.clear()

    temp_files, clients = _setup_synthetic_accounts(num_accounts)

    def sim_clock() -> float:
        return time.monotonic()

    try:
        router = TelegramAccountRouter(clock=sim_clock)
        router._client_factory = lambda path, aid, ahash: clients[
            [acc.account_id for acc in _MEMORY_ACCOUNTS.values() if acc.session_file == path][0]
        ]

        stream_account_mapping: Dict[int, Set[str]] = {i: set() for i in range(num_streams)}
        stream_latencies: List[float] = []
        lease_log: List[Tuple[float, int, str]] = []  # (timestamp, stream_idx, account_id)
        flood_account: Optional[str] = None
        flood_started_at: float = 0.0
        flood_seconds: int = 5

        start_time = time.monotonic()

        async def _simulate_single_stream(stream_idx: int, channel_id: str):
            nonlocal flood_account, flood_started_at
            stream_start = time.monotonic()
            bytes_received = 0

            # Acquire initial lease for the channel
            initial_account, client, release = await router.acquire_channel_lease(channel_id)
            lease_time = sim_clock()
            lease_log.append((lease_time, stream_idx, initial_account))
            stream_account_mapping[stream_idx].add(initial_account)

            try:
                for chunk_idx in range(chunks_per_stream):
                    # Simulate FloodWait injection if enabled
                    if simulate_floodwait and stream_idx == 1 and chunk_idx == 2:
                        flood_account = initial_account
                        flood_started_at = sim_clock()

                        class FloodWaitError(Exception):
                            seconds = flood_seconds

                        router.handle_request_exception(initial_account, channel_id, FloodWaitError())

                    # Verify stream affinity from actual router decisions on continuation
                    try:
                        affinity_account, _, chunk_release = await router.acquire_channel_lease(
                            channel_id, affinity_account=initial_account
                        )
                        chunk_lease_time = sim_clock()
                        lease_log.append((chunk_lease_time, stream_idx, affinity_account))
                        try:
                            stream_account_mapping[stream_idx].add(affinity_account)
                        finally:
                            chunk_release()
                    except RuntimeError as lease_err:
                        # Stream honors FloodWait on the pinned account and halts without crossing accounts
                        if "FloodWait cooldown" in str(lease_err):
                            break
                        raise

                    # Simulate downloading 1MB chunk with affinity
                    async for chunk in client.iter_download(offset=chunk_idx * 1048576, request_size=1048576):
                        bytes_received += len(chunk)
            finally:
                release()

            duration = time.monotonic() - stream_start
            stream_latencies.append(duration)
            return bytes_received

        # Launch concurrent streams
        tasks = [
            _simulate_single_stream(i, f"-100{1000 + (i % 3)}")
            for i in range(num_streams)
        ]
        results = await asyncio.gather(tasks[0], *tasks[1:])

        total_elapsed = time.monotonic() - start_time
        total_bytes = sum(results)
        total_mb = total_bytes / (1024 * 1024)
        throughput_mb_s = total_mb / total_elapsed if total_elapsed > 0 else 0.0

        # Check stream account affinity: each stream MUST only touch exactly 1 account
        affinity_violations = [
            idx for idx, accs in stream_account_mapping.items() if len(accs) > 1
        ]

        per_account_chunks = {aid: c.downloaded_chunks for aid, c in clients.items()}
        per_account_bytes = {aid: c.downloaded_bytes for aid, c in clients.items()}

        cooling_leases_received, affinity_refused = await _verify_cooling_and_affinity(router, flood_account)

        # Assert no lease to the FloodWait account DURING cooldown, not just after cooldown
        leases_during_cooldown_to_flood_acc = _count_leases_during_cooldown(
            lease_log, flood_account, flood_started_at, flood_seconds
        )

        # Check distribution fairness: even in FloodWait mode, other accounts must be fairly balanced
        distribution_fair = _verify_distribution_fairness(clients, simulate_floodwait, flood_account)

        report = {
            "num_accounts": num_accounts,
            "num_streams": num_streams,
            "chunks_per_stream": chunks_per_stream,
            "simulate_floodwait": simulate_floodwait,
            "synthetic_elapsed_seconds": round(total_elapsed, 3),
            "synthetic_transferred_mb": round(total_mb, 2),
            "synthetic_throughput_mb_s": round(throughput_mb_s, 2),
            "synthetic_avg_stream_latency_seconds": round(
                sum(stream_latencies) / len(stream_latencies), 3
            ) if stream_latencies else 0.0,
            "synthetic_affinity_violations_count": len(affinity_violations),
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
            "live_performance_note": "Synthetic benchmark timings only; live MTProto network performance unverified.",
        }
        return report
    finally:
        set_memory_only_mode(False)
        for p in temp_files:
            try:
                os.remove(p)
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description="Telegram Multi-Account Routing Synthetic Benchmark Harness")
    parser.add_argument("--accounts", type=int, default=3, help="Number of simulated accounts (default: 3)")
    parser.add_argument("--streams", type=int, default=12, help="Number of concurrent streams (default: 12)")
    parser.add_argument("--chunks", type=int, default=5, help="Number of 1MB chunks per stream (default: 5)")
    parser.add_argument("--simulate-floodwait", action="store_true", help="Simulate FloodWait on one account")
    args = parser.parse_args()

    print("=================================================================")
    print("  Telegram Multi-Account Synthetic Routing Benchmark Harness")
    print("=================================================================")
    print(f"Accounts: {args.accounts} | Concurrent Streams: {args.streams} | Chunks/Stream: {args.chunks}")
    print(f"Simulate FloodWait: {args.simulate_floodwait}")
    print("NOTE: All timing and throughput metrics are synthetic simulation figures.")
    print("-----------------------------------------------------------------")

    report = asyncio.run(run_benchmark(
        num_accounts=args.accounts,
        num_streams=args.streams,
        chunks_per_stream=args.chunks,
        simulate_floodwait=args.simulate_floodwait,
    ))

    print(json.dumps(report, indent=2))
    print("-----------------------------------------------------------------")
    print("Synthetic Results Summary:")
    print(f"  Synthetic Duration:   {report['synthetic_elapsed_seconds']}s")
    print(f"  Data Transferred:     {report['synthetic_transferred_mb']} MB (Synthetic)")
    print(f"  Synthetic Throughput: {report['synthetic_throughput_mb_s']} MB/s (Synthetic)")
    print(f"  Affinity Violations:  {report['synthetic_affinity_violations_count']} (Expected: 0)")
    print(f"  Distribution:         {report['synthetic_per_account_chunks']}")
    print(f"  Distribution Fair:    {report['synthetic_distribution_fairness_verified']}")
    if args.simulate_floodwait:
        print(f"  FloodWait Honored:    {report['synthetic_floodwait_cooldown_honored']}")
        print(f"  Cooling Excluded:     {report['synthetic_cooling_accounts_received_no_leases']}")
    print("=================================================================")

    if report["synthetic_affinity_violations_count"] > 0:
        print("ERROR: Stream affinity violated! A stream switched accounts mid-transfer.")
        sys.exit(1)
    if not report["synthetic_distribution_fairness_verified"]:
        print("ERROR: Stream lease distribution was not fairly balanced.")
        sys.exit(1)
    if args.simulate_floodwait and not report["synthetic_cooling_accounts_received_no_leases"]:
        print("ERROR: Cooling accounts received new leases or affinity was not refused.")
        sys.exit(1)


if __name__ == "__main__":
    main()
