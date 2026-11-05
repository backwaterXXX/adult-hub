"""StashDB performer metadata enrichment.

Queries StashDB GraphQL endpoint (https://stashdb.org/graphql)
to enrich performers with birth dates, nationality/country,
aliases, and HD portrait images.
Requires STASHDB_API_KEY or STASH_API_KEY environment variable.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable, Dict, List, Optional, Tuple

try:
    import httpx
except ImportError:
    httpx = None

from common import db
from common.external_metadata import entity_key, record_attempt
from common.http_rate_limit import request_with_host_limit, wait_for_host_slot
from common.performer_catalog import normalize_name
from common.performer_profiles import get_override_fields, lock_profile_enrichment

try:
    from common.performers import is_safe_alias
except ImportError:
    try:
        from performers import is_safe_alias
    except ImportError:
        def is_safe_alias(alias: str, canonical: str = "") -> bool:
            return True

logger = logging.getLogger("stashdb")

STASHDB_API_URL = os.environ.get("STASHDB_API_URL", "https://stashdb.org/graphql")
USER_AGENT = "AdultHubSync/1.0 (stashdb-enrichment)"
TIMEOUT = 15.0
ENV_TOKEN_KEY = "STASHDB_API_KEY"

GRAPHQL_QUERY = """
query SearchPerformers($name: String!) {
  queryPerformers(input: { names: $name }) {
    performers {
      id
      name
      disambiguation
      aliases
      gender
      birth_date
      country
      ethnicity
      images {
        url
      }
    }
  }
}
"""


def _decode_bytes_payload(payload) -> Optional[dict]:
    try:
        return json.loads(bytes(payload).decode("utf-8", errors="ignore"))
    except Exception:
        logger.debug("Ignored exception in _decode_http_payload", exc_info=True)
        return None


def _decode_str_payload(payload: str) -> Optional[dict]:
    try:
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        logger.debug("Ignored exception in _decode_http_payload", exc_info=True)
        return None


def _decode_response_object(payload) -> Optional[dict]:
    """Decode a response-like object (httpx / requests / mocks) via .json(), .content or .text."""
    try:
        if callable(getattr(payload, "json", None)):
            data = payload.json()
            return data if isinstance(data, dict) else None
    except Exception:
        logger.debug("Ignored exception in _decode_http_payload", exc_info=True)
    try:
        content = getattr(payload, "content", None)
        if isinstance(content, (bytes, bytearray)):
            return json.loads(bytes(content).decode("utf-8", errors="ignore"))
        text = getattr(payload, "text", None)
        if isinstance(text, str):
            return json.loads(text)
    except Exception:
        logger.debug("Ignored exception in _decode_http_payload", exc_info=True)
    return None


def _decode_http_payload(payload) -> Optional[dict]:
    """Best-effort conversion of an http_post result to parsed JSON."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        return _decode_bytes_payload(payload)
    if isinstance(payload, str):
        return _decode_str_payload(payload)
    return _decode_response_object(payload)


def _api_token(token: Optional[str]) -> str:
    return (
        token
        or os.environ.get(ENV_TOKEN_KEY, "").strip()
        or os.environ.get("STASH_API_KEY", "").strip()
    )


def _post_performer_query(headers: dict, payload: dict, http_post: Optional[Callable]):
    """POST the GraphQL query and return the decoded JSON response, or None."""
    if http_post is not None:
        wait_for_host_slot(STASHDB_API_URL)
        try:
            raw = http_post(STASHDB_API_URL, headers=headers, json=payload, timeout=TIMEOUT)
        except TypeError:
            try:
                raw = http_post(STASHDB_API_URL, json=payload, timeout=TIMEOUT)
            except TypeError:
                raw = http_post(STASHDB_API_URL, json=payload)
        return _decode_http_payload(raw)
    if httpx is not None:
        resp = request_with_host_limit(
            httpx, "post", STASHDB_API_URL, headers=headers, json=payload,
            timeout=TIMEOUT, follow_redirects=True,
        )
        if getattr(resp, "status_code", 200) == 200:
            return resp.json()
    return None


def _select_performer(performers: list, name_clean: str) -> Optional[dict]:
    """Pick the performer whose name, then whose alias, matches the requested name."""
    for candidate in performers:
        if isinstance(candidate, dict) and normalize_name(candidate.get("name") or "") == name_clean:
            return candidate
    for candidate in performers:
        if not isinstance(candidate, dict):
            continue
        aliases = candidate.get("aliases") or []
        if isinstance(aliases, str):
            aliases = aliases.split(",")
        if any(normalize_name(alias) == name_clean for alias in aliases):
            return candidate
    return None


def _performer_aliases(perf: dict, name_clean: str) -> List[str]:
    aliases_raw = perf.get("aliases") or []
    aliases: List[str] = []
    if isinstance(aliases_raw, list):
        for a in aliases_raw:
            if isinstance(a, str) and a.strip() and normalize_name(a) != name_clean:
                aliases.append(a.strip())
    elif isinstance(aliases_raw, str) and aliases_raw.strip():
        for a in aliases_raw.split(","):
            if a.strip() and normalize_name(a) != name_clean:
                aliases.append(a.strip())

    deduped_aliases = []
    seen = set()
    for a in aliases:
        k = a.lower()
        if k not in seen:
            seen.add(k)
            deduped_aliases.append(a)
    return deduped_aliases


def _performer_photo(perf: dict) -> str:
    images = perf.get("images")
    if isinstance(images, list) and images:
        for img in images:
            if isinstance(img, dict) and img.get("url"):
                return str(img["url"]).strip()
    return ""


def _performer_result(perf: dict, name_clean: str) -> Optional[Dict[str, object]]:
    bio = (perf.get("disambiguation") or "").strip()
    birth = (perf.get("birth_date") or "").strip()
    if birth and "T" in birth:
        birth = birth.split("T")[0].strip()

    nationality = (perf.get("country") or "").strip()
    deduped_aliases = _performer_aliases(perf, name_clean)
    photo = _performer_photo(perf)

    if not bio and not birth and not nationality and not deduped_aliases and not photo:
        return None

    perf_id = str(perf.get("id") or "").strip()
    stashdb_url = f"https://stashdb.org/performers/{perf_id}" if perf_id else ""

    return {
        "bio": bio,
        "birth": birth,
        "nationality": nationality,
        "aliases": deduped_aliases,
        "photo": photo,
        "stashdb_id": perf_id,
        "stashdb_url": stashdb_url,
    }


def search_performer(
    name: str,
    token: Optional[str] = None,
    http_post: Optional[Callable] = None,
    *, raise_on_error: bool = False,
) -> Optional[Dict[str, object]]:
    """Search StashDB GraphQL endpoint for performer details.

    Returns dict with bio, birth, nationality, aliases (list), photo (str), or None.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    api_token = _api_token(token)
    if not api_token and http_post is None:
        logger.debug("StashDB search skipped: %s not set", ENV_TOKEN_KEY)
        return None

    headers = {
        "User-Agent": USER_AGENT,
        "ApiKey": api_token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "query": GRAPHQL_QUERY,
        "variables": {"name": name.strip()},
    }

    try:
        data = _post_performer_query(headers, payload, http_post)
    except Exception as e:
        logger.debug("StashDB search_performer(%r) request failed: %s", name, e)
        if raise_on_error:
            raise
        return None

    if not isinstance(data, dict):
        return None

    performers = (
        data.get("data", {})
        .get("queryPerformers", {})
        .get("performers")
    )
    if not isinstance(performers, list) or not performers:
        return None

    name_clean = normalize_name(name)
    perf = _select_performer(performers, name_clean)
    if perf is None:
        return None
    return _performer_result(perf, name_clean)


def _pause_between_requests(sleep: float, index: int) -> None:
    if sleep and index > 0:
        try:
            time.sleep(sleep)
        except Exception:
            logger.debug("Ignored exception in backfill_stashdb", exc_info=True)


def _candidate_rows(conn: db.Connection, limit: int) -> list:
    return conn.execute(
        """
        SELECT b.name FROM performer_bios b
        LEFT JOIN performer_photos p ON lower(p.name) = lower(b.name)
        LEFT JOIN external_metadata_attempts a
          ON a.entity_type='performer' AND a.entity_key=lower(b.name) AND a.source='stashdb'
        WHERE (b.birth IS NULL OR b.birth = '' OR b.nationality IS NULL OR b.nationality = ''
               OR p.thumb IS NULL OR p.thumb = '')
          AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=CURRENT_TIMESTAMP)
        ORDER BY a.last_attempt_at ASC NULLS FIRST,b.updated_at ASC NULLS FIRST,lower(b.name) ASC
        LIMIT %s
        """,
        (max(0, int(limit)),),
    ).fetchall()


def _merge_stored_aliases(conn: db.Connection, name: str, aliases: list, locked_fields) -> list:
    existing = conn.execute("SELECT aliases FROM performer_bios WHERE name=%s", (name,)).fetchone()
    try:
        prior_aliases = json.loads(existing[0]) if existing and existing[0] else []
    except (TypeError, ValueError):
        prior_aliases = []
    if not isinstance(prior_aliases, list):
        prior_aliases = []
    merged_aliases = []
    seen_aliases = set()
    for alias in [*prior_aliases, *([] if "aliases" in locked_fields else aliases)]:
        alias = str(alias or "").strip()
        if alias and alias.casefold() != name.casefold() and alias.casefold() not in seen_aliases:
            seen_aliases.add(alias.casefold())
            merged_aliases.append(alias)
    return merged_aliases


def _update_performer_bio_row(conn: db.Connection, name: str, bio: str, birth: str, nationality: str,
                              stashdb_url: str, merged_aliases: list, locked_fields, now: int) -> None:
    try:
        conn.execute(
            """
            UPDATE performer_bios
            SET bio = CASE WHEN %s THEN bio WHEN (bio IS NULL OR bio = '') AND %s != '' THEN %s ELSE bio END,
                birth = CASE WHEN (birth IS NULL OR birth = '') AND %s != '' THEN %s ELSE birth END,
                nationality = CASE WHEN (nationality IS NULL OR nationality = '') AND %s != '' THEN %s ELSE nationality END,
                stashdb_url = CASE WHEN %s != '' THEN %s ELSE stashdb_url END,
                aliases = CASE WHEN %s THEN aliases WHEN %s != '[]' THEN %s ELSE aliases END,
                updated_at = %s
            WHERE name = %s
            """,
            ("bio" in locked_fields, bio, bio, birth, birth, nationality, nationality,
             stashdb_url, stashdb_url,
             "aliases" in locked_fields,
             json.dumps(merged_aliases, ensure_ascii=False),
             json.dumps(merged_aliases, ensure_ascii=False), now, name),
        )
    except Exception:
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
                SET bio = CASE WHEN %s THEN bio WHEN (bio IS NULL OR bio = '') AND %s != '' THEN %s ELSE bio END,
                    birth = CASE WHEN (birth IS NULL OR birth = '') AND %s != '' THEN %s ELSE birth END,
                    aliases = CASE WHEN %s THEN aliases WHEN %s != '[]' THEN %s ELSE aliases END,
                    updated_at = %s
                WHERE name = %s
                """,
                ("bio" in locked_fields, bio, bio, birth, birth,
                 "aliases" in locked_fields,
                 json.dumps(merged_aliases, ensure_ascii=False),
                 json.dumps(merged_aliases, ensure_ascii=False), now, name),
            )


def _link_aliases(conn: db.Connection, name: str, aliases: list, locked_fields) -> None:
    for alias in ([] if "aliases" in locked_fields else aliases):
        a_clean = str(alias or "").strip().lower()
        if not a_clean or a_clean == name.strip().lower():
            continue
        if not is_safe_alias(a_clean, name):
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


def _store_photo(conn: db.Connection, name: str, photo: str, now: int) -> None:
    conn.execute(
        """
        INSERT INTO performer_photos (name, thumb, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT (name) DO UPDATE SET
            thumb = CASE WHEN performer_photos.thumb IS NULL OR performer_photos.thumb=''
                         THEN EXCLUDED.thumb ELSE performer_photos.thumb END,
            updated_at = EXCLUDED.updated_at
        """,
        (name, photo, now),
    )


def _apply_stashdb_info(conn: db.Connection, name: str, info: dict) -> None:
    """Write one StashDB match into the performer tables and record the successful attempt."""
    bio = info.get("bio") or ""
    birth = info.get("birth") or ""
    nationality = info.get("nationality") or ""
    aliases = info.get("aliases") or []
    photo = info.get("photo") or ""
    stashdb_url = info.get("stashdb_url") or ""
    now = int(time.time())
    lock_profile_enrichment(conn, name)
    locked_fields = get_override_fields(conn, name)
    merged_aliases = _merge_stored_aliases(conn, name, aliases, locked_fields)

    with conn:
        _update_performer_bio_row(conn, name, bio, birth, nationality, stashdb_url,
                                  merged_aliases, locked_fields, now)
        _link_aliases(conn, name, aliases, locked_fields)
        if photo and "photo" not in locked_fields:
            _store_photo(conn, name, photo, now)

    record_attempt(conn, "performer", entity_key(name), "stashdb", "found")


def backfill_stashdb(
    conn: db.Connection,
    limit: int = 15,
    sleep: float = 1.0,
    token: Optional[str] = None,
    fetch: Optional[Callable] = None,
) -> Tuple[int, int]:
    """Enrich performers with missing birth date, nationality, or photos from StashDB.

    Returns (checked, updated).
    """
    api_token = _api_token(token)
    if not api_token and fetch is None:
        logger.debug("backfill_stashdb skipped: no token configured")
        return 0, 0

    try:
        rows = _candidate_rows(conn, limit)
    except Exception as e:
        logger.warning("backfill_stashdb query failed: %s", e)
        return 0, 0

    candidates = [r[0] for r in rows if r and r[0]]
    checked = 0
    updated = 0

    for index, name in enumerate(candidates):
        _pause_between_requests(sleep, index)
        checked += 1
        try:
            info = search_performer(name, token=api_token, http_post=fetch, raise_on_error=True)
            if not info:
                record_attempt(conn, "performer", entity_key(name), "stashdb", "no_match")
                continue

            _apply_stashdb_info(conn, name, info)
            updated += 1
            logger.info("StashDB enriched %s (birth=%s, nat=%s, aliases=%d, photo=%s)",
                        name, bool(info.get("birth") or ""), bool(info.get("nationality") or ""),
                        len(info.get("aliases") or []), bool(info.get("photo") or ""))
        except Exception as e:
            try:
                record_attempt(conn, "performer", entity_key(name), "stashdb", "error", error=e)
            except Exception:
                logger.debug("Could not save StashDB retry state for %r", name)
            logger.warning("backfill_stashdb(%r) failed: %s", name, e)

    if checked:
        logger.info("StashDB performer backfill: checked=%d enriched=%d", checked, updated)
    return checked, updated
