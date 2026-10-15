#!/usr/bin/env python3
"""Check what ffprobe can learn from exactly the two OSHASH byte ranges."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx

from common import db

RESOLVER = os.environ.get("RESOLVER_API_URL", "http://resolver:8090").rstrip("/")
BLOCK = 64 * 1024
PROBE_BYTES = 2 * BLOCK


def random_videos(limit: int = 11):
    conn = db.connect()
    try:
        return conn.execute(
            "SELECT id, duration FROM videos WHERE provider='telegram' "
            "AND coalesce(duration,0)>0 ORDER BY random() LIMIT %s",
            (limit,),
        ).fetchall()
    finally:
        conn.close()


def get_range(video_id: str, start: int, end: int):
    url = f"{RESOLVER}/admin/play/{quote(video_id, safe=':-_')}/stream?mode=proxy"
    with httpx.stream(
        "GET", url, headers={"Range": f"bytes={start}-{end}"}, timeout=45,
    ) as response:
        if response.status_code != 206:
            raise RuntimeError(f"range returned HTTP {response.status_code}")
        content_range = response.headers.get("content-range", "")
        if not content_range.startswith(f"bytes {start}-{end}/"):
            raise RuntimeError("range response did not match the requested bytes")
        size = int(content_range.rsplit("/", 1)[1])
        data = response.read()
    if len(data) != end - start + 1:
        raise RuntimeError("range response was incomplete")
    return data, size


def probe(path: str):
    result = subprocess.run(
        ["/usr/bin/ffprobe", "-v", "error", "-probesize", str(PROBE_BYTES),
         "-analyzeduration", str(PROBE_BYTES), "-show_streams", "-show_format",
         "-of", "json", path],
        capture_output=True, text=True, timeout=30,
    )
    try:
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
    except ValueError:
        payload = {}
    streams = payload.get("streams", [])
    duration = (payload.get("format") or {}).get("duration")
    complete = bool(duration and any(s.get("codec_type") == "video" for s in streams))
    return {
        "complete": complete,
        "duration": duration,
        "format": (payload.get("format") or {}).get("format_name"),
        "streams": [(s.get("codec_type"), s.get("codec_name")) for s in streams],
        "stderr": result.stderr.strip()[:300] if not complete else None,
    }


def test_video(video_id: str, source_duration):
    head, size = get_range(video_id, 0, BLOCK - 1)
    tail_start = max(BLOCK, size - BLOCK)
    tail, tail_size = get_range(video_id, tail_start, size - 1)
    if size != tail_size:
        raise RuntimeError("file size changed between range reads")

    with tempfile.TemporaryDirectory(prefix="oshash-128k-") as directory:
        head_path = Path(directory) / "head.mp4"
        with head_path.open("wb") as stream:
            stream.truncate(size)
            stream.write(head)
        head_result = probe(str(head_path))

        combined_path = Path(directory) / "head-tail.mp4"
        with combined_path.open("wb") as stream:
            stream.truncate(size)
            stream.write(head)
            stream.seek(tail_start)
            stream.write(tail)
        combined_result = probe(str(combined_path))

    return {
        "video_id": video_id,
        "source_duration_s": source_duration,
        "bytes_downloaded": 2 * BLOCK,
        "head_only": head_result,
        "oshash_head_tail": combined_result,
    }


def main():
    output = []
    rows = random_videos()
    for video_id, duration in rows:
        try:
            output.append(test_video(str(video_id), duration))
        except Exception as exc:
            output.append({"video_id": video_id, "error": type(exc).__name__})
    print(json.dumps(output, separators=(",", ":")))


if __name__ == "__main__":
    main()
