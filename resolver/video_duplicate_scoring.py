"""Pure candidate normalization and scoring rules for video duplicates."""

from __future__ import annotations

import difflib
import itertools
import json
import re
import unicodedata
from collections import defaultdict
from typing import Any

from common.performers import TITLE_STOPWORDS

RULES_VERSION = 10
MIN_SCORE = 70.0
EPISODE_PART_SCORE_PENALTY = 20.0
MAX_HEURISTIC_CANDIDATES = 100_000
PHASH_NEAR_MAX_DISTANCE = 10
PHASH_NEAR_SOLO_TITLE_SIMILARITY = 0.90
PHASH_NEAR_SOLO_MAX_DURATION_DELTA = 1


def _normalize_name(value: str) -> str:
    value = value.translate(str.maketrans({"ł": "l", "Ł": "l", "đ": "d", "Đ": "d"}))
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))

def _names(value: Any, aliases: dict[str, str] | None = None) -> tuple[set[str], dict[str, str]]:
    try:
        values = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return set(), {}
    if not isinstance(values, list):
        return set(), {}
    normalized: dict[str, str] = {}
    for item in values:
        if not isinstance(item, str) or not item.strip():
            continue
        key = _normalize_name(item)
        if key:
            canonical = (aliases or {}).get(key, key)
            normalized.setdefault(canonical, item.strip())
    return set(normalized), normalized

def _telegram_file_id(row: dict) -> str | None:
    """Validate Telegram's provider_video_id and return its opaque last field."""
    if row.get("provider") != "telegram":
        return None
    video_id = str(row.get("id") or "")
    ref = video_id[len("telegram:"):] if video_id.startswith("telegram:") else ""
    parts = ref.rsplit(":", 2)
    if len(parts) != 3 or not parts[0] or not parts[2]:
        return None
    if not parts[1].isascii() or not parts[1].isdigit():
        return None
    # Slug is checked only as a diagnostic below, never as identity evidence.
    return parts[2]

def _duration_values(row: dict) -> int | None:
    try:
        value = int(row.get("duration"))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value > 0 else None

def _score(left: dict, right: dict, shared: set[str]) -> dict | None:
    a, b = _duration_values(left), _duration_values(right)
    if a is None or b is None or len(shared) < 2:
        return None
    a_cast, b_cast = left["performer_norm"], right["performer_norm"]
    union = a_cast | b_cast
    jaccard = len(shared) / len(union) if union else 0.0
    if jaccard < 0.6:
        return None
    delta = abs(a - b)
    tolerance = min(15.0, max(5.0, 0.01 * max(a, b)))
    if delta > tolerance:
        return None
    d_score = 1.0 - 0.5 * delta / tolerance
    k = min(len(shared) / 3.0, 1.0)
    cast_points, duration_points, count_points = 55 * jaccard, 30 * d_score, 10 * k
    raw_total = cast_points + duration_points + count_points
    penalty = EPISODE_PART_SCORE_PENALTY if _has_different_season_markers(
        left.get("title", ""), right.get("title", "")) else 0.0
    total = raw_total - penalty
    category = "Strong suggestion" if total >= 85 else "Needs review"
    return {
        "score": total, "score_max": 95, "category": category,
        "rules_version": RULES_VERSION,
        "components": {
            "cast_jaccard": jaccard, "duration_delta": delta,
            "duration_tolerance": tolerance, "shared_performers": len(shared),
            "cast_points": cast_points, "duration_points": duration_points,
            "performer_count_points": count_points,
            "episode_part_penalty": penalty,
        },
    }

def _clean_title_for_solo(title: str) -> str:
    if not title:
        return ""
    t = unicodedata.normalize("NFKD", str(title).casefold())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())

def _title_similarity(t1: str, t2: str) -> float:
    c1, c2 = _clean_title_for_solo(t1), _clean_title_for_solo(t2)
    if not c1 or not c2:
        return 0.0
    if c1 == c2:
        return 1.0
    return difflib.SequenceMatcher(None, c1, c2).ratio()

def _has_different_season_markers(t1: str, t2: str) -> bool:
    """Detect a matching title whose different S<number> markers identify distinct parts."""
    def split_marker(title: str) -> tuple[str, int] | None:
        clean = _clean_title_for_solo(title)
        match = re.search(r"(?<!\w)s(\d{1,2})(?!\w)", clean)
        if not match:
            return None
        base = " ".join((clean[:match.start()] + " " + clean[match.end():]).split())
        return base, int(match.group(1))

    left, right = split_marker(t1), split_marker(t2)
    return bool(
        left and right
        and left[1] != right[1]
        and left[0] and right[0]
        and _title_similarity(left[0], right[0]) >= 0.8
    )

def _score_solo(left: dict, right: dict) -> dict | None:
    a, b = _duration_values(left), _duration_values(right)
    if a is None or b is None:
        return None
    a_cast, b_cast = left["performer_norm"], right["performer_norm"]
    if len(a_cast) != 1 or len(b_cast) != 1 or a_cast != b_cast:
        return None

    delta = abs(a - b)
    tolerance = min(15.0, max(5.0, 0.01 * max(a, b)))
    if delta > tolerance:
        return None

    clean1 = _clean_title_for_solo(left.get("title", ""))
    clean2 = _clean_title_for_solo(right.get("title", ""))
    words1 = [w for w in clean1.split() if len(w) >= 3 and w not in TITLE_STOPWORDS]
    words2 = [w for w in clean2.split() if len(w) >= 3 and w not in TITLE_STOPWORDS]
    if len(words1) < 2 or len(words2) < 2:
        return None

    sim = _title_similarity(left.get("title", ""), right.get("title", ""))
    if sim < 0.55:
        return None

    d_score = 1.0 - 0.5 * delta / tolerance
    title_points = 55.0 * sim
    duration_points = 30.0 * d_score
    actor_points = 10.0
    raw_total = title_points + duration_points + actor_points
    penalty = EPISODE_PART_SCORE_PENALTY if _has_different_season_markers(
        left.get("title", ""), right.get("title", "")) else 0.0
    total = raw_total - penalty

    category = "Strong suggestion" if total >= 85 else "Needs review"
    actor_name = next(iter(left["performer_display"].values()), next(iter(a_cast)))
    reasons = [
        f"Matching solo performer ({actor_name}) and similar duration",
        f"Title similarity: {sim:.0%}",
        f"Duration differs by {delta} s",
    ]
    if penalty:
        reasons.append("Different S-number parts may be different episodes; match confidence reduced")

    return {
        "score": round(total, 1),
        "score_max": 95,
        "category": category,
        "rules_version": RULES_VERSION,
        "reasons": reasons,
        "evidence": "solo_title_duration",
        "components": {
            "title_similarity": round(sim, 2),
            "duration_delta": delta,
            "duration_tolerance": tolerance,
            "shared_performers": 1,
            "title_points": round(title_points, 1),
            "duration_points": round(duration_points, 1),
            "performer_count_points": actor_points,
            "episode_part_penalty": penalty,
        },
    }

def _prepare(rows: list[dict], aliases: dict[str, str] | None = None) -> None:
    for row in rows:
        row["performer_norm"], row["performer_display"] = _names(row.get("performers"), aliases)

def _candidate_pairs(rows: list[dict]) -> tuple[set[tuple[int, int]], bool]:
    """Index shared actor pairs and sweep adjacent duration windows."""
    by_cast_pair: dict[tuple[str, str], list[int]] = defaultdict(list)
    by_single_actor: dict[str, list[int]] = defaultdict(list)

    for i, row in enumerate(rows):
        duration = _duration_values(row)
        if duration is None:
            continue
        p_len = len(row["performer_norm"])
        if p_len >= 2:
            for pair in itertools.combinations(sorted(row["performer_norm"]), 2):
                by_cast_pair[pair].append(i)
        elif p_len == 1:
            actor = next(iter(row["performer_norm"]))
            by_single_actor[actor].append(i)

    pairs: set[tuple[int, int]] = set()

    def _sweep_groups(groups_dict: dict):
        nonlocal pairs
        for indexes in groups_dict.values():
            if len(indexes) < 2:
                continue
            indexes.sort(key=lambda idx: (int(rows[idx]["duration"]), str(rows[idx]["id"])))
            for offset, left in enumerate(indexes):
                left_duration = int(rows[left]["duration"])
                for right in indexes[offset + 1:]:
                    if int(rows[right]["duration"]) - left_duration > 15:
                        break
                    pair = (min(left, right), max(left, right))
                    pairs.add(pair)
                    if len(pairs) >= MAX_HEURISTIC_CANDIDATES:
                        return False
        return True

    if not _sweep_groups(by_cast_pair):
        return pairs, False
    if not _sweep_groups(by_single_actor):
        return pairs, False

    return pairs, True

def _index_dismissals(dismissed: set[tuple[str, str]]) -> dict[str, set[tuple[str, str]]]:
    """Index dismissed pairs by member so same-file groups avoid rescanning all decisions."""
    by_video: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for pair in dismissed:
        by_video[pair[0]].add(pair)
        by_video[pair[1]].add(pair)
    return by_video

def _dismissals_for_group(members: set[str], by_video: dict[str, set[tuple[str, str]]]) -> set[tuple[str, str]]:
    """Return only dismissals wholly contained in one same-file group."""
    return {
        pair
        for member in members
        for pair in by_video.get(member, ())
        if pair[0] in members and pair[1] in members
    }
