"""Porn Base Central (pbc.xxx) enrichment for performer names.

Contract for the (separately implemented) provider:
  - table ``performer_aliases`` maps ``alias.lower()`` -> canonical name,
  - canonical value is stored EXACTLY as it appears in ``videos.performers``.

Polite by design: single MediaWiki API request per performer, 15s timeout,
caller-controlled sleep between requests.
"""
import json
import logging
import re
from common import db
from common.external_metadata import entity_key, record_attempt  # noqa: F401
import time  # noqa: F401 - retained as a patchable compatibility dependency
from typing import Callable, Dict, List, Optional, Tuple

try:
    from common.performers import is_safe_alias, is_valid_performer_name
except ImportError:
    try:
        from performers import is_safe_alias, is_valid_performer_name
    except ImportError:
        def is_safe_alias(a, c):
            return bool(a and c and (len(a.split()) >= 2 or re.search(r'[\d._\-]', a) or a.endswith(('xxx', 'xx', 'vip', 'ofc')) or len(a) >= 12))
        def is_valid_performer_name(n):
            return bool(n and len(n.strip()) >= 2)

logger = logging.getLogger("pbc")

def page_title(name: str) -> str:
    if __package__:
        from .pbc_client import page_title as implementation
    else:
        from pbc_client import page_title as implementation
    return implementation(name)


def _decode_http_payload(payload) -> Optional[dict]:
    if __package__:
        from .pbc_client import _decode_http_payload as implementation
    else:
        from pbc_client import _decode_http_payload as implementation
    return implementation(payload)


def fetch_wikitext(title, http_get=None, *, strict: bool = False) -> Optional[str]:
    if __package__:
        from .pbc_client import fetch_wikitext as implementation
    else:
        from pbc_client import fetch_wikitext as implementation
    return implementation(title, http_get, strict=strict)


def search_page_title(name: str, http_get=None, *, strict: bool = False) -> Optional[str]:
    if __package__:
        from .pbc_client import search_page_title as implementation
    else:
        from pbc_client import search_page_title as implementation
    return implementation(name, http_get, strict=strict)


def _strip_templates(text: str) -> str:
    if __package__:
        from .pbc_parsing import _strip_templates as implementation
    else:
        from pbc_parsing import _strip_templates as implementation
    return implementation(text)


def _clean_inline(text: str) -> str:
    if __package__:
        from .pbc_parsing import _clean_inline as implementation
    else:
        from pbc_parsing import _clean_inline as implementation
    return implementation(text)


def _split_aliases(value: str) -> List[str]:
    if __package__:
        from .pbc_parsing import _split_aliases as implementation
    else:
        from pbc_parsing import _split_aliases as implementation
    return implementation(value)


def _infobox_field(wikitext: str, field: str) -> str:
    if __package__:
        from .pbc_parsing import _infobox_field as implementation
    else:
        from pbc_parsing import _infobox_field as implementation
    return implementation(wikitext, field)


def _first_paragraph_raw(wikitext: str) -> str:
    if __package__:
        from .pbc_parsing import _first_paragraph_raw as implementation
    else:
        from pbc_parsing import _first_paragraph_raw as implementation
    return implementation(wikitext)


def _aka_aliases(paragraph_raw: str) -> List[str]:
    if __package__:
        from .pbc_parsing import _aka_aliases as implementation
    else:
        from pbc_parsing import _aka_aliases as implementation
    return implementation(paragraph_raw)


def parse_person(wikitext) -> Dict[str, object]:
    if __package__:
        from .pbc_parsing import parse_person as implementation
    else:
        from pbc_parsing import parse_person as implementation
    return implementation(wikitext)


def profile_matches_name(name: str, parsed: Dict[str, object]) -> bool:
    if __package__:
        from .pbc_parsing import profile_matches_name as implementation
    else:
        from pbc_parsing import profile_matches_name as implementation
    return implementation(name, parsed)


def ensure_tables(conn: db.Connection) -> None:
    """Ensure performer tables exist (managed by versioned migrations)."""
    try:
        from common.migrations import apply_migrations
        apply_migrations(conn)
    except Exception as _exc:
        logger.debug("Ignored exception in %s", "ensure_tables", exc_info=True)


def known_performer_names(conn: db.Connection) -> List[str]:
    """Distinct performer names from ``videos.performers`` (JSON lists).

    Dedup is case-insensitive; the first-seen spelling variant is kept.
    """
    try:
        rows = conn.execute("SELECT performers FROM videos").fetchall()
    except Exception as e:
        logger.debug("known_performer_names: cannot read videos: %s", e)
        return []
    out: List[str] = []
    seen = set()
    for row in rows:
        raw = row[0] if row else None
        if raw is None or raw == "":
            continue
        names = _performer_values(raw)
        if names is None:
            continue
        for candidate in names:
            if candidate is None:
                continue
            name = str(candidate).strip()
            if not name or not is_valid_performer_name(name):
                continue
            key = name.lower()
            if key not in seen:
                seen.add(key)
                out.append(name)
    return out


def _performer_values(raw) -> Optional[List[str]]:
    """Split one ``videos.performers`` value into raw names; None means the row is unreadable."""
    if isinstance(raw, str) and raw.strip().startswith("["):
        try:
            parsed = json.loads(raw)
        except Exception as _exc:
            logger.debug("Ignored exception in %s", "known_performer_names", exc_info=True)
            return None
        if isinstance(parsed, list):
            return [str(v) for v in parsed]
        return [str(parsed)]
    if isinstance(raw, str):
        return raw.split(",")
    if isinstance(raw, list):
        return [str(v) for v in raw]
    return None


def backfill_pbc(
    conn: db.Connection,
    limit: int = 25,
    sleep: float = 1.0,
    fetch: Callable = None,
    search: Callable = None,
) -> Tuple[int, int]:
    """Compatibility facade for the PBC backfill workflow."""
    if __package__:
        from .pbc_backfill import backfill_pbc as implementation
    else:
        from pbc_backfill import backfill_pbc as implementation
    return implementation(conn, limit=limit, sleep=sleep, fetch=fetch, search=search)
