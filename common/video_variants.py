"""Optimal quality variant selection for pHash, artwork, and background processing.

Implements selection and ranking of Telegram alternative documents (video variants)
for bandwidth-saving perceptual hashing and artwork/thumbnail generation.
Prefers ~480p AV01 transcodes, followed by 360p or 720p alternatives, before
falling back to the full-size original stream.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger("common.video_variants")

# Codec priority scores: AV1 delivers the smallest file sizes and fastest seeks
CODEC_SCORES = {
    "av01": 200,
    "av1": 200,
    "hvc1": 100,
    "hevc": 100,
    "h265": 100,
    "avc1": 50,
    "h264": 50,
}


def score_variant(variant: dict, *, purpose: str = "general") -> int:
    """Calculate a suitability score for a video variant.

    Higher score indicates a more suitable variant for pHash/artwork extraction.
    Returns -1 if the variant is disqualified (e.g. invalid dimensions).
    """
    if not isinstance(variant, dict):
        return -1

    # Original stream has baseline score 0; alternatives get positive scores
    is_original = bool(variant.get("is_original"))
    if is_original:
        return 0

    height = variant.get("height")
    width = variant.get("width")
    if not isinstance(height, int) or height <= 0:
        return -1
    if not isinstance(width, int) or width <= 0:
        return -1

    # For artwork thumbnails (which require minimum dimensions of 180x320 and target 640px wide),
    # ensure height >= 240 (typically >= 360p for full 640px 16:9 width).
    if purpose in ("artwork", "thumbnail") and height < 240:
        return -1

    # Resolution score:
    # 480p (height around 480) is the sweet spot for visual quality and minimal bandwidth
    if 400 <= height <= 540:
        res_score = 500
    elif 320 <= height < 400:  # 360p
        res_score = 400
    elif 540 < height <= 720:  # 720p
        res_score = 300
    elif height > 720:         # 1080p alternative (still smaller than original uncompressed)
        res_score = 100
    else:                      # < 320p
        res_score = 50

    # Codec bonus
    codec = str(variant.get("codec") or "").strip().lower()
    codec_score = 0
    for prefix, score in CODEC_SCORES.items():
        if codec.startswith(prefix):
            codec_score = score
            break

    return res_score + codec_score


def select_optimal_variant(variants: list[dict], *, purpose: str = "general") -> Optional[dict]:
    """Select the best alternative variant from a list of variants.

    Returns the selected variant dict, or None if no alternative variant is suitable.
    """
    if not variants or not isinstance(variants, list):
        return None

    candidates: list[tuple[int, int, dict]] = []
    for item in variants:
        if not isinstance(item, dict) or item.get("is_original"):
            continue
        score = score_variant(item, purpose=purpose)
        if score > 0:
            # Tie-breaker: smaller file_size / estimated_average_bitrate is preferred
            size = item.get("file_size") or item.get("estimated_average_bitrate") or 999_999_999_999
            candidates.append((score, -int(size), item))

    if not candidates:
        return None

    # Sort descending by score, then descending by negative size (i.e. ascending by size)
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    return candidates[0][2]


def fetch_and_select_best_variant_with_meta(
    base_url: str,
    video_id: str,
    *,
    purpose: str = "general",
    http_client: Any = None,
    timeout: float = 6.0,
) -> tuple[Optional[str], Optional[int]]:
    """Query resolver variants API and return (best_variant_id, original_file_size).

    If variants are unavailable, disabled, processing, or fail, returns (None, None).
    """
    base = base_url.rstrip("/")
    url = f"{base}/play/{quote(video_id, safe=':-_')}/variants"
    client = http_client or httpx

    try:
        if hasattr(client, "get"):
            response = client.get(url, timeout=timeout)
        else:
            with client.Client(timeout=timeout) as c:
                response = c.get(url)

        if response.status_code != 200:
            logger.debug("Variants query returned status %s for %s", response.status_code, video_id)
            return None, None

        payload = response.json()
        if not isinstance(payload, dict):
            return None, None

        variants = payload.get("variants")
        if not isinstance(variants, list):
            return None, None

        # Look up original file size
        orig_size = None
        for item in variants:
            if isinstance(item, dict) and item.get("is_original"):
                raw_size = item.get("file_size")
                if isinstance(raw_size, int) and raw_size > 0:
                    orig_size = raw_size
                break

        # Check if alternative documents are ready and available
        if not payload.get("has_alternatives") or payload.get("status") not in ("ready", "processing"):
            return None, orig_size

        best = select_optimal_variant(variants, purpose=purpose)
        if best is not None:
            best_id = str(best.get("id") or "").strip()
            if best_id and best_id.lower() != "original":
                logger.info(
                    "Selected variant '%s' (%sx%s %s) for %s [purpose=%s]",
                    best_id, best.get("width"), best.get("height"), best.get("codec"),
                    video_id, purpose,
                )
                return best_id, orig_size

        return None, orig_size
    except Exception as exc:
        logger.debug("Failed to query variants for %s: %s", video_id, exc)
        return None, None


def fetch_and_select_best_variant(
    base_url: str,
    video_id: str,
    *,
    purpose: str = "general",
    http_client: Any = None,
    timeout: float = 6.0,
) -> Optional[str]:
    """Convenience helper returning only the best_variant_id (or None)."""
    best_id, _ = fetch_and_select_best_variant_with_meta(
        base_url, video_id, purpose=purpose, http_client=http_client, timeout=timeout,
    )
    return best_id
