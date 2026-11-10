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
    from sync.scene_scraper_models import TelegramOshashResult
    from sync import scene_scrapers as _core


def _run_ffprobe_on_sparse_file(ffprobe: str, head: bytes, tail: bytes, file_size: int,
                                tail_start: int, probe_bytes: int):
    """Run ffprobe on a sparse file holding only the sampled ranges and return its JSON payload."""
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
    return json.loads(result.stdout) if result.returncode == 0 else {}


def _probe_format_data(raw_format: dict, file_size: int) -> dict[str, Any]:
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
    return data


def _probe_streams(raw_streams: list) -> list:
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
    return streams


def _summarise_probe_payload(payload: dict, file_size: int, probe_bytes: int, unavailable: dict) -> dict:
    raw_format = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    raw_streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    data = _probe_format_data(raw_format, file_size)
    streams = _probe_streams(raw_streams)
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


def probe_telegram_hash_ranges(head: bytes, tail: bytes, file_size: int, tail_start: int) -> dict:
    """Extract available media facts from the existing OSHASH byte ranges."""
    probe_bytes = len(head) + len(tail)
    unavailable = {"status": "unavailable", "bytes": probe_bytes,
                   "data": {"file_size_bytes": file_size}}
    ffprobe = os.environ.get("FFPROBE_PATH", "/usr/bin/ffprobe")
    if not shutil.which(ffprobe):
        return {**unavailable, "reason": "ffprobe_unavailable"}

    try:
        payload = _run_ffprobe_on_sparse_file(ffprobe, head, tail, file_size, tail_start, probe_bytes)
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return {**unavailable, "reason": "probe_failed"}
    return _summarise_probe_payload(payload, file_size, probe_bytes, unavailable)


def _telegram_play_url(video_id: str) -> str:
    base_url = os.environ.get("RESOLVER_API_URL", "http://resolver:8090").rstrip("/")
    if not base_url.endswith("/admin"):
        base_url += "/admin"
    return f"{base_url}/play/{quote(video_id, safe=':-_')}/stream?mode=proxy"


class _TelegramRangeReader:
    """Fetches bounded byte ranges of one Telegram stream and tracks the bytes read."""

    def __init__(self, client: Any, url: str, budget: int) -> None:
        self.client = client
        self.url = url
        self.budget = budget
        self.read = 0

    def fetch(self, start: int, end: int) -> tuple[bytes, int]:
        expected = end - start + 1
        if expected <= 0 or self.read + expected > self.budget:
            raise ValueError("Telegram fingerprint byte budget exceeded")
        reserve_telegram_fingerprint_bytes(expected)
        response_context = self.client.stream(
            "GET", self.url, headers={"Range": f"bytes={start}-{end}"}, timeout=30.0,
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
                if received > expected or self.read + received > self.budget:
                    raise ValueError("Resolver exceeded the fingerprint byte budget")
                parts.append(chunk)
            if received != expected:
                raise ValueError("Resolver returned an incomplete byte range")
            self.read += received
            return b"".join(parts), size


def _oshash_checksum(size: int, head: bytes, tail: bytes) -> int:
    checksum = size
    for offset in range(0, _core.TELEGRAM_HASH_BLOCK, 8):
        checksum += int.from_bytes(head[offset:offset + 8], "little")
        checksum += int.from_bytes(tail[offset:offset + 8], "little")
    return checksum


def telegram_oshash(video_id: str, *, http_client: Optional[Any] = None) -> TelegramOshashResult:
    """Calculate Stash-compatible OSHASH from two bounded Telegram byte ranges."""
    if not isinstance(video_id, str) or not video_id.startswith("telegram:"):
        raise ValueError("OSHASH requires a Telegram video identifier")
    parts = video_id[len("telegram:"):].rsplit(":", 2)
    if len(parts) != 3 or not parts[0] or not (parts[1].isascii() and parts[1].isdigit()) or not parts[2]:
        raise ValueError("OSHASH requires a Telegram video identifier")
    client = http_client or _core.httpx
    if client is None:
        raise RuntimeError("HTTP client is unavailable")
    reader = _TelegramRangeReader(client, _telegram_play_url(video_id), _core.TELEGRAM_RANGE_BUDGET)

    head, size = reader.fetch(0, _core.TELEGRAM_HASH_BLOCK - 1)
    if size <= _core.TELEGRAM_HASH_BLOCK:
        raise ValueError("Video is too small for OSHASH")
    # The first and last OSHASH blocks overlap for files smaller than 128 KiB.
    # Keep the last block complete; clamping to the end of the head made the
    # tail short and rejected otherwise valid files in that size range.
    tail_start = size - _core.TELEGRAM_HASH_BLOCK
    tail, tail_size = reader.fetch(tail_start, size - 1)
    if tail_size != size or len(tail) != _core.TELEGRAM_HASH_BLOCK:
        raise ValueError("Video size changed while calculating OSHASH")

    checksum = _oshash_checksum(size, head, tail)
    media_probe = _core._probe_telegram_hash_ranges(head, tail, size, tail_start)
    return TelegramOshashResult(f"{checksum & 0xFFFFFFFFFFFFFFFF:016x}", reader.read, media_probe)
