"""Image validation, perceptual hashing, and frame extraction utilities."""

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union

from PIL import Image, ImageStat

logger = logging.getLogger("common.image_utils")

# Known exact MD5 hashes of 100% black thumbnails from Telegram / broken syncs
KNOWN_BLACK_IMAGE_MD5S = {
    "12e8c0e2e2e1e48d5d9f493b2e37136f",
    "6d0bd622d9e60c7791a85c7ea04a6cf6",
    "22f39beb11dee0d03252d330e50ada50",
    "9d8f8d9b15b3c3741829e1f91c9d89d4",
}

# Timeout limits for frame extraction attempts
DEFAULT_FRAME_EXTRACTION_TIMEOUT = 20.0
DEFAULT_ARTWORK_EXTRACTION_TIMEOUT = 90.0


def is_black_image(
    data_or_path: Union[bytes, str, Path, Image.Image],
    max_brightness_threshold: int = 25,
    mean_threshold: float = 5.0,
) -> bool:
    """Return True if the image is entirely or almost entirely black.

    Evaluates both peak brightness across color channels (getextrema) and
    overall mean brightness (ImageStat).
    """
    img = None
    try:
        if isinstance(data_or_path, Image.Image):
            img = data_or_path
        elif isinstance(data_or_path, bytes):
            if not data_or_path:
                return False
            img = Image.open(io.BytesIO(data_or_path))
        else:
            path = Path(data_or_path)
            if not path.is_file():
                return False
            img = Image.open(path)

        # Inspect pixel extrema across channels
        extrema = img.getextrema()
        if isinstance(extrema, (list, tuple)) and len(extrema) > 0:
            if isinstance(extrema[0], tuple):
                # RGB / RGBA / CMYK: list of (min, max) per channel
                max_val = max(e[1] for e in extrema[:3])
            else:
                # Grayscale / 1-channel: (min, max)
                max_val = extrema[1] if len(extrema) > 1 else extrema[0]
            if max_val < max_brightness_threshold:
                return True

        # Inspect mean brightness across RGB channels
        stat = ImageStat.Stat(img)
        means = stat.mean[:3] if len(stat.mean) >= 3 else stat.mean
        if means:
            avg_mean = sum(means) / len(means)
            if avg_mean < mean_threshold:
                return True

        return False
    except Exception as exc:
        logger.debug("Could not determine if image is black: %s", exc)
        return False
    finally:
        if img is not None and not isinstance(data_or_path, Image.Image):
            try:
                img.close()
            except Exception:
                pass


def compute_dhash(
    img_or_path: Union[bytes, str, Path, Image.Image],
    hash_size: int = 8,
) -> str:
    """Compute a 64-bit difference hash (dHash) as a 16-character hex string.

    Fast perceptual hash ideal for grouping identical or near-identical
    bumpers, intro animations, and studio logos.
    """
    img = None
    try:
        if isinstance(img_or_path, Image.Image):
            source = img_or_path
        elif isinstance(img_or_path, bytes):
            source = Image.open(io.BytesIO(img_or_path))
            img = source
        else:
            source = Image.open(str(img_or_path))
            img = source

        # Convert to grayscale and resize to (hash_size + 1, hash_size)
        resized = source.convert("L").resize(
            (hash_size + 1, hash_size),
            Image.Resampling.BILINEAR,
        )
        raw_bytes = resized.tobytes()

        val = 0
        stride = hash_size + 1
        for row in range(hash_size):
            offset = row * stride
            for col in range(hash_size):
                bit = 1 if raw_bytes[offset + col] > raw_bytes[offset + col + 1] else 0
                val = (val << 1) | bit

        return f"{val:016x}"
    finally:
        if img is not None:
            try:
                img.close()
            except Exception:
                pass


def hamming_distance(h1: str, h2: str) -> int:
    """Calculate the bitwise Hamming distance between two hex hashes."""
    try:
        return (int(h1, 16) ^ int(h2, 16)).bit_count()
    except (ValueError, TypeError):
        return 64


def find_bumper_clusters(
    file_hashes: Dict[Union[str, Path], str],
    max_distance: int = 4,
    min_count: int = 5,
) -> List[Dict[str, Union[str, Set[str], List[Path], int]]]:
    """Cluster image file hashes using bitwise Hamming distance on dHash.

    Groups files where hamming_distance(h1, h2) <= max_distance.
    Clusters with total count >= min_count are returned, sorted by count descending.
    Each cluster dictionary contains:
      - 'representative_hash': the most frequent 16-hex dHash in the cluster
      - 'hashes': set of all hex hashes present in the cluster
      - 'files': list of Path objects in the cluster
      - 'count': total number of files in the cluster
    """
    if not file_hashes:
        return []

    hash_to_files: Dict[str, List[Path]] = defaultdict(list)
    for p, h in file_hashes.items():
        if h:
            h_clean = str(h).strip().lower()
            if len(h_clean) == 16:
                hash_to_files[h_clean].append(Path(p))

    if not hash_to_files:
        return []

    unique_hexes = list(hash_to_files.keys())
    valid_hexes: List[str] = []
    unique_ints: List[int] = []
    for h in unique_hexes:
        try:
            val = int(h, 16)
            unique_ints.append(val)
            valid_hexes.append(h)
        except ValueError:
            continue

    n = len(unique_ints)
    parent = list(range(n))

    def _find(i: int) -> int:
        root = i
        while parent[root] != root:
            root = parent[root]
        curr = i
        while curr != root:
            nxt = parent[curr]
            parent[curr] = root
            curr = nxt
        return root

    def _union(i: int, j: int) -> None:
        ri, rj = _find(i), _find(j)
        if ri != rj:
            parent[ri] = rj

    if max_distance > 0:
        for i in range(n):
            hi = unique_ints[i]
            for j in range(i + 1, n):
                if (hi ^ unique_ints[j]).bit_count() <= max_distance:
                    _union(i, j)

    groups: Dict[int, List[int]] = defaultdict(list)
    for idx in range(n):
        root = _find(idx)
        groups[root].append(idx)

    clusters: List[Dict[str, Union[str, Set[str], List[Path], int]]] = []
    for idxs in groups.values():
        cluster_files: List[Path] = []
        cluster_hashes: Set[str] = set()
        hash_counts: Dict[str, int] = {}
        for idx in idxs:
            h_str = valid_hexes[idx]
            files = hash_to_files[h_str]
            cluster_files.extend(files)
            cluster_hashes.add(h_str)
            hash_counts[h_str] = len(files)

        if len(cluster_files) >= min_count:
            rep_hash = max(hash_counts.items(), key=lambda kv: kv[1])[0]
            clusters.append({
                "representative_hash": rep_hash,
                "hashes": cluster_hashes,
                "files": cluster_files,
                "count": len(cluster_files),
            })

    clusters.sort(key=lambda c: int(c["count"]), reverse=True)
    return clusters


def is_bumper_hash(
    dhash_hex_or_int: Union[str, int],
    bumper_hashes: Union[Set[int], List[int], Set[str], List[str]],
    max_distance: int = 4,
) -> bool:
    """Return True if dhash matches any known bumper hash within max_distance bits."""
    if not dhash_hex_or_int or not bumper_hashes:
        return False

    try:
        if isinstance(dhash_hex_or_int, int):
            target_val = dhash_hex_or_int
        else:
            target_val = int(str(dhash_hex_or_int).strip(), 16)
    except (ValueError, TypeError):
        return False

    for b in bumper_hashes:
        try:
            b_val = b if isinstance(b, int) else int(str(b).strip(), 16)
            if (target_val ^ b_val).bit_count() <= max_distance:
                return True
        except (ValueError, TypeError):
            continue

    return False


def load_bumper_hashes(cache_path: Union[str, Path]) -> Set[int]:
    """Load bumper dHashes from a JSON cache file as a set of 64-bit integers."""
    path = Path(cache_path)
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_list = data.get("dhashes", []) if isinstance(data, dict) else data
        result: Set[int] = set()
        for item in raw_list:
            if isinstance(item, int):
                result.add(item)
            elif isinstance(item, str):
                try:
                    result.add(int(item.strip(), 16))
                except ValueError:
                    pass
        return result
    except Exception as exc:
        logger.warning("Failed to load bumper hashes from %s: %s", path, exc)
        return set()


def save_bumper_hashes(
    cache_path: Union[str, Path],
    hashes: Union[Set[Union[str, int]], List[Union[str, int]]],
    metadata: Optional[dict] = None,
) -> None:
    """Atomically save bumper dHashes to a JSON cache file."""
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    hex_list = sorted({
        f"{h:016x}" if isinstance(h, int) else str(h).strip().lower()
        for h in hashes
        if h
    })

    payload = {
        "updated_at": time.time(),
        "count": len(hex_list),
        "dhashes": hex_list,
    }
    if metadata:
        payload.update(metadata)

    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def extract_video_frame(
    stream_url: str,
    dest_path: Union[str, Path],
    offset_seconds: int = 60,
    timeout: float = DEFAULT_FRAME_EXTRACTION_TIMEOUT,
    fallback_seconds: int = 15,
) -> Tuple[bool, str]:
    """Extract a representative artwork frame from video stream using ffmpeg.

    Applies fast input seeking first (offset_seconds, e.g. 60s), falling back to
    sequential output seeking (fallback_seconds, e.g. 15s) if seeking fails.
    If the extracted frame is still black, attempts offset_seconds + 30s.
    """
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    def _run_ffmpeg(args: list[str]) -> bool:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"] + args
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
            return res.returncode == 0
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg timed out extracting frame from %s", stream_url)
            return False
        except Exception as err:
            logger.warning("ffmpeg execution failed: %s", err)
            return False

    with tempfile.NamedTemporaryFile(
        prefix=".artwork-gen-",
        suffix=".jpg",
        dir=dest.parent,
        delete=False,
    ) as tmp_file:
        tmp_path = Path(tmp_file.name)

    try:
        scale_filter = "scale='min(640,iw)':-2"
        # Step 1: Fast input seeking (-ss before -i) to offset_seconds
        success = _run_ffmpeg([
            "-ss", str(offset_seconds),
            "-i", stream_url,
            "-frames:v", "1",
            "-update", "1",
            "-vf", scale_filter,
            "-q:v", "3",
            str(tmp_path),
        ])

        # Step 2: Fallback for non-seekable streams (-i before -ss)
        if not success or not tmp_path.exists() or tmp_path.stat().st_size == 0:
            logger.info("Fast seek failed for %s, trying sequential fallback at %ss", stream_url, fallback_seconds)
            success = _run_ffmpeg([
                "-i", stream_url,
                "-ss", str(fallback_seconds),
                "-frames:v", "1",
                "-update", "1",
                "-vf", scale_filter,
                "-q:v", "3",
                str(tmp_path),
            ])

        if not success or not tmp_path.exists() or tmp_path.stat().st_size == 0:
            return False, "ffmpeg failed to extract a video frame from stream."

        # Step 3: Check if frame is black; if so, retry with offset + 30s
        if is_black_image(tmp_path):
            alt_offset = offset_seconds + 30
            logger.info("Frame at %ss was black, trying %ss", offset_seconds, alt_offset)
            alt_success = _run_ffmpeg([
                "-ss", str(alt_offset),
                "-i", stream_url,
                "-frames:v", "1",
                "-update", "1",
                "-vf", scale_filter,
                "-q:v", "3",
                str(tmp_path),
            ])
            if alt_success and tmp_path.exists() and not is_black_image(tmp_path):
                success = True

        # Ensure file permissions allow reading by all containers and host
        try:
            os.chmod(str(tmp_path), 0o666)
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "extract_video_frame", exc_info=True)

        # Atomically replace destination file
        shutil.move(str(tmp_path), str(dest))
        # Remove any failure marker next to dest
        dest.with_suffix(".thumb_fail").unlink(missing_ok=True)
        return True, ""
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return False, str(exc)
