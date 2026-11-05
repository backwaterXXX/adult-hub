#!/usr/bin/env python3
"""Bounded OSHASH plus MP4 probe pilot using only exact Telegram byte ranges.

Run inside the sync container. The first and last 64 KiB are shared with
OSHASH, then the sparse MP4 view grows in 64 KiB steps at both ends. No open-
ended request is issued, so each attempt has a strict byte budget.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from urllib.parse import quote

import httpx

from common import db

RESOLVER = os.environ.get("RESOLVER_API_URL", "http://resolver:8090").rstrip("/")
BLOCK = 64 * 1024
MIB = 1024 * 1024


def sample_ids(limit=10):
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, duration FROM videos WHERE provider='telegram' "
            "AND coalesce(duration,0)>0 ORDER BY duration, first_seen NULLS LAST, id"
        ).fetchall()
    finally:
        conn.close()
    if len(rows) <= limit:
        return rows
    return [rows[round(i * (len(rows) - 1) / (limit - 1))] for i in range(limit)]


def get_range(video_id, start, end):
    url = f"{RESOLVER}/admin/play/{quote(video_id, safe=':-_')}/stream?mode=proxy"
    with httpx.stream("GET", url, headers={"Range": f"bytes={start}-{end}"}, timeout=45) as response:
        if response.status_code != 206:
            raise RuntimeError(f"range returned HTTP {response.status_code}")
        expected = f"bytes {start}-{end}/"
        content_range = response.headers.get("content-range", "")
        if not content_range.startswith(expected):
            raise RuntimeError("range response did not match the requested bytes")
        total = int(content_range.rsplit("/", 1)[1])
        data = response.read()
    if len(data) != end - start + 1:
        raise RuntimeError("range response was incomplete")
    return data, total


def _oshash_from_blocks(head, tail, total):
    # The two exact ranges are precisely the data used by the OSHASH algorithm.
    checksum = total
    for data in (head, tail):
        for offset in range(0, BLOCK, 8):
            checksum += int.from_bytes(data[offset:offset + 8], "little")
    return f"{checksum & 0xffffffffffffffff:016x}"


def _write_initial_ranges(tmp, head, tail, tail_start, total):
    tmp.truncate(total)
    tmp.seek(0)
    tmp.write(head)
    tmp.seek(tail_start)
    tmp.write(tail)
    tmp.flush()


def _grow_sparse_view(tmp, video_id, total, side_size, target_side):
    begin, end = side_size, target_side - 1
    data, _ = get_range(video_id, begin, end)
    tmp.seek(begin)
    tmp.write(data)
    start = max(BLOCK, total - target_side)
    stop = max(BLOCK, total - side_size)
    if start < stop:
        data, _ = get_range(video_id, start, stop - 1)
        tmp.seek(start)
        tmp.write(data)


def _ffprobe_media(path, max_budget):
    p = subprocess.run(
        ["/usr/bin/ffprobe", "-v", "error", "-probesize", str(max_budget),
         "-analyzeduration", str(max_budget), "-show_streams", "-show_format",
         "-of", "json", path], capture_output=True, text=True, timeout=30,
    )
    try:
        return json.loads(p.stdout) if p.returncode == 0 else {}
    except ValueError:
        return {}


def _make_checkpoint(used, media):
    streams = media.get("streams", [])
    duration = (media.get("format") or {}).get("duration")
    checkpoint = {"budget_mib": used // MIB, "bytes_total": used,
                  "complete": bool(duration and any(s.get("codec_type") == "video" for s in streams))}
    if checkpoint["complete"]:
        checkpoint["duration"] = duration
        checkpoint["streams"] = [(s.get("codec_type"), s.get("codec_name")) for s in streams]
    return checkpoint


def probe_sample(video_id, source_duration, max_budget):
    head, total = get_range(video_id, 0, BLOCK - 1)
    tail_start = max(BLOCK, total - BLOCK)
    tail, tail_total = get_range(video_id, tail_start, total - 1)
    if tail_total != total:
        raise RuntimeError("file size changed during range reads")
    oshash = _oshash_from_blocks(head, tail, total)

    with tempfile.NamedTemporaryFile(prefix="adult-hub-probe-", suffix=".mp4") as tmp:
        _write_initial_ranges(tmp, head, tail, tail_start, total)
        used = 2 * BLOCK
        result = None
        checkpoints = []
        side_size = BLOCK
        for target_side in range(MIB // 2, max_budget // 2 + 1, MIB // 2):
            if target_side > side_size:
                _grow_sparse_view(tmp, video_id, total, side_size, target_side)
                used += (target_side - side_size) * 2
                side_size = target_side
                tmp.flush()
            if used > max_budget:
                break
            checkpoint = _make_checkpoint(used, _ffprobe_media(tmp.name, max_budget))
            checkpoints.append(checkpoint)
            if checkpoint["complete"]:
                result = {**checkpoint, "oshash": oshash, "checkpoints": checkpoints}
                break
        return result or {"complete": False, "bytes_total": used, "oshash": oshash,
                          "source_duration_s": source_duration, "checkpoints": checkpoints}


def main():
    results = []
    if len(sys.argv) > 1:
        ids = [(video_id, 0) for video_id in sys.argv[1:]]
    else:
        ids = sample_ids()
    for video_id, duration in ids:
        try:
            result = probe_sample(str(video_id), duration, 10 * MIB)
            results.append({"video_id": video_id, "source_duration_s": duration, **result})
        except Exception as exc:
            results.append({"video_id": video_id, "error": type(exc).__name__})
    print(json.dumps(results, separators=(",", ":")))


if __name__ == "__main__":
    main()
