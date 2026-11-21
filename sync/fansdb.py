"""FansDB performer metadata enrichment.

Queries FansDB GraphQL endpoint (https://fansdb.cc/graphql)
to enrich performers with birth dates, nationality/country,
aliases, and HD portrait images.
Requires FANSDB_API_KEY environment variable.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable, Dict, Optional, Tuple

try:
    import httpx
except ImportError:
    httpx = None

from common import db
from common.external_metadata import entity_key, record_attempt
from common.http_rate_limit import request_with_host_limit, wait_for_host_slot
from common.performer_catalog import normalize_name
from common.performer_profiles import get_override_fields, lock_profile_enrichment

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

logger = logging.getLogger("fansdb")

FANSDB_API_URL = os.environ.get("FANSDB_API_URL", "https://fansdb.cc/graphql")
USER_AGENT = "AdultHubSync/1.0 (fansdb-enrichment)"
TIMEOUT = 15.0
ENV_TOKEN_KEY = "FANSDB_API_KEY"

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


def _decode_http_payload(payload) -> Optional[dict]:
    """Best-effort conversion of an http_post result to parsed JSON."""
    return decode_http_payload(payload, logger)


def _api_token(token: Optional[str]) -> str:
    return resolve_api_token(token, ENV_TOKEN_KEY, "FANS_API_KEY")


def _post_performer_query(headers: dict, payload: dict, http_post: Optional[Callable]):
    """POST the GraphQL query and return the decoded JSON response, or None."""
    api_url = os.environ.get("FANSDB_API_URL", FANSDB_API_URL)
    if http_post is not None:
        wait_for_host_slot(api_url)
        raw = http_post(api_url, headers=headers, json=payload, timeout=TIMEOUT)
        status_code = getattr(raw, "status_code", None)
        if status_code is None and isinstance(raw, dict):
            status_code = raw.get("status_code") or raw.get("status")
        if status_code is not None and status_code != 200:
            raise RuntimeError(f"FansDB HTTP error {status_code}")
        data = _decode_http_payload(raw)
        if data is None and not (isinstance(raw, dict) and "data" in raw):
            raise RuntimeError("FansDB returned invalid or unparseable response")
        return data
    if httpx is not None:
        resp = request_with_host_limit(
            httpx, "post", api_url, headers=headers, json=payload,
            timeout=TIMEOUT, follow_redirects=True,
        )
        status_code = getattr(resp, "status_code", 200)
        if status_code != 200:
            raise RuntimeError(f"FansDB HTTP error {status_code}")
        return resp.json()
    return None


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
    deduped_aliases = performer_aliases(perf, name_clean)
    photo = _performer_photo(perf)

    if not bio and not birth and not nationality and not deduped_aliases and not photo:
        return None

    perf_id = str(perf.get("id") or "").strip()
    fansdb_url = f"https://fansdb.cc/performers/{perf_id}" if perf_id else ""

    return {
        "bio": bio,
        "birth": birth,
        "nationality": nationality,
        "aliases": deduped_aliases,
        "photo": photo,
        "fansdb_id": perf_id,
        "fansdb_url": fansdb_url,
    }


def search_performer(
    name: str,
    token: Optional[str] = None,
    http_post: Optional[Callable] = None,
    *, raise_on_error: bool = False,
) -> Optional[Dict[str, object]]:
    """Search FansDB GraphQL endpoint for performer details.

    Returns dict with bio, birth, nationality, aliases (list), photo (str), or None.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    api_token = _api_token(token)
    if not api_token and http_post is None:
        logger.debug("FansDB search skipped: %s not set", ENV_TOKEN_KEY)
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
        if data is None or not isinstance(data, dict):
            raise RuntimeError("FansDB response was empty or not a valid JSON object")
        errors = data.get("errors")
        if errors:
            raise RuntimeError(f"FansDB GraphQL errors for {name!r}: {errors}")
        if "data" not in data or not isinstance(data.get("data"), dict):
            raise RuntimeError("FansDB response missing data object")
        query_performers = data["data"].get("queryPerformers")
        if query_performers is None or not isinstance(query_performers, dict):
            raise RuntimeError("FansDB response missing queryPerformers object")
        performers = query_performers.get("performers")
        if performers is None or not isinstance(performers, list):
            raise RuntimeError("FansDB response missing performers list")
    except Exception as e:
        logger.debug("FansDB search_performer(%r) request failed: %s", name, e)
        if raise_on_error:
            raise
        return None

    if not performers:
        return None

    name_clean = normalize_name(name)
    perf = match_by_name_or_alias(performers, name_clean)
    if perf is None:
        return None
    return _performer_result(perf, name_clean)


def _candidate_rows(conn: db.Connection, limit: int) -> list:
    return conn.execute(
        """
        SELECT b.name FROM performer_bios b
        LEFT JOIN performer_photos p ON lower(p.name) = lower(b.name)
        LEFT JOIN external_metadata_attempts a
          ON a.entity_type='performer' AND a.entity_key=lower(b.name) AND a.source='fansdb'
        WHERE (b.birth IS NULL OR b.birth = '' OR b.nationality IS NULL OR b.nationality = ''
               OR p.thumb IS NULL OR p.thumb = '')
          AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=CURRENT_TIMESTAMP)
        ORDER BY a.last_attempt_at ASC NULLS FIRST,b.updated_at ASC NULLS FIRST,lower(b.name) ASC
        LIMIT %s
        """,
        (max(0, int(limit)),),
    ).fetchall()


def _update_performer_bio_row(conn: db.Connection, name: str, bio: str, birth: str, nationality: str,
                              fansdb_url: str, merged_aliases: list, locked_fields, now: int) -> None:
    cols = set(db.table_columns(conn, "performer_bios"))
    if "fansdb_url" in cols:
        conn.execute(
            """
            UPDATE performer_bios
            SET bio = CASE WHEN %s THEN bio WHEN (bio IS NULL OR bio = '') AND %s != '' THEN %s ELSE bio END,
                birth = CASE WHEN (birth IS NULL OR birth = '') AND %s != '' THEN %s ELSE birth END,
                nationality = CASE WHEN (nationality IS NULL OR nationality = '') AND %s != '' THEN %s ELSE nationality END,
                fansdb_url = CASE WHEN %s != '' THEN %s ELSE fansdb_url END,
                aliases = CASE WHEN %s THEN aliases WHEN %s != '[]' THEN %s ELSE aliases END,
                updated_at = %s
            WHERE name = %s
            """,
            ("bio" in locked_fields, bio, bio, birth, birth, nationality, nationality,
             fansdb_url, fansdb_url,
             "aliases" in locked_fields,
             json.dumps(merged_aliases, ensure_ascii=False),
             json.dumps(merged_aliases, ensure_ascii=False), now, name),
        )
    else:
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


def _apply_fansdb_info(conn: db.Connection, name: str, info: dict) -> None:
    """Write one FansDB match into the performer tables and record the successful attempt."""
    bio = info.get("bio") or ""
    birth = info.get("birth") or ""
    nationality = info.get("nationality") or ""
    aliases = info.get("aliases") or []
    photo = info.get("photo") or ""
    fansdb_url = info.get("fansdb_url") or ""
    now = int(time.time())
    lock_profile_enrichment(conn, name)
    locked_fields = get_override_fields(conn, name)
    merged_aliases = merge_stored_aliases(conn, name, aliases, locked_fields)

    with conn:
        _update_performer_bio_row(conn, name, bio, birth, nationality, fansdb_url,
                                  merged_aliases, locked_fields, now)
        link_aliases(conn, name, aliases, locked_fields)
        if photo and "photo" not in locked_fields:
            store_photo(conn, name, photo, now)

    record_attempt(conn, "performer", entity_key(name), "fansdb", "found")


def backfill_fansdb(
    conn: db.Connection,
    limit: int = 15,
    sleep: float = 1.0,
    token: Optional[str] = None,
    fetch: Optional[Callable] = None,
) -> Tuple[int, int]:
    """Enrich performers with missing birth date, nationality, or photos from FansDB.

    Returns (checked, updated).
    """
    api_token = _api_token(token)
    if not api_token and fetch is None:
        logger.debug("backfill_fansdb skipped: no token configured")
        return 0, 0

    try:
        rows = _candidate_rows(conn, limit)
    except Exception as e:
        logger.warning("backfill_fansdb query failed: %s", e)
        raise

    candidates = [r[0] for r in rows if r and r[0]]
    checked = 0
    updated = 0

    for index, name in enumerate(candidates):
        pause_between_requests(sleep, index, logger, "backfill_fansdb")
        checked += 1
        try:
            info = search_performer(name, token=api_token, http_post=fetch, raise_on_error=True)
            if not info:
                record_attempt(conn, "performer", entity_key(name), "fansdb", "no_match")
                continue

            _apply_fansdb_info(conn, name, info)
            updated += 1
            logger.info("FansDB enriched %s (birth=%s, nat=%s, aliases=%d, photo=%s)",
                        name, bool(info.get("birth") or ""), bool(info.get("nationality") or ""),
                        len(info.get("aliases") or []), bool(info.get("photo") or ""))
        except Exception as e:
            try:
                record_attempt(conn, "performer", entity_key(name), "fansdb", "error", error=e)
            except Exception:
                logger.debug("Could not save FansDB retry state for %r", name)
            logger.warning("backfill_fansdb(%r) failed: %s", name, e)

    if checked:
        logger.info("FansDB performer backfill: checked=%d enriched=%d", checked, updated)
    return checked, updated
