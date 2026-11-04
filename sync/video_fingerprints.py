"""Bounded Telegram video fingerprint extraction for scene lookup."""

from __future__ import annotations

import http.server
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from io import BytesIO
from typing import Callable, Optional
from urllib.parse import quote

import httpx
from common.telegram_transfer_budget import (
    DailyTelegramTransferBudgetExceeded,
    DailyTelegramTransferBudgetUnavailable,
    reserve_telegram_fingerprint_bytes,
)
import logging

logger = logging.getLogger(__name__)


if __package__:
    from . import fingerprint_mp4 as _fingerprint_mp4
else:
    import fingerprint_mp4 as _fingerprint_mp4

# Re-export the previous helper names for callers that import this facade.
TELEGRAM_BLOCK_BYTES = _fingerprint_mp4.TELEGRAM_BLOCK_BYTES
_box_header = _fingerprint_mp4._box_header
_FaststartMP4 = _fingerprint_mp4._FaststartMP4
_RangeHandler = _fingerprint_mp4._RangeHandler
_relocated_moov = _fingerprint_mp4._relocated_moov
_top_level_layout = _fingerprint_mp4._top_level_layout

DEFAULT_BYTE_BUDGET = 256 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 1200
PREFETCH_BYTES = 128 * 1024


class FingerprintBudgetExceeded(RuntimeError):
    """The cumulative upstream transfer allowance for this job is exhausted."""


def _stash_sample_times(duration: float) -> list[float]:
    """Keep Stash's float operation order for its 25 center-90% samples."""
    offset = 0.05 * duration
    step_size = (0.90 * duration) / 25
    return [offset + (index * step_size) for index in range(25)]


class FingerprintBudget:
    """Range gateway that never asks Telegram for more than the remaining cap."""

    def __init__(self, video_id: str, *, budget: int = DEFAULT_BYTE_BUDGET,
                 resolver_url: Optional[str] = None,
                 max_bytes_per_second: Optional[int | Callable[[], int]] = None,
                 allow_rate_above_configured: bool = False,
                 cache_dir: Optional[str] = None, cache_id: Optional[str] = None,
                 variant: Optional[str] = None):
        base = (resolver_url or os.environ.get("RESOLVER_API_URL") or "http://resolver:8090").rstrip("/")
        self.base_url = base if base.endswith("/admin") else base + "/admin"
        self.video_id = video_id
        self.variant = str(variant).strip() if variant and str(variant).strip().lower() != "original" else None
        stream_query = "mode=proxy"
        if self.variant:
            stream_query += f"&variant={quote(self.variant, safe='')}"
        self.stream_url = f"{self.base_url}/play/{quote(video_id, safe=':-_')}/stream?{stream_query}"
        self.budget = max(0, int(budget))
        self.bytes_read = 0
        self.total_size: Optional[int] = None
        self.lock = threading.Lock()
        self.cache_segments: list[tuple[int, bytes]] = []
        self.http_client = httpx
        self.max_bytes_per_second = max_bytes_per_second
        self.allow_rate_above_configured = bool(allow_rate_above_configured)
        self.cache_dir = None
        self.usage_path = None
        if cache_dir and cache_id:
            cache_identity = (
                f"{cache_id}\0{video_id}\0{self.variant}"
                if self.variant
                else f"{cache_id}\0{video_id}"
            )
            identity = hashlib.sha256(cache_identity.encode("utf-8")).hexdigest()
            self.cache_dir = os.path.join(cache_dir, identity)
            os.makedirs(self.cache_dir, mode=0o700, exist_ok=True)
            self.usage_path = os.path.join(self.cache_dir, "usage.json")
            self.bytes_read = self._read_reserved_bytes()
            if not os.path.exists(self.usage_path):
                self._write_usage()

    def _read_reserved_bytes(self) -> int:
        assert self.usage_path is not None
        try:
            with open(self.usage_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if (not isinstance(data, dict) or type(data.get("bytes_reserved")) is not int
                    or data["bytes_reserved"] < 0
                    or type(data.get("updated_at")) not in (int, float)):
                raise ValueError("Invalid pHash transfer ledger")
            return data["bytes_reserved"]
        except FileNotFoundError:
            return 0

    def _reserve(self, byte_count: int) -> bool:
        """Persist the charge before network I/O so crashes cannot reset the cap."""
        with self.lock:
            configured_rate = self.max_bytes_per_second
            if callable(configured_rate):
                configured_rate = configured_rate()
            if configured_rate is not None:
                configured_rate = max(1, int(configured_rate))
            if self.bytes_read + byte_count > self.budget:
                return False
            reserve_telegram_fingerprint_bytes(
                byte_count, max_bytes_per_second=configured_rate,
                allow_rate_above_configured=self.allow_rate_above_configured,
            )
            self.bytes_read += byte_count
            if self.usage_path is not None:
                self._write_usage()
            return True

    def _write_usage(self) -> None:
        payload = json.dumps({
            "bytes_reserved": self.bytes_read, "updated_at": time.time(),
        }).encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix="usage-", dir=self.cache_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.usage_path)
            directory_fd = os.open(self.cache_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _cached_block(self, start: int) -> Optional[bytes]:
        if self.cache_dir is None:
            return None
        path = os.path.join(self.cache_dir, f"{start}.range")
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except FileNotFoundError:
            return None

    def _store_block(self, start: int, data: bytes) -> None:
        if self.cache_dir is None:
            with self.lock:
                self.cache_segments.append((start, data))
            return
        fd, temporary = tempfile.mkstemp(prefix="range-", dir=self.cache_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, os.path.join(self.cache_dir, f"{start}.range"))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load_size(self) -> int:
        with self.http_client.stream("HEAD", self.stream_url, timeout=15.0) as response:
            response.raise_for_status()
            raw = response.headers.get("content-length")
            if not raw or not raw.isdigit():
                raise ValueError("Resolver did not provide a video size")
            self.total_size = int(raw)
        return self.total_size

    def fetch(self, start: int, requested_end: int) -> tuple[int, bytes]:
        if self.total_size is None:
            self.load_size()
        assert self.total_size is not None
        if start < 0 or start >= self.total_size:
            raise ValueError("Invalid Telegram byte range")
        requested_end = min(requested_end, self.total_size - 1)
        if requested_end < start:
            raise ValueError("Invalid Telegram byte range")

        target_length = requested_end - start + 1
        data = bytearray()
        cursor = start
        while len(data) < target_length:
            with self.lock:
                cached = next((
                    (cached_start, cached_data) for cached_start, cached_data in self.cache_segments
                    if cached_start <= cursor < cached_start + len(cached_data)
                ), None)
                if cached:
                    cached_start, cached_data = cached
                    take = min(target_length - len(data), cached_start + len(cached_data) - cursor)
                    data.extend(cached_data[cursor - cached_start:cursor - cached_start + take])
                    cursor += take
                    continue
                # Fetch a bounded block and serve FFmpeg's smaller reads from cache.
                fetch_start = cursor // PREFETCH_BYTES * PREFETCH_BYTES
                fetch_end = min(
                    fetch_start + PREFETCH_BYTES - 1,
                    self.total_size - 1,
                )
                length = fetch_end - fetch_start + 1
            cached_block = self._cached_block(fetch_start)
            if cached_block is not None:
                if len(cached_block) > length:
                    raise ValueError("Cached pHash range has an unexpected size")
                if fetch_start <= cursor < fetch_start + len(cached_block):
                    take = min(target_length - len(data), fetch_start + len(cached_block) - cursor)
                    data.extend(cached_block[cursor - fetch_start:cursor - fetch_start + take])
                    cursor += take
                    continue
            remaining = self.budget - self.bytes_read
            if remaining <= 0:
                if self.cache_dir is None:
                    break
                raise FingerprintBudgetExceeded("Video fingerprint byte budget exhausted")
            # Persistent cache entries have fixed, aligned ranges. Do not create
            # a short final entry that a later attempt could mistake for a full block.
            if self.cache_dir is not None and remaining < length:
                raise FingerprintBudgetExceeded("Video fingerprint byte budget exhausted")
            length = min(length, remaining)
            if not self._reserve(length):
                break
            end = fetch_start + length - 1
            with self.http_client.stream(
                "GET", self.stream_url,
                headers={"Range": f"bytes={fetch_start}-{end}",
                         "X-Adult-Hub-Transfer": "phash"}, timeout=20.0,
            ) as response:
                if response.status_code != 206:
                    raise ValueError("Resolver did not honor a bounded byte range")
                content_range = str(response.headers.get("content-range") or "")
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                if not match or tuple(map(int, match.groups())) != (fetch_start, end, self.total_size):
                    raise ValueError("Resolver returned an unexpected byte range")
                chunk_data = bytearray()
                for chunk in response.iter_bytes():
                    if len(chunk_data) + len(chunk) > length:
                        raise ValueError("Resolver exceeded the requested range")
                    chunk_data.extend(chunk)
                if len(chunk_data) != length:
                    raise ValueError("Resolver returned an incomplete byte range")
            self._store_block(fetch_start, bytes(chunk_data))
        if not data:
            raise FingerprintBudgetExceeded("Video fingerprint byte budget exhausted")
        return start + len(data) - 1, bytes(data)



def _stash_phash(image) -> str:
    """Use the repository's pinned Stash-compatible implementation."""
    from stash_phash.phash import perception_hash

    return f"{perception_hash(image):016x}"


def telegram_phash(video_id: str, duration: int, *, byte_budget: int = DEFAULT_BYTE_BUDGET,
                   timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
                   resolver_url: Optional[str] = None,
                   max_bytes_per_second: Optional[int | Callable[[], int]] = None,
                   allow_rate_above_configured: bool = False,
                   cache_dir: Optional[str] = None, cache_id: Optional[str] = None,
                   include_thumbnail: bool = False,
                   include_visual_signature: bool = False,
                   thumbnail_only: bool = False,
                   size_sink: Optional[Callable[[int], None]] = None,
                   variant: Optional[str] = "auto"):
    """Generate the Stash video pHash using a strict cumulative range budget.

    FFmpeg seeks separately to the 25 frames in Stash's 5x5 montage. Its HTTP
    input is a loopback range server, which proxies bounded 128 KiB Telegram
    requests through the resolver and persists a per-job transfer ledger.

    ``size_sink`` receives the total file size learned from Content-Length; a
    sink failure must never abort hashing.
    """
    if duration <= 0:
        raise ValueError("A known video duration is required for pHash")
    try:
        import shutil
        from PIL import Image
        from stash_phash.video import combine_images
        from common.image_utils import compute_dhash, is_black_image, is_bumper_hash
        try:
            from .thumbnails import _get_cached_bumper_hashes, _valid_thumbnail_bytes
        except ImportError:
            from thumbnails import _get_cached_bumper_hashes, _valid_thumbnail_bytes
    except ImportError as exc:
        raise RuntimeError(f"Pillow is required for pHash generation: {exc}") from exc
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for pHash generation")

    chosen_variant = None
    original_size = None
    resolved_base = (resolver_url or os.environ.get("RESOLVER_API_URL") or "http://resolver:8090").rstrip("/")
    api_base = resolved_base if resolved_base.endswith("/admin") else resolved_base + "/admin"

    if variant == "auto":
        try:
            from common.video_variants import fetch_and_select_best_variant_with_meta
            chosen_variant, original_size = fetch_and_select_best_variant_with_meta(
                api_base, video_id, purpose="phash",
            )
        except Exception:
            chosen_variant = None
    elif variant and str(variant).strip().lower() != "original":
        chosen_variant = str(variant).strip()

    budget = FingerprintBudget(
        video_id,
        budget=byte_budget,
        resolver_url=resolver_url,
        max_bytes_per_second=max_bytes_per_second,
        allow_rate_above_configured=allow_rate_above_configured,
        cache_dir=cache_dir,
        cache_id=cache_id,
        variant=chosen_variant,
    )
    budget.load_size()
    if size_sink is not None:
        if chosen_variant is not None:
            if original_size is not None and original_size > 0:
                try:
                    size_sink(int(original_size))
                except Exception as _exc:
                    logger.debug("Ignored exception in %s", "telegram_phash", exc_info=True)
        elif budget.total_size is not None:
            try:
                size_sink(int(budget.total_size))
            except Exception as _exc:
                logger.debug("Ignored exception in %s", "telegram_phash", exc_info=True)
    virtual_mp4 = _FaststartMP4(budget)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    server.fingerprint_budget = budget
    server.virtual_mp4 = virtual_mp4
    server.request_ranges = []
    server.transfer_error = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    frames = []
    visual_samples = []
    thumbnail_bytes = None
    thumbnail_candidates = []
    sample_times = _stash_sample_times(duration)
    try:
        video_url = f"http://127.0.0.1:{server.server_address[1]}/video.mp4"
        # Stash samples the central 90% at 25 evenly spaced positions.
        for index in range(25):
            if time.monotonic() >= deadline:
                raise TimeoutError("Video pHash exceeded its time limit")
            timestamp = sample_times[index]
            timeout = max(1.0, min(120.0, deadline - time.monotonic()))
            try:
                result = subprocess.run(
                    [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss",
                     str(timestamp), "-seekable", "1", "-i", video_url, "-frames:v", "1", "-vf", "scale=160:-2",
                     "-f", "image2pipe", "-vcodec", "bmp", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError(
                    f"ffmpeg timed out extracting pHash frame at {timestamp:.1f}s "
                    f"after reading {budget.bytes_read} bytes"
                ) from exc
            if result.returncode != 0 or not result.stdout:
                transfer_error = str(getattr(server, "transfer_error", "")).lower()
                if "fingerprint transfer ledger" in transfer_error:
                    raise DailyTelegramTransferBudgetUnavailable(transfer_error)
                if "telegram fingerprint daily transfer" in transfer_error:
                    raise DailyTelegramTransferBudgetExceeded(transfer_error)
                if "budget exhausted" in transfer_error:
                    raise FingerprintBudgetExceeded(
                        f"pHash job exhausted its {budget.budget}-byte transfer budget "
                        f"after reserving {budget.bytes_read} bytes"
                    )
                raise RuntimeError(
                    f"ffmpeg could not extract pHash frame ({result.returncode}): "
                    f"{result.stderr.decode('utf-8', errors='replace')[:400]} "
                    f"(range bytes={budget.bytes_read}, requests={server.request_ranges[-8:]})"
                )
            image = Image.open(BytesIO(result.stdout)).convert("RGBA")
            if image.width != 160:
                raise RuntimeError("ffmpeg returned an unexpected pHash frame size")
            frames.append(image)
            if include_visual_signature:
                gray = image.convert("L")
                histogram = gray.histogram()
                total = max(1, sum(histogram))
                entropy = -sum((count / total) * __import__("math").log2(count / total)
                               for count in histogram if count)
                visual_samples.append({
                    "t": round(float(timestamp), 3),
                    "phash": _stash_phash(image),
                    "dhash": compute_dhash(image),
                    "valid": __import__("statistics").pstdev(gray.getdata()) >= 12
                             and entropy >= 4,
                })
            if include_thumbnail:
                try:
                    if is_black_image(image):
                        continue
                    bumper_hashes = _get_cached_bumper_hashes()
                    if bumper_hashes and is_bumper_hash(compute_dhash(image), bumper_hashes, max_distance=4):
                        continue
                    thumbnail_candidates.append(index)
                except Exception:
                    # A failed optional quality check must not fail fingerprint generation.
                    thumbnail_candidates.append(index)

        if include_thumbnail:
            # Prefer the central frame, then nearby samples; avoid black frames and
            # known studio bumpers before persisting a fallback poster.
            thumbnail_candidates.sort(key=lambda index: (abs(index - 12), index))
            bumper_hashes = _get_cached_bumper_hashes()
            for index in thumbnail_candidates:
                thumbnail_timeout = max(1.0, min(120.0, deadline - time.monotonic()))
                thumb = subprocess.run(
                    [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss",
                     str(sample_times[index]), "-seekable", "1", "-i", video_url, "-frames:v", "1",
                     "-vf", "scale='min(640,iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=thumbnail_timeout, check=False,
                )
                if thumb.returncode != 0 or not thumb.stdout or len(thumb.stdout) > 2_000_000:
                    continue
                with Image.open(BytesIO(thumb.stdout)) as thumbnail:
                    thumbnail = thumbnail.convert("RGB")
                    if not (320 <= thumbnail.width <= 1280) or not (1 <= thumbnail.height <= 2048):
                        continue
                    output = BytesIO()
                    thumbnail.save(output, format="JPEG", quality=84, optimize=True)
                    candidate_bytes = output.getvalue()
                if len(candidate_bytes) > 1_500_000:
                    continue
                if _valid_thumbnail_bytes(candidate_bytes, "phash://frame", bumper_hashes,
                                          minimum_size=(180, 320)):
                    thumbnail_bytes = candidate_bytes
                    break

        if thumbnail_only:
            return None, budget.bytes_read, thumbnail_bytes
        sprite = combine_images(frames)
        result = (_stash_phash(sprite), budget.bytes_read)
        if include_visual_signature:
            signature = {"algorithm": "visual-sequence-v1", "duration": float(duration), "samples": visual_samples}
            visual_sheet = BytesIO()
            sprite.convert("RGB").save(visual_sheet, format="JPEG", quality=62, optimize=True)
            sheet_bytes = visual_sheet.getvalue()
            if len(sheet_bytes) > 1_000_000:
                raise RuntimeError("visual contact sheet exceeded its storage limit")
            return (*result, thumbnail_bytes, signature, sheet_bytes) if include_thumbnail else (*result, signature, sheet_bytes)
        return (*result, thumbnail_bytes) if include_thumbnail else result
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def telegram_frame_samples(video_id: str, timestamps: list[float], *,
                          byte_budget: int = DEFAULT_BYTE_BUDGET,
                          timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
                          resolver_url: Optional[str] = None,
                          cache_dir: Optional[str] = None,
                          cache_id: Optional[str] = None) -> list[bytes]:
    """Extract bounded grayscale 160x90 frames at caller-selected timestamps."""
    import shutil
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for visual verification")
    if not timestamps or len(timestamps) > 256 or any(float(t) < 0 for t in timestamps):
        raise ValueError("Invalid visual verification timestamps")
    budget = FingerprintBudget(video_id, budget=byte_budget, resolver_url=resolver_url,
                               cache_dir=cache_dir, cache_id=cache_id, variant=None)
    budget.load_size()
    virtual_mp4 = _FaststartMP4(budget)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    server.fingerprint_budget = budget
    server.virtual_mp4 = virtual_mp4
    server.request_ranges = []
    server.transfer_error = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    output = []
    try:
        video_url = f"http://127.0.0.1:{server.server_address[1]}/video.mp4"
        for timestamp in timestamps:
            if time.monotonic() >= deadline:
                raise TimeoutError("Visual verification exceeded its time limit")
            remaining = max(1.0, min(90.0, deadline - time.monotonic()))
            result = subprocess.run(
                [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss",
                 f"{float(timestamp):.3f}", "-seekable", "1", "-i", video_url,
                 "-frames:v", "1", "-vf", "scale=160:90,format=gray", "-pix_fmt", "gray",
                 "-f", "rawvideo", "pipe:1"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=remaining, check=False,
            )
            if result.returncode or len(result.stdout) != 160 * 90:
                detail = str(getattr(server, "transfer_error", "") or "")
                if "budget exhausted" in detail.lower():
                    raise FingerprintBudgetExceeded("Visual verification byte budget exhausted")
                raise RuntimeError(f"Could not extract visual verification frame at {timestamp:.3f}s")
            output.append(result.stdout)
        return output
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
