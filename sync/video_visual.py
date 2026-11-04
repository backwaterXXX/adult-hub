"""Local, source-versioned visual sequence comparisons for Telegram videos."""
from __future__ import annotations

import hashlib
from io import BytesIO

ALGORITHM_VERSION = "visual-sequence-v1"
CONFIRMATION_POLICY_VERSION = "strict-5-of-8-v2"


def source_key(video: dict) -> str:
    parts = (str(video.get("id") or ""), str(video.get("duration") or ""),
             str(video.get("file_size_bytes") or ""), str(video.get("oshash") or ""))
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _distance(left: str, right: str) -> int:
    try:
        return (int(left, 16) ^ int(right, 16)).bit_count()
    except (ValueError, TypeError):
        return 64


def screen_signatures(left: dict, right: dict) -> bool:
    """Require several informative near-matches spread over the timeline."""
    a = [x for x in (left or {}).get("samples", []) if x.get("valid")]
    b = [x for x in (right or {}).get("samples", []) if x.get("valid")]
    if len(a) < 8 or len(b) < 8:
        return False
    hits = []
    for x in a:
        if not b:
            break
        y = min(b, key=lambda item: abs(float(item["t"]) / max(1, float((left or {}).get("duration", 1)))
                                       - float(item["t"]) / max(1, float((right or {}).get("duration", 1)))))
        if _distance(x.get("phash"), y.get("phash")) <= 12 and _distance(x.get("dhash"), y.get("dhash")) <= 10:
            hits.append(float(x["t"]) / max(1, float((left or {}).get("duration", 1))))
    return len(hits) >= 5 and any(t < .34 for t in hits) and any(.33 <= t <= .67 for t in hits) and any(t > .66 for t in hits)


def compare_signatures(left: dict, right: dict) -> tuple[str, dict]:
    """Conservative matching with normalized timeline alignment and offsets."""
    a = [x for x in (left or {}).get("samples", []) if x.get("valid")]
    b = [x for x in (right or {}).get("samples", []) if x.get("valid")]
    if len(a) < 8 or len(b) < 8:
        return "inconclusive", {"reason": "insufficient_informative_frames"}
    da = float((left or {}).get("duration", 0) or 0)
    db = float((right or {}).get("duration", 0) or 0)
    if da <= 0 or db <= 0:
        return "inconclusive", {"reason": "missing_duration"}
    matches, all_distances = [], []
    for x in a:
        normalized = float(x["t"]) / da
        y = min(b, key=lambda item: abs(float(item["t"]) / db - normalized))
        pd, dd = _distance(x.get("phash"), y.get("phash")), _distance(x.get("dhash"), y.get("dhash"))
        all_distances.append((pd, dd))
        if pd <= 6 and dd <= 6:
            matches.append({"left_t": x["t"], "right_t": y["t"], "phash": pd, "dhash": dd})
    loose = sum(pd <= 12 and dd <= 10 for pd, dd in all_distances)
    sections = {"beginning": False, "middle": False, "end": False}
    for match in matches:
        t = float(match["left_t"]) / da
        sections["beginning" if t < .34 else "end" if t > .66 else "middle"] = True
    median_dhash = sorted(x["dhash"] for x in matches)[len(matches)//2] if matches else 64
    evidence = {"matches": matches, "informative_frames": len(a), "loose_matches": loose,
                "median_dhash": median_dhash, "sections": sections,
                "algorithm_version": ALGORITHM_VERSION,
                "note": "sequence_hash_screen; exact frame SSIM verification required"}
    strict_matches = [item for item in matches if item["phash"] <= 4 and item["dhash"] <= 4]
    if len(strict_matches) >= 8 and all(sections.values()) and median_dhash <= 2:
        evidence["verification_method"] = "25-frame sequence hashes"
        return "confirmed", evidence
    if len(matches) >= 7 and all(sections.values()) and median_dhash <= 4:
        return "review", evidence
    if len(a) >= 8 and loose <= 2:
        return "different", evidence
    return "inconclusive", evidence


def _ssim(left, right) -> float:
    import numpy as np
    a = np.frombuffer(left, dtype=np.uint8).astype(np.float64)
    b = np.frombuffer(right, dtype=np.uint8).astype(np.float64)
    if a.size != 160 * 90 or b.size != a.size:
        return 0.0
    mean_a, mean_b = float(a.mean()), float(b.mean())
    var_a, var_b = float(a.var()), float(b.var())
    cov = float(((a - mean_a) * (b - mean_b)).mean())
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return ((2 * mean_a * mean_b + c1) * (2 * cov + c2)
            / ((mean_a ** 2 + mean_b ** 2 + c1) * (var_a + var_b + c2)))


def _frame_hashes(frame: bytes) -> tuple[str, str, bool]:
    import numpy as np
    from PIL import Image
    from common.image_utils import compute_dhash
    from stash_phash.phash import perception_hash
    image = Image.frombytes("L", (160, 90), frame)
    values = np.frombuffer(frame, dtype=np.uint8)
    histogram = np.bincount(values, minlength=256) / max(1, len(values))
    entropy = -float(sum(p * np.log2(p) for p in histogram if p > 0))
    informative = float(values.std()) >= 12 and entropy >= 4
    return f"{perception_hash(image):016x}", compute_dhash(image), informative


def reevaluate_evidence(evidence: dict) -> str | None:
    """Apply the current policy to saved measurements without decoding frames."""
    if not isinstance(evidence, dict) or len(evidence.get("matches", [])) != 8:
        return None
    try:
        matches = evidence["matches"]
        strict = [x for x in matches if x["informative"] and x["phash"] <= 6 and x["dhash"] <= 6 and x["ssim"] >= .90]
        loose = [x for x in matches if x["informative"] and x["phash"] <= 12 and x["dhash"] <= 10 and x["ssim"] >= .85]
        sections = evidence["strict_sections"]
        fit = evidence["offset_fit"]
        ssims = sorted(x["ssim"] for x in strict)
        dhashes = sorted(x["dhash"] for x in strict)
        if (len(strict) >= 5 and len(loose) >= 6 and all(sections.get(x) for x in ("beginning", "middle", "end"))
                and ssims[len(ssims)//2] >= .90 and dhashes[len(dhashes)//2] <= 6
                and fit["max_residual_seconds"] <= .5 and fit["predicted"]
                and all(-2 <= x <= 2 for x in fit["predicted"]) and evidence["aspect_ratio_auto_safe"]):
            return "confirmed"
        return "review" if len(loose) >= 5 and all(evidence["sections"].values()) else (
            "different" if sum(bool(x["informative"]) for x in matches) >= 6 and len(loose) <= 2 else "inconclusive")
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def compare_pair(left_id: str, right_id: str, left_duration: float, right_duration: float,
                 *, left_aspect: float | None = None, right_aspect: float | None = None,
                 cache_dir: str | None = None, byte_budget: int = 256 * 1024 * 1024) -> tuple[str, dict, bytes | None]:
    """Fine-verify a candidate with 8 anchors, a +/-2 s search and SSIM."""
    # Import here so missing optional dependencies fail before any frame is fetched.
    import numpy as np  # noqa: F401
    from PIL import Image, ImageDraw  # noqa: F401
    if left_duration <= 0 or right_duration <= 0:
        return "inconclusive", {"reason": "missing_duration"}, None
    left_anchors = [left_duration * (0.10 + 0.80 * i / 7) for i in range(8)]
    coarse_offsets = [round(-2 + .25 * i, 2) for i in range(17)]
    coarse_indices = (0, 2, 5, 7)
    coarse_right_times = sorted({round(left_anchors[i] + delta, 3)
                                 for delta in coarse_offsets for i in coarse_indices
                                 if 0 <= left_anchors[i] + delta < right_duration})
    cache_base = "visual-" + _identity(left_id, right_id)

    left_frames = _extract_telegram_frames(left_id, [round(t, 3) for t in left_anchors], cache_base + "-L",
                                           byte_budget, cache_dir)
    coarse_frames = _extract_telegram_frames(right_id, coarse_right_times, cache_base + "-R",
                                             byte_budget, cache_dir)
    coarse_by_time = dict(zip(coarse_right_times, coarse_frames))
    best_coarse = _best_coarse_offset(left_frames, left_anchors, coarse_offsets, coarse_indices, coarse_by_time)
    fine_offsets = [round(best_coarse - .25 + .05 * i, 2) for i in range(11)
                    if -2 <= best_coarse - .25 + .05 * i <= 2]
    fine_right_times = sorted({round(left_anchors[i] + delta, 3)
                               for delta in fine_offsets for i in range(8)
                               if 0 <= left_anchors[i] + delta < right_duration})
    fine_frames = _extract_telegram_frames(right_id, fine_right_times, cache_base + "-R", byte_budget, cache_dir)
    fine_by_time = dict(zip(fine_right_times, fine_frames))
    anchor_results, local_offsets = _anchor_matches(left_frames, left_anchors, fine_offsets, fine_by_time)
    informative = [x for x in anchor_results if x["informative"]]
    strict = [x for x in anchor_results if x["match"]]
    loose = [x for x in anchor_results if x["loose"]]
    thirds, strict_thirds = _section_coverage(loose, left_duration)
    slope, intercept, residual, predicted = _offset_fit(local_offsets, left_anchors)
    matched_ssim = sorted(x["ssim"] for x in strict)
    median_ssim = matched_ssim[len(matched_ssim)//2] if matched_ssim else 0.0
    matched_dhash = sorted(x["dhash"] for x in strict)
    median_dhash = matched_dhash[len(matched_dhash)//2] if matched_dhash else 64
    aspect_ok = not (left_aspect and right_aspect and abs(left_aspect / right_aspect - 1) > .03)
    evidence = {"algorithm_version": ALGORITHM_VERSION,
                "confirmation_policy": CONFIRMATION_POLICY_VERSION,
                "verification_method": "8-anchor +/-2s SSIM",
                "matches": [{key: value for key, value in item.items() if key not in {"left_frame", "right_frame"}}
                            for item in anchor_results],
                "informative_frames": len(informative), "strict_matches": len(strict),
                "loose_matches": len(loose), "sections": thirds, "strict_sections": strict_thirds,
                "median_ssim": round(median_ssim, 4), "median_dhash": median_dhash,
                "offset_fit": {"slope": round(float(slope), 6) if len(local_offsets) >= 2 else None,
                               "intercept": round(float(intercept), 4) if len(local_offsets) >= 2 else None,
                               "max_residual_seconds": round(residual, 4), "predicted": predicted},
                "aspect_ratio_auto_safe": aspect_ok, "coarse_offset_seconds": best_coarse}
    if (len(strict) >= 5 and len(loose) >= 6 and all(strict_thirds.values())
            and median_dhash <= 6 and median_ssim >= .90 and residual <= .5
            and predicted and all(-2 <= value <= 2 for value in predicted) and aspect_ok):
        evidence["confirmation_rule"] = "five_strict_six_loose"
        state = "confirmed"
    elif len(loose) >= 5 and all(thirds.values()):
        state = "review"
    elif len(informative) >= 6 and len(loose) <= 2:
        state = "different"
    else:
        state = "inconclusive"
    sheet = _contact_sheet(anchor_results)
    return state, evidence, sheet


def _extract_telegram_frames(video_id: str, times: list[float], cache_id: str,
                             byte_budget: int, cache_dir: str | None) -> list[bytes]:
    try:
        from resolver_video_fingerprints import telegram_frame_samples
    except ImportError:
        from video_fingerprints import telegram_frame_samples
    return telegram_frame_samples(video_id, times, byte_budget=byte_budget,
                                  timeout_seconds=900, resolver_url="http://127.0.0.1:8090/admin",
                                  cache_dir=cache_dir, cache_id=cache_id)


def _best_coarse_offset(left_frames, left_anchors, coarse_offsets, coarse_indices, coarse_by_time) -> float:
    coarse_scores = []
    for delta in coarse_offsets:
        scores = [_ssim(left_frames[i], coarse_by_time[round(left_anchors[i] + delta, 3)])
                  for i in coarse_indices if round(left_anchors[i] + delta, 3) in coarse_by_time]
        coarse_scores.append((sum(scores) / len(scores) if len(scores) == len(coarse_indices) else -1, delta))
    return max(coarse_scores, key=lambda value: (value[0], -abs(value[1])))[1]


def _anchor_matches(left_frames, left_anchors, fine_offsets, fine_by_time) -> tuple[list, list]:
    """Score each left anchor against its best fine-offset frame. Returns (results, local offsets)."""
    anchor_results = []
    local_offsets = []
    for i, left_frame in enumerate(left_frames):
        choices = [(delta, fine_by_time[round(left_anchors[i] + delta, 3)])
                   for delta in fine_offsets if round(left_anchors[i] + delta, 3) in fine_by_time]
        if not choices:
            continue
        left_phash, left_dhash, left_valid = _frame_hashes(left_frame)
        scored = []
        for delta, right_frame in choices:
            right_phash, right_dhash, right_valid = _frame_hashes(right_frame)
            score = _ssim(left_frame, right_frame) if left_valid and right_valid else 0.0
            scored.append((score, delta, right_frame, right_phash, right_dhash, right_valid))
        score, delta, right_frame, right_phash, right_dhash, right_valid = max(scored, key=lambda x: (x[0], -abs(x[1])))
        pd, dd = _distance(left_phash, right_phash), _distance(left_dhash, right_dhash)
        valid = left_valid and right_valid
        anchor_results.append({"left_t": round(left_anchors[i], 3),
                               "right_t": round(left_anchors[i] + delta, 3),
                               "offset": delta, "phash": pd, "dhash": dd,
                               "ssim": round(score, 4), "informative": valid,
                               "match": bool(valid and pd <= 6 and dd <= 6 and score >= .90),
                               "loose": bool(valid and pd <= 12 and dd <= 10 and score >= .85),
                               "left_frame": left_frame, "right_frame": right_frame})
        if valid:
            local_offsets.append((left_anchors[i], delta))
    return anchor_results, local_offsets


def _section_coverage(loose: list, left_duration: float) -> tuple[dict, dict]:
    """Which thirds of the video have a loose match, and which have a strict one."""
    thirds = {"beginning": False, "middle": False, "end": False}
    strict_thirds = {"beginning": False, "middle": False, "end": False}
    for item in loose:
        position = item["left_t"] / left_duration
        section = "beginning" if position < 1/3 else "end" if position > 2/3 else "middle"
        thirds[section] = True
        if item["match"]:
            strict_thirds[section] = True
    return thirds, strict_thirds


def _offset_fit(local_offsets: list, left_anchors: list) -> tuple:
    """Fit offset = slope * time + intercept. Returns (slope, intercept, max residual, predicted offsets)."""
    import numpy as np
    if len(local_offsets) >= 2:
        x = np.array([v[0] for v in local_offsets], dtype=np.float64)
        y = np.array([v[1] for v in local_offsets], dtype=np.float64)
        slope, intercept = np.polyfit(x, y, 1)
        residual = float(np.max(np.abs(y - (slope * x + intercept))))
        predicted = [float(slope * t + intercept) for t in left_anchors]
        return slope, intercept, residual, predicted
    return None, None, 99.0, []


def _contact_sheet(anchor_results: list) -> bytes | None:
    """JPEG grid of up to four anchor pairs for manual review, or None if too large."""
    from PIL import Image, ImageDraw
    if not anchor_results:
        return None
    picks = [anchor_results[i] for i in (0, 2, 5, 7) if i < len(anchor_results)]
    tile_w, tile_h = 320, 180
    canvas = Image.new("RGB", (tile_w * 2, (tile_h + 26) * len(picks)), "#10151d")
    draw = ImageDraw.Draw(canvas)
    for row, item in enumerate(picks):
        y = row * (tile_h + 26)
        draw.text((4, y + 3), f"A {item['left_t']:.2f}s  B {item['right_t']:.2f}s  SSIM {item['ssim']:.3f}", fill="white")
        canvas.paste(Image.frombytes("L", (160, 90), item["left_frame"]).convert("RGB").resize((tile_w, tile_h)), (0, y + 26))
        canvas.paste(Image.frombytes("L", (160, 90), item["right_frame"]).convert("RGB").resize((tile_w, tile_h)), (tile_w, y + 26))
    output = BytesIO()
    canvas.save(output, format="JPEG", quality=68, optimize=True)
    if len(output.getvalue()) <= 2_000_000:
        return output.getvalue()
    return None


def _identity(left_id: str, right_id: str) -> str:
    return hashlib.sha256((left_id + "\0" + right_id).encode()).hexdigest()[:20]
