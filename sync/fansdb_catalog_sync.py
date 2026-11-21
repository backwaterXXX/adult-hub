"""Import the FansDB performer catalogue into the local matching index."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from common import db
from common.config import get_fansdb_api_key, get_fansdb_api_url
from common.http_rate_limit import request_with_host_limit
from common.performer_catalog import normalize_name

logger = logging.getLogger("fansdb_catalog")
DEFAULT_ENDPOINT = "https://fansdb.cc/graphql"


def _updated_epoch(value) -> float | None:
    """Convert FansDB's ISO-8601 updated timestamp to the DB epoch API."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


QUERY = """
query PerformerCatalogue($page: Int!, $perPage: Int!, $gender: GenderFilterEnum) {
  queryPerformers(input: {gender: $gender, page: $page, per_page: $perPage, sort: UPDATED_AT, direction: DESC}) {
    count
    performers { id name aliases gender updated deleted }
  }
}
"""


def _get_endpoint() -> str:
    return os.getenv("FANSDB_API_URL", "").strip() or get_fansdb_api_url() or DEFAULT_ENDPOINT


def _get_api_key(token: str | None = None) -> str:
    return (token or os.getenv("FANSDB_API_KEY", "").strip()
            or os.getenv("FANS_API_KEY", "").strip()
            or get_fansdb_api_key() or "")


KEY_SYNCED_AT = "performer_catalog_fansdb_synced_at"
PROJECT_TIMEZONE = ZoneInfo("Europe/Warsaw")


def _record_synced_timestamp(conn: db.Connection, now_ts: int | None = None) -> int:
    ts = int(time.time()) if now_ts is None else int(now_ts)
    with conn:
        conn.execute("""
            INSERT INTO meta(key,value) VALUES(%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
        """, (KEY_SYNCED_AT, str(ts)))
    return ts


def is_catalog_due(conn: db.Connection, *, now: int | float | None = None,
                   interval_seconds: int = 86400) -> bool:
    """True if FansDB performer catalog should sync.

    For the daily gate (interval_seconds=86400), uses local calendar date in the
    project timezone (Europe/Warsaw) so that seconds jitter across consecutive days
    and daylight saving time transitions (23h or 25h days) do not skip scheduled runs,
    while preventing repeat executions on the same calendar day.
    """
    row = conn.execute("SELECT value FROM meta WHERE key=%s", (KEY_SYNCED_AT,)).fetchone()
    try:
        last = int(row[0]) if row else 0
    except (TypeError, ValueError):
        last = 0
    if not last:
        return True
    now_ts = int(time.time()) if now is None else int(now)
    if interval_seconds != 86400:
        return (now_ts - last) >= interval_seconds

    last_date = datetime.fromtimestamp(last, tz=PROJECT_TIMEZONE).date()
    now_date = datetime.fromtimestamp(now_ts, tz=PROJECT_TIMEZONE).date()
    return now_date > last_date


def sync_catalog_if_due(conn: db.Connection, *, interval_seconds: int = 86400,
                        now: int | float | None = None) -> tuple[int, int] | None:
    """Refresh at most daily; missing credentials or not due keeps cache usable."""
    if not _get_api_key():
        logger.debug("FansDB performer catalog skipped: unconfigured")
        return None
    now_ts = int(time.time()) if now is None else int(now)
    if not is_catalog_due(conn, now=now_ts, interval_seconds=interval_seconds):
        logger.debug("FansDB performer catalog skipped: not due")
        return None
    return sync_catalog(conn, now=now_ts)


sync_fansdb_catalog_if_due = sync_catalog_if_due


def _post(payload, token: str, endpoint: str | None = None):
    import httpx
    url = endpoint or _get_endpoint()
    response = request_with_host_limit(
        httpx, "post", url, json=payload, timeout=30,
        headers={"ApiKey": token, "Accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


def _fetch_catalog_records(request: Callable, api_key: str, per_page: int, pause: float,  # noqa: C901
                           gender: str | None = None, max_pages: int | None = None) -> list:
    """Collect every performer row across pages, pausing between requests."""
    if per_page <= 0:
        raise ValueError("per_page must be positive")
    records = []
    seen_ids: set[str] = set()
    page = 1
    total_count: int | None = None

    while True:
        page_rows, count = _fetch_catalog_page(request, api_key, page, per_page, gender=gender)
        if total_count is None:
            total_count = count
        elif count != total_count:
            raise RuntimeError(f"FansDB total count changed during pagination: was {total_count}, now {count}")

        if total_count == 0:
            if page_rows:
                raise RuntimeError("FansDB returned records but reported count=0")
            break

        total_pages = (total_count + per_page - 1) // per_page
        effective_max_pages = total_pages if max_pages is None else min(total_pages, max(0, max_pages))

        expected_page_size = min(per_page, total_count - len(records))
        if len(page_rows) != expected_page_size:
            raise RuntimeError(
                f"FansDB returned incomplete page {page}: got {len(page_rows)}, expected {expected_page_size}"
            )

        page_ids: list[str] = []
        for row in page_rows:
            if not isinstance(row, dict) or not row.get("id") or not str(row.get("name", "")).strip():
                raise RuntimeError(f"FansDB returned malformed performer record on page {page}")
            r_id = str(row["id"])
            if r_id in seen_ids or r_id in page_ids:
                raise RuntimeError(f"FansDB returned repeated performer id {r_id!r} on page {page}")
            page_ids.append(r_id)

        seen_ids.update(page_ids)
        records.extend(page_rows)

        if page >= effective_max_pages or len(records) >= total_count:
            break

        page += 1
        if pause > 0:
            time.sleep(pause)

    if max_pages is None and total_count is not None and len(records) != total_count:
        raise RuntimeError(f"FansDB catalog import incomplete: got {len(records)} of {total_count} records")

    return records


def _fetch_catalog_page(request: Callable, api_key: str, page: int, per_page: int,
                        gender: str | None = None) -> tuple[list, int]:
    """Return (performer rows on this page, total count reported by FansDB)."""
    variables: dict = {"page": page, "perPage": per_page}
    if gender:
        variables["gender"] = gender
    payload = {"query": QUERY, "variables": variables}
    result = request(payload, api_key)
    if hasattr(result, "json"):
        result = result.json()
    if not isinstance(result, dict) or result.get("errors"):
        raise RuntimeError("FansDB returned an invalid performer catalogue page")
    data = result.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("FansDB response missing data object")
    query_data = data.get("queryPerformers")
    if not isinstance(query_data, dict):
        raise RuntimeError("FansDB response missing queryPerformers object")
    count = query_data.get("count")
    if count is None or not isinstance(count, int) or count < 0:
        raise RuntimeError(f"FansDB response missing or invalid count: {count!r}")
    page_rows = query_data.get("performers")
    if not isinstance(page_rows, list):
        raise RuntimeError("FansDB response did not include performers list")
    return page_rows, count


def sync_catalog(conn: db.Connection, *, token: str | None = None,
                 post: Callable | None = None, per_page: int = 100,
                 pause: float = 0.25, gender: str | None = None,
                 max_pages: int | None = None,
                 now: int | float | None = None) -> tuple[int, int]:
    """Fetch FansDB performer catalogue pages and merge into local index.

    Returns (performers, aliases). Existing local rows survive source errors.
    """
    api_key = _get_api_key(token)
    if not api_key and post is None:
        raise RuntimeError("FANSDB_API_KEY is required to import the catalogue")
    gender_filter = gender or os.getenv("FANSDB_CATALOG_GENDER", "").strip() or None
    request = post or _post
    records = _fetch_catalog_records(request, api_key, per_page, pause,
                                     gender=gender_filter, max_pages=max_pages)

    alias_count = 0
    with conn:
        for record in records:
            performer_id = "fansdb:" + str(record["id"])
            name = str(record["name"]).strip()
            if not name:
                continue
            conn.execute("""
                INSERT INTO performer_catalog(id,source,source_id,name,updated_at,active)
                VALUES (%s,'fansdb',%s,%s,%s,%s)
                ON CONFLICT(source,source_id) DO UPDATE SET
                    name=EXCLUDED.name, updated_at=EXCLUDED.updated_at,
                    active=EXCLUDED.active
            """, (performer_id, str(record["id"]), name, _updated_epoch(record.get("updated")),
                  not bool(record.get("deleted"))))
            conn.execute("DELETE FROM performer_catalog_aliases WHERE performer_id=%s AND origin='fansdb'",
                         (performer_id,))
            for alias in dict.fromkeys([name, *(record.get("aliases") or [])]):
                alias = str(alias or "").strip()
                normalized = normalize_name(alias)
                if not normalized:
                    continue
                conn.execute("""
                    INSERT INTO performer_catalog_aliases(performer_id,alias,normalized,origin,approved)
                    VALUES (%s,%s,%s,'fansdb',TRUE)
                    ON CONFLICT(performer_id,normalized,origin) DO UPDATE SET
                        alias=EXCLUDED.alias,approved=TRUE
                """, (performer_id, alias, normalized))
                alias_count += 1
    if max_pages is None:
        _record_synced_timestamp(conn, now_ts=now)
        logger.info("FansDB catalogue imported: %d people, %d alias spellings", len(records), alias_count)
    else:
        logger.info("FansDB diagnostic partial import completed: %d people, %d alias spellings (max_pages=%d)",
                    len(records), alias_count, max_pages)
    return len(records), alias_count


sync_fansdb_catalog = sync_catalog
