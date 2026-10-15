"""Scene title normalization and confidence scoring."""
from __future__ import annotations

import difflib
import re
from typing import List, Optional


def _clean_query(text: str, studio: str = "") -> str:
    """Clean video title / caption for search queries (Pre-search Sanitizer)."""
    if not text:
        return ""
    t = text
    # 1. Remove URLs, Telegram links and mentions
    t = re.sub(r"https?://\S+|t\.me/\S+|@[\w\.-]+", " ", t)
    # 2. Remove emojis and special unicode symbols (emojis, dingbats, misc symbols)
    t = re.sub(r"[\U00010000-\U0010ffff]|[\u2600-\u27bf]|[\u2300-\u23ff]|[\u2b50-\u2b55]", " ", t)
    # 3. Remove leading/trailing bracketed tags e.g. [Men.com], (Falcon), #tag
    t = re.sub(r"\[.*?\]|\(.*?\)", " ", t)
    t = re.sub(r"#\w+", " ", t)
    # 4. Remove studio name if explicitly passed or known studio tokens
    if studio:
        s_esc = re.escape(studio.strip())
        t = re.sub(rf"(?i)\b{s_esc}\b", " ", t)
    t = re.sub(
        r"(?i)\b(falcon(?:\s*studios?)?|raging\s*stallion|hot\s*house|next\s*door(?:\s*studios?)?|nextdoor|"
        r"sean\s*cody|men\.com|helix(?:\s*studios?)?|belami(?:online)?|lucas(?:\s*entertainment)?)\b",
        " ",
        t,
    )
    # 5. Remove video quality, codec, format and tech tokens
    t = re.sub(
        r"(?i)\b(2160p|1080p|720p|480p|360p|4k|uhd|fhd|hd|sd|hevc|x264|x265|h264|h265|10bit|60fps|fps|aac|mp4|mkv|avi|wmv)\b",
        " ",
        t,
    )
    # 6. Remove common marketing, aggregator, and generic genre words
    t = re.sub(
        r"(?i)\b(full\s*video|full\s*movie|bonus\s*video|behind\s*the\s*scenes|uncut|exclusive|download|leak|free|xxx|porn|solo|duo|threesome|bareback)\b",
        " ",
        t,
    )
    # 7. Clean punctuation and separators
    t = re.sub(r"[-–—_.:|/\\+~@,;!?*]", " ", t)
    # 8. Collapse whitespace and strip
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _calculate_match_confidence(
    query_title: str,
    target_title: str,
    query_duration: int = 0,
    target_duration: int = 0,
    query_performers: Optional[List[str]] = None,
    target_performers: Optional[List[str]] = None,
) -> float:
    """Calculate confidence score (0.0 to 1.0) between source and scraped scene."""
    if not target_title:
        return 0.0

    q_title_norm = _clean_query(query_title).lower()
    t_title_norm = _clean_query(target_title).lower()

    # 1. Title similarity
    title_ratio = difflib.SequenceMatcher(None, q_title_norm, t_title_norm).ratio()

    # Check substring inclusion: if target title is entirely within query title
    if t_title_norm and t_title_norm in q_title_norm:
        title_ratio = max(title_ratio, 0.85)

    # 2. Duration similarity
    duration_score = 0.5  # neutral default if unknown
    has_duration = query_duration > 0 and target_duration > 0
    if has_duration:
        diff = abs(query_duration - target_duration)
        if diff <= 60:
            duration_score = 1.0
        elif diff <= 120:
            duration_score = 0.8
        elif diff <= 240:
            duration_score = 0.6
        elif diff <= 480:
            duration_score = 0.3
        else:
            duration_score = 0.0  # Big mismatch penalty

    # 3. Performer overlap
    q_perfs = {p.strip().lower() for p in (query_performers or []) if p.strip()}
    t_perfs = {p.strip().lower() for p in (target_performers or []) if p.strip()}
    performer_score = 0.5  # neutral default
    if q_perfs and t_perfs:
        common = q_perfs & t_perfs
        if common == q_perfs or common == t_perfs:
            performer_score = 1.0
        elif len(common) > 0:
            performer_score = 0.8
        else:
            performer_score = 0.2

    # Weighted composite score
    if has_duration and q_perfs and t_perfs:
        total = 0.40 * title_ratio + 0.35 * duration_score + 0.25 * performer_score
    elif has_duration:
        total = 0.55 * title_ratio + 0.45 * duration_score
    elif q_perfs and t_perfs:
        total = 0.60 * title_ratio + 0.40 * performer_score
    else:
        total = title_ratio

    return round(total, 3)
