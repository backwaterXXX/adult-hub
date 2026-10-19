"""Bounded Telegram OSHASH calculation and metadata probing."""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Optional
from urllib.parse import quote
from common.telegram_transfer_budget import reserve_telegram_fingerprint_bytes

try:
    from sync.scene_scraper_models import TelegramOshashResult
    from sync import scene_scrapers as _core
except ModuleNotFoundError:
    from scene_scraper_models import TelegramOshashResult
    import scene_scrapers as _core


def probe_telegram_hash_ranges(head: bytes, tail: bytes, file_size: int, tail_start: int) -> dict:
    """Extract available media facts from the existing OSHASH byte ranges."""
    probe_bytes = len(head) + len(tail)
    unavailable = {"status": "unavailable", "bytes": probe_bytes,
                   "data": {"file_size_bytes": file_size}}
    ffprobe = os.environ.get("FFPROBE_PATH", "/usr/bin/ffprobe")
    if not shutil.which(ffprobe):
        return {**unavailable, "reason": "ffprobe_unavailable"}

    try:
        with tempfile.NamedTemporaryFile(prefix="adult-hub-oshash-", suffix=".mp4") as sparse:
            sparse.truncate(file_size)
            sparse.seek(0)
            sparse.write(head)
            sparse.seek(tail_start)
            sparse.write(tail)
            sparse.flush()
            result = subprocess.run(
                [ffprobe, "-v", "error", "-probesize", str(probe_bytes),
                 "-analyzeduration", str(probe_bytes), "-show_streams", "-show_format",
                 "-of", "json", sparse.name],
                capture_output=True, text=True, timeout=5,
            )
        payload = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return {**unavailable, "reason": "probe_failed"}

    raw_format = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    raw_streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    data: dict[str, Any] = {"file_size_bytes": file_size}
    format_name = raw_format.get("format_name")
    if isinstance(format_name, str) and format_name:
        data["format_name"] = format_name
    try:
        duration_value = float(raw_format.get("duration"))
        if math.isfinite(duration_value) and duration_value >= 0:
            data["duration_seconds"] = duration_value
    except (TypeError, ValueError):
        pass

    streams = []
    for raw_stream in raw_streams:
        if not isinstance(raw_stream, dict):
            continue
        stream = {
            key: raw_stream[key]
            for key in (
                "index", "codec_type", "codec_name", "profile", "width", "height",
                "avg_frame_rate", "sample_rate", "channels", "channel_layout", "bit_rate",
            )
            if raw_stream.get(key) is not None
        }
        if stream:
            streams.append(stream)
    if streams:
        data["streams"] = streams

    if data.get("duration_seconds") is not None and any(
        stream.get("codec_type") == "video" for stream in streams
    ):
        status = "complete"
    elif len(data) > 1:
        status = "partial"
    else:
        return {**unavailable, "reason": "no_metadata_in_sampled_ranges"}
    return {"status": status, "bytes": probe_bytes, "data": data}


def telegram_oshash(video_id: str, *, http_client: Optional[Any] = None) -> TelegramOshashResult:
    """Calculate Stash-compatible OSHASH from two bounded Telegram byte ranges."""
    if not isinstance(video_id, str) or not re.fullmatch(r"telegram:-?\d+:\d+:\d+", video_id):
        raise ValueError("OSHASH requires a Telegram video identifier")
    client = http_client or _core.httpx
    if client is None:
        raise RuntimeError("HTTP client is unavailable")
    base_url = os.environ.get("RESOLVER_API_URL", "http://resolver:8090").rstrip("/")
    if not base_url.endswith("/admin"):
        base_url += "/admin"
    url = f"{base_url}/play/{quote(video_id, safe=':-_')}/stream?mode=proxy"
    budget = _core.TELEGRAM_RANGE_BUDGET
    read = 0

    def fetch_range(start: int, end: int) -> tuple[bytes, int]:
        nonlocal read
        expected = end - start + 1
        if expected <= 0 or read + expected > budget:
            raise ValueError("Telegram fingerprint byte budget exceeded")
        reserve_telegram_fingerprint_bytes(expected)
        response_context = client.stream(
            "GET", url, headers={"Range": f"bytes={start}-{end}"}, timeout=30.0,
        )
        with response_context as response:
            if response.status_code != 206:
                raise ValueError("Resolver did not honor the bounded byte range")
            content_range = str(response.headers.get("content-range") or "")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
            if not match or int(match.group(1)) != start or int(match.group(2)) != end:
                raise ValueError("Resolver returned an unexpected byte range")
            size = int(match.group(3))
            parts = []
            received = 0
            for chunk in response.iter_bytes():
                received += len(chunk)
                if received > expected or read + received > budget:
                    raise ValueError("Resolver exceeded the fingerprint byte budget")
                parts.append(chunk)
            if received != expected:
                raise ValueError("Resolver returned an incomplete byte range")
            read += received
            return b"".join(parts), size

    head, size = fetch_range(0, _core.TELEGRAM_HASH_BLOCK - 1)
    if size <= _core.TELEGRAM_HASH_BLOCK:
        raise ValueError("Video is too small for OSHASH")
    # The first and last OSHASH blocks overlap for files smaller than 128 KiB.
    # Keep the last block complete; clamping to the end of the head made the
    # tail short and rejected otherwise valid files in that size range.
    tail_start = size - _core.TELEGRAM_HASH_BLOCK
    tail, tail_size = fetch_range(tail_start, size - 1)
    if tail_size != size or len(tail) != _core.TELEGRAM_HASH_BLOCK:
        raise ValueError("Video size changed while calculating OSHASH")

    checksum = size
    for offset in range(0, _core.TELEGRAM_HASH_BLOCK, 8):
        checksum += int.from_bytes(head[offset:offset + 8], "little")
        checksum += int.from_bytes(tail[offset:offset + 8], "little")
    media_probe = _core._probe_telegram_hash_ranges(head, tail, size, tail_start)
    return TelegramOshashResult(f"{checksum & 0xFFFFFFFFFFFFFFFF:016x}", read, media_probe)
