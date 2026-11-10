"""Porn Base Central (pbc.xxx) enrichment for performer names.

Contract for the (separately implemented) provider:
  - table ``performer_aliases`` maps ``alias.lower()`` -> canonical name,
  - canonical value is stored EXACTLY as it appears in ``videos.performers``.

Polite by design: single MediaWiki API request per performer, 15s timeout,
caller-controlled sleep between requests.
"""
import importlib
import json
import logging
from common import db
from common.external_metadata import entity_key, record_attempt  # noqa: F401
import time  # noqa: F401 - retained as a patchable compatibility dependency
from typing import Callable, Dict, List, Optional, Tuple

from common.performers import is_safe_alias, is_valid_performer_name  # noqa: F401 - is_safe_alias re-exported to pbc_backfill

logger = logging.getLogger("pbc")


def _sibling(module_name: str):
    """Import a sibling ``sync`` module by its package name.

    Resolved on every call, so a patch on the implementation module (for
    example ``pbc_client``) is seen through this facade.
    """
    return importlib.import_module("sync." + module_name)


def page_title(name: str) -> str:
    return _sibling("pbc_client").page_title(name)


def _decode_http_payload(payload) -> Optional[dict]:
    return _sibling("pbc_client")._decode_http_payload(payload)


def fetch_wikitext(title, http_get=None, *, strict: bool = False) -> Optional[str]:
    return _sibling("pbc_client").fetch_wikitext(title, http_get, strict=strict)


def search_page_title(name: str, http_get=None, *, strict: bool = False) -> Optional[str]:
    return _sibling("pbc_client").search_page_title(name, http_get, strict=strict)


def _strip_templates(text: str) -> str:
    return _sibling("pbc_parsing")._strip_templates(text)


def _clean_inline(text: str) -> str:
    return _sibling("pbc_parsing")._clean_inline(text)


def _split_aliases(value: str) -> List[str]:
    return _sibling("pbc_parsing")._split_aliases(value)


def _infobox_field(wikitext: str, field: str) -> str:
    return _sibling("pbc_parsing")._infobox_field(wikitext, field)


def _first_paragraph_raw(wikitext: str) -> str:
    return _sibling("pbc_parsing")._first_paragraph_raw(wikitext)


def _aka_aliases(paragraph_raw: str) -> List[str]:
    return _sibling("pbc_parsing")._aka_aliases(paragraph_raw)


def parse_person(wikitext) -> Dict[str, object]:
    return _sibling("pbc_parsing").parse_person(wikitext)


def profile_matches_name(name: str, parsed: Dict[str, object]) -> bool:
    return _sibling("pbc_parsing").profile_matches_name(name, parsed)


def ensure_tables(conn: db.Connection) -> None:
    """Ensure performer tables exist (managed by versioned migrations)."""
    try:
        from common.migrations import apply_migrations
        apply_migrations(conn)
    except Exception:
        logger.debug("Ignored exception in ensure_tables", exc_info=True)


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
        except Exception:
            logger.debug("Ignored exception in _performer_values", exc_info=True)
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
    return _sibling("pbc_backfill").backfill_pbc(conn, limit=limit, sleep=sleep, fetch=fetch, search=search)
