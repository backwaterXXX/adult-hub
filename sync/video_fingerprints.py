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
from typing import Any, Callable, NamedTuple, Optional
from urllib.parse import quote
import logging

import httpx
from common.telegram_transfer_budget import (
    DailyTelegramTransferBudgetExceeded,
    DailyTelegramTransferBudgetUnavailable,
    reserve_telegram_fingerprint_bytes,
)

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

    def _validated_range(self, start: int, requested_end: int) -> int:
        if self.total_size is None:
            self.load_size()
        assert self.total_size is not None
        if start < 0 or start >= self.total_size:
            raise ValueError("Invalid Telegram byte range")
        requested_end = min(requested_end, self.total_size - 1)
        if requested_end < start:
            raise ValueError("Invalid Telegram byte range")
        return requested_end

    def _memory_step(self, cursor: int, data: bytearray, target_length: int) -> tuple[int, int, int]:
        """Serve from in-memory segments. Returns (bytes served, fetch_start, fetch_length)."""
        with self.lock:
            cached = next((
                (cached_start, cached_data) for cached_start, cached_data in self.cache_segments
                if cached_start <= cursor < cached_start + len(cached_data)
            ), None)
            if cached:
                cached_start, cached_data = cached
                take = min(target_length - len(data), cached_start + len(cached_data) - cursor)
                data.extend(cached_data[cursor - cached_start:cursor - cached_start + take])
                return take, 0, 0
            # Fetch a bounded block and serve FFmpeg's smaller reads from cache.
            fetch_start = cursor // PREFETCH_BYTES * PREFETCH_BYTES
            fetch_end = min(
                fetch_start + PREFETCH_BYTES - 1,
                self.total_size - 1,
            )
            return 0, fetch_start, fetch_end - fetch_start + 1

    def _disk_step(self, fetch_start: int, cursor: int, data: bytearray,
                   target_length: int, length: int) -> int:
        """Serve from a persisted aligned block. Returns the number of bytes served (0 when unused)."""
        cached_block = self._cached_block(fetch_start)
        if cached_block is None:
            return 0
        if len(cached_block) > length:
            raise ValueError("Cached pHash range has an unexpected size")
        if not (fetch_start <= cursor < fetch_start + len(cached_block)):
            return 0
        take = min(target_length - len(data), fetch_start + len(cached_block) - cursor)
        data.extend(cached_block[cursor - fetch_start:cursor - fetch_start + take])
        return take

    def _budgeted_length(self, length: int) -> Optional[int]:
        """Clamp a fetch to the remaining budget and reserve it. Returns None when the fetch loop must stop."""
        remaining = self.budget - self.bytes_read
        if remaining <= 0:
            if self.cache_dir is None:
                return None
            raise FingerprintBudgetExceeded("Video fingerprint byte budget exhausted")
        # Persistent cache entries have fixed, aligned ranges. Do not create
        # a short final entry that a later attempt could mistake for a full block.
        if self.cache_dir is not None and remaining < length:
            raise FingerprintBudgetExceeded("Video fingerprint byte budget exhausted")
        length = min(length, remaining)
        if not self._reserve(length):
            return None
        return length

    def _download_block(self, fetch_start: int, length: int) -> bytes:
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
        return bytes(chunk_data)

    def fetch(self, start: int, requested_end: int) -> tuple[int, bytes]:
        requested_end = self._validated_range(start, requested_end)

        target_length = requested_end - start + 1
        data = bytearray()
        cursor = start
        while len(data) < target_length:
            served, fetch_start, length = self._memory_step(cursor, data, target_length)
            if served:
                cursor += served
                continue
            served = self._disk_step(fetch_start, cursor, data, target_length, length)
            if served:
                cursor += served
                continue
            length = self._budgeted_length(length)
            if length is None:
                break
            self._store_block(fetch_start, self._download_block(fetch_start, length))
        if not data:
            raise FingerprintBudgetExceeded("Video fingerprint byte budget exhausted")
        return start + len(data) - 1, bytes(data)


def _stash_phash(image) -> str:
    """Use the repository's pinned Stash-compatible implementation."""
    from stash_phash.phash import perception_hash

    return f"{perception_hash(image):016x}"


class _PhashDeps(NamedTuple):
    shutil: Any
    Image: Any
    combine_images: Callable
    compute_dhash: Callable
    is_black_image: Callable
    is_bumper_hash: Callable
    get_bumper_hashes: Callable
    valid_thumbnail_bytes: Callable


def _load_phash_dependencies() -> _PhashDeps:
    import shutil
    from PIL import Image
    from stash_phash.video import combine_images
    from common.image_utils import compute_dhash, is_black_image, is_bumper_hash
    try:
        from .thumbnails import _get_cached_bumper_hashes, _valid_thumbnail_bytes
    except ImportError:
        from thumbnails import _get_cached_bumper_hashes, _valid_thumbnail_bytes
    return _PhashDeps(shutil, Image, combine_images, compute_dhash, is_black_image,
                      is_bumper_hash, _get_cached_bumper_hashes, _valid_thumbnail_bytes)


def _phash_api_base(resolver_url: Optional[str]) -> str:
    resolved_base = (resolver_url or os.environ.get("RESOLVER_API_URL") or "http://resolver:8090").rstrip("/")
    return resolved_base if resolved_base.endswith("/admin") else resolved_base + "/admin"


def _choose_phash_variant(video_id: str, variant: Optional[str], api_base: str) -> tuple[Optional[str], Optional[int]]:
    chosen_variant = None
    original_size = None
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
    return chosen_variant, original_size


def _emit_phash_size(size_sink: Optional[Callable[[int], None]], chosen_variant: Optional[str],
                     original_size: Optional[int], total_size: Optional[int]) -> None:
    if size_sink is None:
        return
    if chosen_variant is not None:
        if original_size is not None and original_size > 0:
            try:
                size_sink(int(original_size))
            except Exception:
                logger.debug("Ignored exception in telegram_phash", exc_info=True)
    elif total_size is not None:
        try:
            size_sink(int(total_size))
        except Exception:
            logger.debug("Ignored exception in telegram_phash", exc_info=True)


def _start_range_server(budget: FingerprintBudget):
    virtual_mp4 = _FaststartMP4(budget)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    server.fingerprint_budget = budget
    server.virtual_mp4 = virtual_mp4
    server.request_ranges = []
    server.transfer_error = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _raise_frame_failure(server, budget: FingerprintBudget, result) -> None:
    """Raise the most specific error for a failed ffmpeg frame extraction."""
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


def _extract_sample_frame(ffmpeg: str, video_url: str, timestamp: float, deadline: float,
                          budget: FingerprintBudget, server, image_module):
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
        _raise_frame_failure(server, budget, result)
    image = image_module.open(BytesIO(result.stdout)).convert("RGBA")
    if image.width != 160:
        raise RuntimeError("ffmpeg returned an unexpected pHash frame size")
    return image


def _visual_sample(image, timestamp: float, compute_dhash: Callable) -> dict:
    gray = image.convert("L")
    histogram = gray.histogram()
    total = max(1, sum(histogram))
    entropy = -sum((count / total) * __import__("math").log2(count / total)
                   for count in histogram if count)
    return {
        "t": round(float(timestamp), 3),
        "phash": _stash_phash(image),
        "dhash": compute_dhash(image),
        "valid": __import__("statistics").pstdev(gray.getdata()) >= 12
                 and entropy >= 4,
    }


def _note_thumbnail_candidate(image, index: int, candidates: list, deps: _PhashDeps) -> None:
    try:
        if deps.is_black_image(image):
            return
        bumper_hashes = deps.get_bumper_hashes()
        if bumper_hashes and deps.is_bumper_hash(deps.compute_dhash(image), bumper_hashes, max_distance=4):
            return
        candidates.append(index)
    except Exception:
        # A failed optional quality check must not fail fingerprint generation.
        candidates.append(index)


def _collect_sample_frames(ffmpeg: str, video_url: str, server, budget: FingerprintBudget,
                           sample_times: list, deadline: float, deps: _PhashDeps,
                           include_visual_signature: bool, include_thumbnail: bool,
                           frames: list, visual_samples: list, thumbnail_candidates: list) -> None:
    # Stash samples the central 90% at 25 evenly spaced positions.
    for index in range(25):
        if time.monotonic() >= deadline:
            raise TimeoutError("Video pHash exceeded its time limit")
        timestamp = sample_times[index]
        image = _extract_sample_frame(ffmpeg, video_url, timestamp, deadline, budget, server, deps.Image)
        frames.append(image)
        if include_visual_signature:
            visual_samples.append(_visual_sample(image, timestamp, deps.compute_dhash))
        if include_thumbnail:
            _note_thumbnail_candidate(image, index, thumbnail_candidates, deps)


def _render_thumbnail_candidate(ffmpeg: str, video_url: str, timestamp: float,
                                deadline: float, image_module) -> Optional[bytes]:
    """Render one JPEG poster candidate. Returns None when the frame is unusable."""
    thumbnail_timeout = max(1.0, min(120.0, deadline - time.monotonic()))
    thumb = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-ss",
         str(timestamp), "-seekable", "1", "-i", video_url, "-frames:v", "1",
         "-vf", "scale='min(640,iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=thumbnail_timeout, check=False,
    )
    if thumb.returncode != 0 or not thumb.stdout or len(thumb.stdout) > 2_000_000:
        return None
    with image_module.open(BytesIO(thumb.stdout)) as thumbnail:
        thumbnail = thumbnail.convert("RGB")
        if not (320 <= thumbnail.width <= 1280) or not (1 <= thumbnail.height <= 2048):
            return None
        output = BytesIO()
        thumbnail.save(output, format="JPEG", quality=84, optimize=True)
        candidate_bytes = output.getvalue()
    if len(candidate_bytes) > 1_500_000:
        return None
    return candidate_bytes


def _select_thumbnail(ffmpeg: str, video_url: str, sample_times: list, candidates: list,
                      deadline: float, deps: _PhashDeps) -> Optional[bytes]:
    # Prefer the central frame, then nearby samples; avoid black frames and
    # known studio bumpers before persisting a fallback poster.
    candidates.sort(key=lambda index: (abs(index - 12), index))
    bumper_hashes = deps.get_bumper_hashes()
    for index in candidates:
        candidate_bytes = _render_thumbnail_candidate(
            ffmpeg, video_url, sample_times[index], deadline, deps.Image)
        if candidate_bytes is None:
            continue
        if deps.valid_thumbnail_bytes(candidate_bytes, "phash://frame", bumper_hashes,
                                      minimum_size=(180, 320)):
            return candidate_bytes
    return None


def _phash_result(sprite, budget: FingerprintBudget, duration: int, visual_samples: list,
                  thumbnail_bytes: Optional[bytes], include_thumbnail: bool,
                  include_visual_signature: bool) -> tuple:
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
        deps = _load_phash_dependencies()
    except ImportError as exc:
        raise RuntimeError(f"Pillow is required for pHash generation: {exc}") from exc
    ffmpeg = deps.shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required for pHash generation")

    api_base = _phash_api_base(resolver_url)
    chosen_variant, original_size = _choose_phash_variant(video_id, variant, api_base)

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
    _emit_phash_size(size_sink, chosen_variant, original_size, budget.total_size)
    server, thread = _start_range_server(budget)
    deadline = time.monotonic() + max(1, int(timeout_seconds))
    frames = []
    visual_samples = []
    thumbnail_candidates = []
    sample_times = _stash_sample_times(duration)
    try:
        video_url = f"http://127.0.0.1:{server.server_address[1]}/video.mp4"
        _collect_sample_frames(ffmpeg, video_url, server, budget, sample_times, deadline, deps,
                               include_visual_signature, include_thumbnail,
                               frames, visual_samples, thumbnail_candidates)
        thumbnail_bytes = None
        if include_thumbnail:
            thumbnail_bytes = _select_thumbnail(ffmpeg, video_url, sample_times,
                                                thumbnail_candidates, deadline, deps)

        if thumbnail_only:
            return None, budget.bytes_read, thumbnail_bytes
        sprite = deps.combine_images(frames)
        return _phash_result(sprite, budget, duration, visual_samples, thumbnail_bytes,
                             include_thumbnail, include_visual_signature)
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
