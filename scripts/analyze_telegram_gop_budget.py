#!/usr/bin/env python3
"""Fetch only a Telegram MP4 moov index and estimate sample GOP packet bytes.

Example (inside a container with the resolver reachable):
    python scripts/analyze_telegram_gop_budget.py telegram:-100...:...

The default index-transfer cap is 16 MiB. This script never fetches media
packets and never computes or stores a pHash.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from urllib.parse import quote

import httpx

try:
    from sync.mp4_gop_budget import MP4IndexError, estimate_video_gops, union_bytes
except ModuleNotFoundError:
    from mp4_gop_budget import MP4IndexError, estimate_video_gops, union_bytes


class BoundedRangeReader:
    def __init__(self, url: str, max_bytes: int, client=httpx):
        self.url = url
        self.max_bytes = max_bytes
        self.bytes_fetched = 0
        self.total_size: int | None = None
        self.segments: list[tuple[int, bytes]] = []
        self.client = client

    def load_size(self) -> int:
        with self.client.stream("HEAD", self.url, timeout=20) as response:
            response.raise_for_status()
            raw = response.headers.get("content-length")
            if not raw or not raw.isdigit():
                raise ValueError("Telegram resolver did not return a content length")
            self.total_size = int(raw)
        return self.total_size

    def _fetch_gap(self, start: int, end: int) -> bytes:
        amount = end - start + 1
        if amount <= 0 or self.bytes_fetched + amount > self.max_bytes:
            raise ValueError(f"index byte cap ({self.max_bytes}) reached")
        with self.client.stream(
            "GET", self.url, headers={"Range": f"bytes={start}-{end}"}, timeout=30,
        ) as response:
            if response.status_code != 206:
                raise ValueError("Telegram resolver did not honor a bounded Range request")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)",
                                 response.headers.get("content-range", ""))
            if not match or tuple(map(int, match.groups())) != (start, end, self.total_size):
                raise ValueError("Telegram resolver returned an unexpected Content-Range")
            payload = response.read()
        if len(payload) != amount:
            raise ValueError("Telegram resolver returned an incomplete index range")
        self.bytes_fetched += len(payload)
        self.segments.append((start, payload))
        return payload

    def fetch(self, start: int, end: int) -> bytes:
        if self.total_size is None:
            self.load_size()
        assert self.total_size is not None
        if start < 0 or end < start or end >= self.total_size:
            raise ValueError("invalid MP4 index range")
        output = bytearray(end - start + 1)
        covered = bytearray(len(output))
        for segment_start, data in self.segments:
            segment_end = segment_start + len(data)
            left, right = max(start, segment_start), min(end + 1, segment_end)
            if left < right:
                out_left = left - start
                out_right = right - start
                output[out_left:out_right] = data[left - segment_start:right - segment_start]
                covered[out_left:out_right] = b"\x01" * (out_right - out_left)
        cursor = 0
        while cursor < len(output):
            if covered[cursor]:
                cursor += 1
                continue
            gap_start = cursor
            while cursor < len(output) and not covered[cursor]:
                cursor += 1
            gap_end = cursor
            gap_data = self._fetch_gap(start + gap_start, start + gap_end - 1)
            output[gap_start:gap_end] = gap_data
            covered[gap_start:gap_end] = b"\x01" * len(gap_data)
        return bytes(output)


def _atom(data: bytes, offset: int, total: int) -> tuple[int, bytes]:
    if offset + 8 > len(data):
        raise ValueError("top-level MP4 atom header is outside the probe")
    size = int.from_bytes(data[offset:offset + 4], "big")
    kind = data[offset + 4:offset + 8]
    header = 8
    if size == 1:
        if offset + 16 > len(data):
            raise ValueError("truncated extended MP4 atom header")
        size = int.from_bytes(data[offset + 8:offset + 16], "big")
        header = 16
    elif size == 0:
        size = total - offset
    if size < header or offset + size > total:
        raise ValueError("invalid top-level MP4 atom size")
    return size, kind


def _find_moov(reader: BoundedRangeReader, scan_chunk: int = 65536) -> tuple[int, int, bytes]:
    total = reader.total_size or reader.load_size()
    probe_len = min(scan_chunk, total)
    head = reader.fetch(0, probe_len - 1)
    pos = 0
    while pos + 8 <= len(head):
        size, kind = _atom(head, pos, total)
        if kind == b"moov":
            return pos, size, reader.fetch(pos, pos + size - 1)
        if kind == b"mdat":
            break
        pos += size
        if pos > len(head):
            break

    # Tail-moov files are common in Telegram uploads. Scan backwards using
    # small overlapping probes, then fetch the atom once through the cache.
    cursor = total
    overlap = 7
    while cursor > 0:
        start = max(0, cursor - scan_chunk)
        chunk = reader.fetch(start, cursor - 1)
        pos = chunk.rfind(b"moov")
        while pos >= 4:
            atom_start = start + pos - 4
            atom_size = int.from_bytes(chunk[pos - 4:pos], "big")
            if atom_size >= 8 and atom_start + atom_size <= total:
                raw = reader.fetch(atom_start, atom_start + atom_size - 1)
                if raw[4:8] == b"moov":
                    return atom_start, atom_size, raw
            pos = chunk.rfind(b"moov", 0, pos)
        if start == 0:
            break
        cursor = start + overlap
    raise ValueError("could not locate the MP4 moov atom within the index cap")


def _sample_times(duration: float) -> list[float]:
    offset = 0.05 * duration
    step_size = (0.90 * duration) / 25
    return [offset + index * step_size for index in range(25)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video_id", help="Telegram video ID, e.g. telegram:-100...:message:document")
    parser.add_argument("--duration", type=float, default=2432.0)
    parser.add_argument("--index-byte-cap", type=int, default=16 * 1024 * 1024)
    args = parser.parse_args()
    if not re.fullmatch(r"telegram:-?\d+:\d+:\d+", args.video_id):
        parser.error("video_id must be a Telegram composite ID")
    base = (os.getenv("RESOLVER_API_URL") or "http://resolver:8090").rstrip("/")
    if not base.endswith("/admin"):
        base += "/admin"
    url = f"{base}/play/{quote(args.video_id, safe=':-_')}/stream?mode=proxy"
    reader = BoundedRangeReader(url, max(1, args.index_byte_cap))
    try:
        file_size = reader.load_size()
        moov_offset, moov_size, moov = _find_moov(reader)
        estimates = estimate_video_gops(moov, _sample_times(args.duration))
    except (OSError, ValueError, MP4IndexError, httpx.HTTPError) as exc:
        print(json.dumps({
            "status": "unavailable", "error": str(exc),
            "bytes_fetched": reader.bytes_fetched,
            "index_byte_cap": reader.max_bytes,
        }, separators=(",", ":")))
        return 2

    packet_union = union_bytes(estimates)
    samples = [{
        "n": n + 1,
        "timestamp_seconds": round(item.timestamp, 9),
        "sync_sample": item.sync_sample,
        "target_sample": item.target_sample,
        "video_packet_bytes": item.packet_bytes,
    } for n, item in enumerate(estimates)]
    budget = 128 * 1024 * 1024
    report = {
        "status": "estimated",
        "video_id": args.video_id,
        "duration_seconds": args.duration,
        "file_size_bytes": file_size,
        "moov_offset": moov_offset,
        "moov_size_bytes": moov_size,
        "index_bytes_fetched": reader.bytes_fetched,
        "video_packet_union_bytes_lower_bound": packet_union,
        "index_plus_video_packet_lower_bound_bytes": reader.bytes_fetched + packet_union,
        "job_budget_bytes": budget,
        "lower_bound_exceeds_job_budget": reader.bytes_fetched + packet_union > budget,
        "caveat": "Excludes FFmpeg probe/reordering overhead, audio, and other tracks; an under-budget estimate does not guarantee completion.",
        "samples": samples,
    }
    print(json.dumps(report, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
