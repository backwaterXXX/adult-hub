"""PBC performer backfill workflow, kept separate from its API/parser layers."""
from __future__ import annotations

from typing import List
import logging

logger = logging.getLogger(__name__)


def backfill_pbc(conn, limit=25, sleep=1.0, fetch=None, search=None):
    """Run a PBC backfill using dependencies from the compatibility facade."""
    from sync import pbc as _core
    return _backfill_pbc(conn, limit, sleep, fetch, search, _core)


def _load_candidates(conn, limit, _core):
    """Performer names whose PBC lookup is due, oldest attempt first.

    Returns None when a query stage fails; the failure is logged and the
    caller stops without touching any performer.
    """
    try:
        _core.ensure_tables(conn)
    except Exception as e:
        _core.logger.warning("backfill_pbc: ensure_tables failed: %s", e)
        return None
    try:
        names = _core.known_performer_names(conn)
    except Exception as e:
        _core.logger.warning("backfill_pbc: known_performer_names failed: %s", e)
        return None
    try:
        states = conn.execute(
            "SELECT entity_key,EXTRACT(EPOCH FROM next_attempt_at),"
            "EXTRACT(EPOCH FROM last_attempt_at) FROM external_metadata_attempts "
            "WHERE entity_type='performer' AND source='pbc'"
        ).fetchall()
        attempts = {str(row[0]).casefold(): (float(row[1]), float(row[2])) for row in states}
        now = _core.time.time()
        candidates = [name for name in names
                      if _core.entity_key(name) not in attempts or attempts[_core.entity_key(name)][0] <= now]
        candidates.sort(key=lambda name: (
            attempts.get(_core.entity_key(name), (0, 0))[1], name.casefold(), name))
    except Exception as e:
        _core.logger.warning("backfill_pbc: cannot select due performer lookups: %s", e)
        return None
    try:
        lim = int(limit)
    except Exception:
        lim = 25
    return candidates[: max(0, lim)]


def _call_fetch(fetch, slug: str, _core):
    """Call the injected fetcher; a fetcher that rejects ``strict`` falls back to the default."""
    try:
        return fetch(slug, strict=True) if fetch is _core.fetch_wikitext else fetch(slug)
    except TypeError:
        # Allow fetch mocks taking no/extra args gracefully.
        return _core.fetch_wikitext(slug)


def _fetch_wikitext(name: str, title: str, fetch, search, _core):
    """Return ``(wikitext, matched_slug)``, trying a title search when the direct page is empty."""
    wikitext = _call_fetch(fetch, title, _core)
    matched_slug = title
    if wikitext:
        return wikitext, matched_slug
    search_callable = search
    if search_callable is None and fetch is _core.fetch_wikitext:
        search_callable = _core.search_page_title
    if search_callable is None:
        return wikitext, matched_slug
    try:
        alt_title = (search_callable(name, strict=True)
                     if search_callable is _core.search_page_title else search_callable(name))
    except Exception as e:
        _core.logger.debug("search fallback for %r failed: %s", name, e)
        if search_callable is _core.search_page_title:
            raise
        alt_title = None
    if alt_title and _core.page_title(alt_title) != title:
        alt_slug = _core.page_title(alt_title)
        wikitext = _call_fetch(fetch, alt_slug, _core)
        if wikitext:
            matched_slug = alt_slug
    return wikitext, matched_slug


def _deduped_aliases(raw_aliases, name: str) -> List[str]:
    """Aliases that differ from the performer's own name, case-insensitively unique."""
    aliases = [
        a
        for a in raw_aliases
        if isinstance(a, str)
        and a.strip()
        and a.strip().lower() != name.lower()
    ]
    deduped: List[str] = []
    seen = set()
    for alias in aliases:
        alias = alias.strip()
        if alias.lower() not in seen:
            seen.add(alias.lower())
            deduped.append(alias)
    return deduped


def _upsert_bio(conn, name: str, bio: str, birth: str, merged_aliases, locked_fields, pbc_url: str,
                now: int, _core) -> None:
    """Insert or refresh ``performer_bios``, honouring manual overrides in ``locked_fields``."""
    try:
        conn.execute(
            """
            INSERT INTO performer_bios
                (name, bio, birth, aliases, updated_at, pbc_url)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                bio = CASE WHEN %s THEN performer_bios.bio WHEN performer_bios.bio IS NULL OR performer_bios.bio=''
                           THEN EXCLUDED.bio ELSE performer_bios.bio END,
                birth = CASE WHEN performer_bios.birth IS NULL OR performer_bios.birth=''
                             THEN EXCLUDED.birth ELSE performer_bios.birth END,
                pbc_url = CASE WHEN %s != '' THEN %s ELSE performer_bios.pbc_url END,
                aliases = CASE WHEN %s THEN performer_bios.aliases ELSE EXCLUDED.aliases END,
                updated_at = EXCLUDED.updated_at
            """,
            (name, bio, birth, _core.json.dumps(merged_aliases, ensure_ascii=False), now, pbc_url,
             "bio" in locked_fields, pbc_url, pbc_url, "aliases" in locked_fields),
        )
    except Exception:
        conn.execute(
            """
            INSERT INTO performer_bios
                (name, bio, birth, aliases, updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                bio = CASE WHEN %s THEN performer_bios.bio WHEN performer_bios.bio IS NULL OR performer_bios.bio=''
                           THEN EXCLUDED.bio ELSE performer_bios.bio END,
                birth = CASE WHEN performer_bios.birth IS NULL OR performer_bios.birth=''
                             THEN EXCLUDED.birth ELSE performer_bios.birth END,
                aliases = CASE WHEN %s THEN performer_bios.aliases ELSE EXCLUDED.aliases END,
                updated_at = EXCLUDED.updated_at
            """,
            (name, bio, birth, _core.json.dumps(merged_aliases, ensure_ascii=False), now,
             "bio" in locked_fields, "aliases" in locked_fields),
        )


def _store_aliases(conn, name: str, deduped, locked_fields, _core) -> None:
    for alias in ([] if "aliases" in locked_fields else deduped):
        a_clean = str(alias or "").strip().lower()
        if not _core.is_safe_alias(a_clean, name):
            continue
        rev = conn.execute(
            "SELECT 1 FROM performer_aliases WHERE alias = %s AND lower(canonical) = %s",
            (name.strip().lower(), a_clean)
        ).fetchone()
        if rev:
            continue
        conn.execute(
            """
            INSERT INTO performer_aliases (alias, canonical)
            VALUES (%s, %s)
            ON CONFLICT (alias) DO NOTHING
            """,
            (a_clean, name),
        )


def _store_performer(conn, name: str, fetch, search, _core, lock_profile_enrichment, get_override_fields) -> bool:
    """Fetch and persist PBC data for one performer. Returns False on a name mismatch."""
    title = _core.page_title(name)
    wikitext, matched_slug = _fetch_wikitext(name, title, fetch, search, _core)
    parsed = _core.parse_person(wikitext)
    if wikitext is not None and not _core.profile_matches_name(name, parsed):
        _core.record_attempt(
            conn, "performer", _core.entity_key(name), "pbc", "no_match"
        )
        return False
    deduped = _deduped_aliases(parsed.get("aliases") or [], name)
    bio = parsed.get("bio") or ""
    birth = parsed.get("birth") or ""
    if not isinstance(bio, str):
        bio = ""
    if not isinstance(birth, str):
        birth = ""
    now = int(_core.time.time())
    try:
        lock_profile_enrichment(conn, name)
        locked_fields = get_override_fields(conn, name)
        old_row = conn.execute("SELECT aliases FROM performer_bios WHERE name=%s", (name,)).fetchone()
        old_aliases = _core.json.loads(old_row[0]) if old_row and old_row[0] else []
        if not isinstance(old_aliases, list):
            old_aliases = []
    except Exception:
        old_aliases = []
    merged_aliases = []
    alias_seen = set()
    for alias in [*old_aliases, *([] if "aliases" in locked_fields else deduped)]:
        value = str(alias or "").strip()
        if value and value.casefold() != name.casefold() and value.casefold() not in alias_seen:
            alias_seen.add(value.casefold())
            merged_aliases.append(value)
    pbc_url = f"https://pbc.xxx/wiki/{matched_slug}" if (wikitext and matched_slug) else ""
    with conn:
        _upsert_bio(conn, name, bio, birth, merged_aliases, locked_fields, pbc_url, now, _core)
        _store_aliases(conn, name, deduped, locked_fields, _core)
    _core.record_attempt(conn, "performer", _core.entity_key(name), "pbc",
                         "found" if bio or birth or deduped else "no_match")
    return True


def _backfill_pbc(conn, limit, sleep, fetch, search, _core):
    """Backfill performer aliases/bios from PBC for names missing a bio row.

    Returns ``(checked, updated)``. Never raises: per-performer errors are
    caught and logged.
    """
    from common.performer_profiles import get_override_fields, lock_profile_enrichment
    if fetch is None:
        fetch = _core.fetch_wikitext
    candidates = _load_candidates(conn, limit, _core)
    if candidates is None:
        return 0, 0

    checked = 0
    updated = 0
    for index, name in enumerate(candidates):
        if sleep and index > 0:
            try:
                _core.time.sleep(sleep)
            except Exception:
                logger.debug("Ignored exception in _backfill_pbc", exc_info=True)
        checked += 1
        try:
            if _store_performer(conn, name, fetch, search, _core, lock_profile_enrichment, get_override_fields):
                updated += 1
        except Exception as e:
            try:
                _core.record_attempt(conn, "performer", _core.entity_key(name), "pbc", "error", error=e)
            except Exception:
                _core.logger.debug("Could not save PBC retry state for %r", name)
            _core.logger.warning("backfill_pbc(%r) failed: %s", name, e)
            continue
    if checked:
        _core.logger.info("PBC performer backfill: checked=%d records_written=%d", checked, updated)
    return checked, updated
