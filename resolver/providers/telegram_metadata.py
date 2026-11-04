"""Telegram caption and performer metadata parsing helpers."""
import os
import re
from typing import Dict, List, Optional, Tuple

from common.performers import is_valid_performer_name, prune_redundant_performers
import logging

logger = logging.getLogger(__name__)


_MAX_PERFORMERS_PER_VIDEO = 5

def _hub_db_url() -> Optional[str]:
    """PostgreSQL URL of the hub database (None when unconfigured)."""
    url = (os.getenv("DATABASE_URL") or "").strip()
    return url or None


_PERFORMER_CACHE_TTL_SECONDS = 300.0

_KNOWN_PERFORMERS_CACHE: Dict[str, object] = {"mtime": 0.0, "names": []}
_CATALOG_CACHE: Dict[str, object] = {"mtime": 0.0, "records": []}


def load_performer_catalog() -> List[dict]:
    """Cached local catalogue; legacy names remain available as local entries."""
    import time
    from common import db as _db
    if not _hub_db_url():
        return []
    now = time.time()
    if now - float(_CATALOG_CACHE.get("mtime") or 0) < _PERFORMER_CACHE_TTL_SECONDS and _CATALOG_CACHE.get("records"):
        return list(_CATALOG_CACHE["records"])
    conn = None
    try:
        conn = _db.connect(_hub_db_url())
        from common.performer_catalog import load_catalog
        records = load_catalog(conn)
    except Exception as _exc:
        logger.debug("Ignored exception in %s", "load_performer_catalog", exc_info=True)
        return list(_CATALOG_CACHE.get("records") or [])
    finally:
        if conn is not None:
            conn.close()
    _CATALOG_CACHE.update({"mtime": now, "records": records})
    return list(records)


def load_known_performers() -> List[str]:
    """Dictionary of performers from videos.performers column (JSON lists).

    Cache invalidated by TTL. Missing DB / error -> [] (never raises).
    """
    import json as _json
    from common import db as _db
    if not _hub_db_url():
        return []
    now = __import__("time").time()
    cache = _KNOWN_PERFORMERS_CACHE
    if now - float(cache.get("mtime") or 0.0) < _PERFORMER_CACHE_TTL_SECONDS and cache.get("names"):
        return list(cache["names"])  # type: ignore[arg-type]
    names: List[str] = []
    seen = set()
    try:
        conn = _db.connect(_hub_db_url())
        try:
            rows = conn.execute(
                "SELECT performers FROM videos WHERE performers <> '[]'::jsonb"
            ).fetchall()
        finally:
            conn.close()
    except Exception as _exc:
        logger.debug("Ignored exception in %s", "load_known_performers", exc_info=True)
        return list(cache.get("names") or [])  # type: ignore[arg-type]
    alias_map = load_alias_map()
    for (raw,) in rows:
        try:
            parsed = _json.loads(raw) if isinstance(raw, str) else []
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "load_known_performers", exc_info=True)
            continue
        if not isinstance(parsed, list):
            continue
        for entry in parsed:
            name = str(entry or "").strip()
            if name and is_valid_performer_name(name):
                if alias_map and name.lower() in alias_map:
                    name = alias_map[name.lower()]
                if name.lower() not in seen and is_valid_performer_name(name):
                    seen.add(name.lower())
                    names.append(name)
    cache["mtime"] = __import__("time").time()
    cache["names"] = names
    return list(names)


_ALIAS_MAP_CACHE: Dict[str, object] = {"mtime": 0.0, "entries": {}}


def load_alias_map() -> Dict[str, str]:
    """Map alias.lower() -> canonical name from performer_aliases (PBC).

    Cache invalidated by TTL. Missing table/DB -> {} (never raises).
    """
    from common import db as _db
    if not _hub_db_url():
        return {}
    now = __import__("time").time()
    cache = _ALIAS_MAP_CACHE
    if now - float(cache.get("mtime") or 0.0) < _PERFORMER_CACHE_TTL_SECONDS and cache.get("entries"):
        return dict(cache.get("entries"))  # type: ignore[arg-type]
    entries: Dict[str, str] = {}
    try:
        conn = _db.connect(_hub_db_url())
        try:
            rows = conn.execute(
                "SELECT alias, canonical FROM performer_aliases"
            ).fetchall()
        finally:
            conn.close()
    except Exception as _exc:
        logger.debug("Ignored exception in %s", "load_alias_map", exc_info=True)
        return dict(cache.get("entries") or {})  # type: ignore[arg-type]
    for alias, canonical in rows:
        a = str(alias or "").strip().lower()
        c = str(canonical or "").strip()
        if a and c and is_valid_performer_name(c) and a not in entries:
            entries[a] = c
    cache["mtime"] = now
    cache["entries"] = entries
    return dict(entries)


def match_known_performers(text: str, known_names, aliases=None) -> List[str]:
    """Match performers from dictionary against text (title+caption+filename).

    Case-insensitive, word boundaries (unicode-aware), longest names first,
    up to _MAX_PERFORMERS_PER_VIDEO. Missing dictionary / no matches -> [].
    No guessing: performers not present in database are never added.

    Prevents sub-name collisions (e.g. "James" within "James Cassidy"):
    matched text spans are masked. An isolated single-word pseudonym
    remains a valid match.
    """
    if not text or (not known_names and not aliases):
        return []
    haystack = str(text)
    claimed_spans: List[Tuple[int, int]] = []
    matched: List[str] = []
    seen_lower = set()

    def _is_unclaimed(start: int, end: int) -> bool:
        return not any(c_start < end and start < c_end for c_start, c_end in claimed_spans)

    def _is_isolated_word(start: int, end: int) -> bool:
        if re.match(r"^\s+[A-Z][a-z]+", haystack[end:]):
            return False
        if re.search(r"\b[A-Z][a-z]+\s+$", haystack[:start]):
            return False
        return True

    def _claim_span(start: int, end: int, display: str) -> None:
        claimed_spans.append((start, end))
        if display.lower() not in seen_lower:
            seen_lower.add(display.lower())
            matched.append(display)

    haystack_lower = haystack.lower()

    candidates = []
    for name in (known_names or []):
        clean = str(name or "").strip()
        if len(clean) < 2 or not is_valid_performer_name(clean):
            continue
        clean_lower = clean.lower()
        has_space = " " in clean
        nospace = clean_lower.replace(" ", "") if has_space else ""
        if clean_lower in haystack_lower or (has_space and len(nospace) >= 8 and nospace in haystack_lower):
            candidates.append(clean)
    candidates.sort(key=len, reverse=True)

    for clean in candidates:
        if len(matched) >= _MAX_PERFORMERS_PER_VIDEO:
            break
        has_space = " " in clean
        clean_lower = clean.lower()
        nospace = clean_lower.replace(" ", "") if has_space else ""
        display = (aliases or {}).get(clean_lower, clean)
        try:
            pattern = r"(?<!\w)" + re.escape(clean) + r"(?!\w)"
            found = False
            for m in re.finditer(pattern, haystack, re.IGNORECASE | re.UNICODE):
                s, e = m.start(), m.end()
                if _is_unclaimed(s, e):
                    if not has_space and not _is_isolated_word(s, e):
                        continue
                    _claim_span(s, e, display)
                    found = True
            if not found and has_space and len(nospace) >= 8 and nospace in haystack_lower:
                pattern_ns = r"(?<!\w)" + re.escape(nospace) + r"(?!\w)"
                for m in re.finditer(pattern_ns, haystack, re.IGNORECASE | re.UNICODE):
                    s, e = m.start(), m.end()
                    if _is_unclaimed(s, e):
                        _claim_span(s, e, display)
        except re.error:
            pass

    alias_candidates = []
    for alias_lower, canonical in (aliases or {}).items():
        alias = str(alias_lower or "").strip()
        display = str(canonical or "").strip()
        if len(alias) < 2 or not display or not is_valid_performer_name(display) or display.lower() in seen_lower:
            continue
        a_low = alias.lower()
        has_space = " " in alias
        nospace = a_low.replace(" ", "") if has_space else ""
        if a_low in haystack_lower or (has_space and len(nospace) >= 8 and nospace in haystack_lower):
            alias_candidates.append((alias, display, has_space, nospace))
    alias_candidates.sort(key=lambda x: len(x[0]), reverse=True)

    for alias, display, has_space, nospace in alias_candidates:
        if len(matched) >= _MAX_PERFORMERS_PER_VIDEO:
            break
        if display.lower() in seen_lower:
            continue
        try:
            pattern = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
            found = False
            for m in re.finditer(pattern, haystack, re.IGNORECASE | re.UNICODE):
                s, e = m.start(), m.end()
                if _is_unclaimed(s, e):
                    if not has_space and not _is_isolated_word(s, e):
                        continue
                    _claim_span(s, e, display)
                    found = True
            if not found and has_space and len(nospace) >= 8 and nospace in haystack_lower:
                pattern_ns = r"(?<!\w)" + re.escape(nospace) + r"(?!\w)"
                for m in re.finditer(pattern_ns, haystack, re.IGNORECASE | re.UNICODE):
                    s, e = m.start(), m.end()
                    if _is_unclaimed(s, e):
                        _claim_span(s, e, display)
        except re.error:
            pass

    return prune_redundant_performers(matched)
