"""ThePornDB (TPDB) performer metadata enrichment fallback.

Fallback source after PBC. Fetches performer data from
api.metadataapi.net REST API when PBC yields an empty bio.
Requires THEPORNDB_API_TOKEN environment variable or config setting.
"""
from __future__ import annotations

import json
import logging
from common import db
from common.external_metadata import entity_key, record_attempt
from common.http_rate_limit import request_with_host_limit, wait_for_host_slot
from common.performer_catalog import normalize_name
from common.performer_profiles import get_override_fields, lock_profile_enrichment
import time
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import quote

try:
    import httpx
except ImportError:
    httpx = None

from sync.provider_common import (
    decode_http_payload,
    link_aliases,
    match_by_name_or_alias,
    merge_stored_aliases,
    pause_between_requests,
    performer_aliases,
    resolve_api_token,
    store_photo,
)

logger = logging.getLogger("theporndb")

TPDB_API_URL = "https://api.theporndb.net/performers"
LEGACY_TPDB_API_URL = "https://api.metadataapi.net/performers"
USER_AGENT = "AdultHubSync/1.0 (theporndb-enrichment)"
TIMEOUT = 15.0
ENV_TOKEN_KEY = "THEPORNDB_API_TOKEN"


def _decode_http_payload(payload) -> Optional[dict]:
    """Best-effort conversion of an http_get result to parsed JSON."""
    return decode_http_payload(payload, logger)


def _api_token(token: Optional[str]) -> str:
    return resolve_api_token(token, ENV_TOKEN_KEY, "THEPORNDB_API_KEY")


def _get_via_http_get(url: str, headers: dict, http_get: Callable):
    wait_for_host_slot(url)
    try:
        raw = http_get(url, headers=headers, timeout=TIMEOUT)
    except TypeError:
        try:
            raw = http_get(url, timeout=TIMEOUT)
        except TypeError:
            raw = http_get(url)
    return _decode_http_payload(raw)


def _httpx_get_json(url: str, headers: dict) -> Optional[dict]:
    try:
        resp = request_with_host_limit(
            httpx, "get", url, headers=headers, timeout=TIMEOUT,
            follow_redirects=True,
        )
        if getattr(resp, "status_code", 200) == 200:
            return resp.json()
    except Exception:
        logger.debug("Ignored exception in search_performer", exc_info=True)
    return None


def _urllib_get_json(url: str, headers: dict):
    import urllib.request
    req = urllib.request.Request(url, headers=headers)
    wait_for_host_slot(url)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _search_without_http_get(name: str, url: str, headers: dict):
    """Query httpx (primary, then legacy host), falling back to urllib."""
    data = None
    if httpx is not None:
        data = _httpx_get_json(url, headers)
        if data is None:
            legacy_url = f"{LEGACY_TPDB_API_URL}?q={quote(name.strip(), safe='')}"
            data = _httpx_get_json(legacy_url, headers)
    if data is None:
        data = _urllib_get_json(url, headers)
    return data


def _select_performer(results: list, name_clean: str) -> Optional[dict]:
    """Pick the best matching result: exact name first, then exact alias."""
    perf = match_by_name_or_alias(results, name_clean)
    return dict(perf) if perf is not None else None


def _merge_performer_details(perf: dict, name: str, headers: dict, http_get: Optional[Callable]) -> None:
    """Fetch details if available to obtain extras (birthday, nationality, birthplace) and posters."""
    perf_id = perf.get("id") or perf.get("slug") or perf.get("_id")
    if perf_id and http_get is None and httpx is not None:
        try:
            detail_url = f"{TPDB_API_URL}/{perf_id}"
            d_resp = request_with_host_limit(
                httpx, "get", detail_url, headers=headers, timeout=TIMEOUT,
                follow_redirects=True,
            )
            if getattr(d_resp, "status_code", 200) == 200:
                d_data = d_resp.json()
                if isinstance(d_data, dict) and isinstance(d_data.get("data"), dict):
                    for k, v in d_data["data"].items():
                        if v is not None:
                            perf[k] = v
        except Exception as e:
            logger.debug("search_performer(%r) detail fetch failed: %s", name, e)


def _performer_photo(perf: dict) -> str:
    """High-quality photo priority: posters list -> image -> poster -> thumbnail."""
    photo = ""
    posters = perf.get("posters")
    if isinstance(posters, list) and posters:
        for p_item in posters:
            if isinstance(p_item, dict) and p_item.get("url"):
                photo = str(p_item["url"]).strip()
                break
            elif isinstance(p_item, str) and p_item.strip():
                photo = p_item.strip()
                break
    if not photo:
        photo = (perf.get("image") or perf.get("poster") or perf.get("thumbnail") or "").strip()
    return photo


def _performer_result(perf: dict, name_clean: str) -> Optional[Dict[str, object]]:
    bio = (perf.get("bio") or perf.get("description") or "").strip()
    extras = perf.get("extras") if isinstance(perf.get("extras"), dict) else {}
    birth = (extras.get("birthday") or perf.get("birthday") or perf.get("date_of_birth") or "").strip()
    if birth and "T" in birth:
        birth = birth.split("T")[0].strip()

    nationality = (extras.get("nationality") or extras.get("birthplace") or perf.get("nationality") or "").strip()
    deduped_aliases = performer_aliases(perf, name_clean)
    photo = _performer_photo(perf)

    if not bio and not birth and not nationality and not deduped_aliases and not photo:
        return None

    return {
        "bio": bio,
        "birth": birth,
        "nationality": nationality,
        "aliases": deduped_aliases,
        "photo": photo,
    }


def search_performer(
    name: str,
    token: Optional[str] = None,
    http_get: Optional[Callable] = None,
    *, raise_on_error: bool = False,
) -> Optional[Dict[str, object]]:
    """Search TPDB for performer details.

    Returns dict with bio, birth, nationality, aliases (list), photo (str), or None.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    api_token = _api_token(token)
    if not api_token:
        logger.debug("TPDB search skipped: %s not set", ENV_TOKEN_KEY)
        return None

    url = f"{TPDB_API_URL}?q={quote(name.strip(), safe='')}"
    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }
    try:
        if http_get is not None:
            data = _get_via_http_get(url, headers, http_get)
        else:
            data = _search_without_http_get(name, url, headers)
    except Exception as e:
        logger.debug("search_performer(%r) request failed: %s", name, e)
        if raise_on_error:
            raise
        return None

    if not isinstance(data, dict):
        return None

    results = data.get("data")
    if not isinstance(results, list) or not results:
        return None

    name_clean = normalize_name(name)
    perf = _select_performer(results, name_clean)
    if perf is None:
        return None

    _merge_performer_details(perf, name, headers, http_get)
    return _performer_result(perf, name_clean)


def _append_extra_candidates(conn: db.Connection, candidates: list, limit: int) -> None:
    """Top up candidates with performers whose metadata is incomplete, ignoring retry timing."""
    extra_needed = limit - len(candidates)
    try:
        if candidates:
            placeholders = ",".join("%s" for _ in candidates)
            extra_rows = conn.execute(
                f"""
                SELECT b.name FROM performer_bios b
                LEFT JOIN performer_photos p ON lower(p.name) = lower(b.name)
                WHERE (b.birth IS NULL OR b.birth = '' OR b.nationality IS NULL OR b.nationality = ''
                       OR p.thumb IS NULL OR p.thumb = '')
                  AND b.name NOT IN ({placeholders})
                ORDER BY b.updated_at ASC NULLS FIRST, b.name ASC
                LIMIT %s
                """,
                tuple(candidates) + (extra_needed,),
            ).fetchall()
        else:
            extra_rows = conn.execute(
                """
                SELECT b.name FROM performer_bios b
                LEFT JOIN performer_photos p ON lower(p.name) = lower(b.name)
                WHERE (b.birth IS NULL OR b.birth = '' OR b.nationality IS NULL OR b.nationality = ''
                       OR p.thumb IS NULL OR p.thumb = '')
                ORDER BY b.updated_at ASC NULLS FIRST, b.name ASC
                LIMIT %s
                """,
                (extra_needed,),
            ).fetchall()
        for er in extra_rows:
            if er and er[0]:
                candidates.append(er[0])
    except Exception:
        logger.debug("Ignored exception in backfill_tpdb", exc_info=True)


def _tpdb_candidate_names(conn: db.Connection, limit: int, fetch: Optional[Callable]) -> list:
    # Select oldest due lookups first so an empty result cannot pin the
    # same alphabetic names at the head of every daily run.
    rows = conn.execute(
        """
        SELECT b.name FROM performer_bios b
        LEFT JOIN performer_photos p ON lower(p.name)=lower(b.name)
        LEFT JOIN external_metadata_attempts a
          ON a.entity_type='performer' AND a.entity_key=lower(b.name) AND a.source='theporndb'
        WHERE (b.bio IS NULL OR b.bio='' OR b.birth IS NULL OR b.birth=''
               OR b.nationality IS NULL OR b.nationality='' OR p.thumb IS NULL OR p.thumb='')
          AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=CURRENT_TIMESTAMP)
        ORDER BY a.last_attempt_at ASC NULLS FIRST, lower(b.name) ASC
        LIMIT %s
        """,
        (max(0, int(limit)),),
    ).fetchall()
    candidates = [r[0] for r in rows if r and r[0]]
    if len(candidates) < limit and fetch is None:
        _append_extra_candidates(conn, candidates, limit)
    return candidates


def _update_performer_bio_row(conn: db.Connection, name: str, bio: str, birth: str, nationality: str,
                              merged_aliases: list, locked_fields, now: int) -> None:
    try:
        conn.execute(
            """
            UPDATE performer_bios
            SET bio = CASE WHEN %s THEN bio WHEN (bio IS NULL OR bio = '') AND %s != '' THEN %s ELSE bio END,
                birth = CASE WHEN (birth IS NULL OR birth = '') AND %s != '' THEN %s ELSE birth END,
                nationality = CASE WHEN (nationality IS NULL OR nationality = '') AND %s != '' THEN %s ELSE nationality END,
                aliases = CASE WHEN %s THEN aliases WHEN %s != '[]' THEN %s ELSE aliases END,
                updated_at = %s
            WHERE name = %s
            """,
            ("bio" in locked_fields, bio, bio, birth, birth, nationality, nationality,
             "aliases" in locked_fields,
             json.dumps(merged_aliases, ensure_ascii=False),
             json.dumps(merged_aliases, ensure_ascii=False), now, name),
        )
    except Exception:
        conn.execute(
            """
            UPDATE performer_bios
            SET bio = CASE WHEN %s THEN bio WHEN %s != '' THEN %s ELSE bio END,
                birth = CASE WHEN %s != '' THEN %s ELSE birth END,
                aliases = CASE WHEN %s THEN aliases WHEN %s != '[]' THEN %s ELSE aliases END,
                updated_at = %s
            WHERE name = %s
            """,
            ("bio" in locked_fields, bio, bio, birth, birth,
             "aliases" in locked_fields,
             json.dumps(merged_aliases, ensure_ascii=False),
             json.dumps(merged_aliases, ensure_ascii=False), now, name),
        )


def _apply_tpdb_info(conn: db.Connection, name: str, info: dict) -> None:
    """Write one TPDB match into the performer tables and record the successful attempt."""
    bio = info.get("bio") or ""
    birth = info.get("birth") or ""
    nationality = info.get("nationality") or ""
    aliases = info.get("aliases") or []
    photo = info.get("photo") or ""
    now = int(time.time())
    lock_profile_enrichment(conn, name)
    locked_fields = get_override_fields(conn, name)
    merged_aliases = merge_stored_aliases(conn, name, aliases, locked_fields)

    with conn:
        _update_performer_bio_row(conn, name, bio, birth, nationality, merged_aliases, locked_fields, now)
        link_aliases(conn, name, aliases, locked_fields)
        if photo and "photo" not in locked_fields:
            store_photo(conn, name, photo, now)
    record_attempt(conn, "performer", entity_key(name), "theporndb", "found")


def backfill_tpdb(
    conn: db.Connection,
    limit: int = 15,
    sleep: float = 1.0,
    token: Optional[str] = None,
    fetch: Optional[Callable] = None,
) -> Tuple[int, int]:
    """Enrich performers with empty bios or missing metadata from ThePornDB API.

    Returns (checked, updated).
    """
    api_token = _api_token(token)
    if not api_token and fetch is None:
        logger.debug("backfill_tpdb skipped: no token configured")
        return 0, 0

    try:
        candidates = _tpdb_candidate_names(conn, limit, fetch)
    except Exception as e:
        logger.warning("backfill_tpdb query failed: %s", e)
        return 0, 0

    checked = 0
    updated = 0

    for index, name in enumerate(candidates):
        pause_between_requests(sleep, index, logger, "backfill_tpdb")
        checked += 1
        try:
            info = search_performer(name, token=api_token, http_get=fetch, raise_on_error=True)
            if not info:
                record_attempt(conn, "performer", entity_key(name), "theporndb", "no_match")
                continue

            _apply_tpdb_info(conn, name, info)
            updated += 1
            logger.info("TPDB enriched %s (bio=%s, birth=%s, nat=%s, aliases=%d, photo=%s)",
                        name, bool(info.get("bio") or ""), bool(info.get("birth") or ""),
                        bool(info.get("nationality") or ""), len(info.get("aliases") or []),
                        bool(info.get("photo") or ""))
        except Exception as e:
            try:
                record_attempt(conn, "performer", entity_key(name), "theporndb", "error", error=e)
            except Exception:
                logger.debug("Could not save ThePornDB retry state for %r", name)
            logger.warning("backfill_tpdb(%r) failed: %s", name, e)

    if checked:
        logger.info("ThePornDB performer backfill: checked=%d enriched=%d", checked, updated)
    return checked, updated
