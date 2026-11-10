"""Pure helpers used across the sync pipeline: filenames, titles, retry math and NFO lookups.

None of these read the mutable sync settings, so they can live outside sync.py.
sync.py re-exports every name, so existing ``sync.<name>`` references keep working.
"""

import re
from typing import Any, Dict, Iterable, List, Optional

from common import db
from common.duration import parse_duration_seconds
from common.performer_titles import parse_title_performers_with_known_map as _parse_title_performers_with_known_map
from sync.http_client import (
    is_retryable_error as _is_retryable_error_impl,
    parse_retry_after as _parse_retry_after,
    retry_after_from_error as _retry_after_from_error_impl,
)
from sync.nfo import (
    format_dateadded as _format_dateadded_impl,
    load_performer_links as _load_performer_links_impl,
    load_performer_thumbs as _load_performer_thumbs_impl,
)
from sync.video_persistence import _merge_video_fields as _merge_video_fields_impl


def sanitize_filename(name: str) -> str:
    clean = re.sub(r'[\\/*?:"<>|]', "", name)
    clean = re.sub(r'\s+', " ", clean).strip()
    return clean[:120]

def format_category_name(cat_id: str) -> str:
    parts = cat_id.split("-")
    if len(parts) > 1 and len(parts[-1]) == 5 and parts[-1].isalnum():
        name_parts = parts[:-1]
    else:
        name_parts = parts
    return " ".join(p.capitalize() for p in name_parts)

def parse_duration_to_seconds(dur_str: Optional[str]) -> int:
    """Parse duration string supporting HH:MM:SS, MM:SS, SS, and min/sec text."""
    return parse_duration_seconds(dur_str)

def parse_title_performers(title: str, known_performers: Optional[Iterable[str]] = None) -> List[str]:
    if isinstance(known_performers, dict):
        known_set = known_performers
    elif known_performers:
        known_set = {k.strip().lower(): k.strip() for k in known_performers}
    else:
        known_set = {}
    return _parse_title_performers_with_known_map(title, known_set)

def _merge_video_fields(existing: Dict[str, Any], new: Dict[str, Any]):
    """Union performers/categories from a duplicate sighting (order-preserving)."""
    _merge_video_fields_impl(existing, new)

def parse_retry_after(value: Any, default: float, cap: float = 60.0) -> float:
    return _parse_retry_after(value, default, cap)

def _retry_after_from_error(e: Exception, default: float) -> float:
    return _retry_after_from_error_impl(e, default, parse_retry_after_fn=parse_retry_after)

def _is_retryable_error(e: Exception) -> bool:
    return _is_retryable_error_impl(e)

def format_dateadded(ts) -> str:
    return _format_dateadded_impl(ts)

def load_performer_thumbs(conn: db.Connection, names) -> Dict[str, str]:
    return _load_performer_thumbs_impl(conn, names)

def load_performer_links(conn: db.Connection, names) -> Dict[str, Dict[str, str]]:
    return _load_performer_links_impl(conn, names)
