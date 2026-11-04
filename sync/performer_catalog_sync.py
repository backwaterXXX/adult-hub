"""Import the StashDB performer catalogue into the local matching index."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Callable

from common import db
from common.performer_catalog import normalize_name
from common.http_rate_limit import request_with_host_limit

logger = logging.getLogger("performer_catalog")
ENDPOINT = os.getenv("STASHDB_API_URL", "https://stashdb.org/graphql")


def _updated_epoch(value) -> float | None:
    """Convert StashDB's ISO-8601 updated timestamp to the DB epoch API."""
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
query PerformerCatalogue($page: Int!, $perPage: Int!) {
  queryPerformers(input: {gender: MALE, page: $page, per_page: $perPage, sort: UPDATED_AT, direction: DESC}) {
    count
    performers { id name aliases gender updated deleted }
  }
}
"""


def sync_catalog_if_due(conn: db.Connection, *, interval_seconds: int = 86400) -> tuple[int, int] | None:
    """Refresh at most daily; missing credentials or failed fetch keeps cache usable."""
    if not (os.getenv("STASHDB_API_KEY") or os.getenv("STASH_API_KEY")):
        return None
    key = "performer_catalog_stashdb_synced_at"
    row = conn.execute("SELECT value FROM meta WHERE key=%s", (key,)).fetchone()
    try:
        last = int(row[0]) if row else 0
    except (TypeError, ValueError):
        last = 0
    now = int(time.time())
    if now - last < interval_seconds:
        return None
    people, aliases = sync_catalog(conn)
    with conn:
        conn.execute("""
            INSERT INTO meta(key,value) VALUES(%s,%s)
            ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value
        """, (key, str(now)))
    return people, aliases


def _post(payload, token: str):
    import httpx
    response = request_with_host_limit(
        httpx, "post", ENDPOINT, json=payload, timeout=30,
        headers={"ApiKey": token, "Accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


def _fetch_catalog_records(request, post, api_key: str, per_page: int, pause: float) -> list:
    """Collect every performer row across pages, pausing between requests."""
    records = []
    page = 1
    expected = None
    while expected is None or len(records) < expected:
        page_rows, expected = _fetch_catalog_page(request, post, api_key, page, per_page)
        records.extend(row for row in page_rows if isinstance(row, dict) and row.get("id") and row.get("name"))
        if not page_rows or len(records) >= expected:
            break
        page += 1
        if pause:
            time.sleep(pause)
    return records


def _fetch_catalog_page(request, post, api_key: str, page: int, per_page: int) -> tuple[list, int]:
    """Return (performer rows on this page, total count reported by StashDB)."""
    payload = {"query": QUERY, "variables": {"page": page, "perPage": per_page}}
    try:
        result = request(payload, api_key) if post is None else request(
            ENDPOINT, json=payload, headers={"ApiKey": api_key}, timeout=30)
    except TypeError:
        result = request(payload)
    if hasattr(result, "json"):
        result = result.json()
    if not isinstance(result, dict) or result.get("errors"):
        raise RuntimeError("StashDB returned an invalid performer catalogue page")
    data = result.get("data", {}).get("queryPerformers", {})
    page_rows = data.get("performers")
    if not isinstance(page_rows, list):
        raise RuntimeError("StashDB response did not include performers")
    return page_rows, int(data.get("count") or 0)


def sync_catalog(conn: db.Connection, *, token: str | None = None,
                 post: Callable | None = None, per_page: int = 100,
                 pause: float = 0.25) -> tuple[int, int]:
    """Fetch every male-performer catalogue page before publishing the records.

    Returns (performers, aliases). Existing local rows survive source errors.
    """
    api_key = (token or os.getenv("STASHDB_API_KEY", "").strip()
               or os.getenv("STASH_API_KEY", "").strip())
    if not api_key and post is None:
        raise RuntimeError("STASHDB_API_KEY is required to import the catalogue")
    request = post or _post
    records = _fetch_catalog_records(request, post, api_key, per_page, pause)

    alias_count = 0
    with conn:
        for record in records:
            performer_id = "stashdb:" + str(record["id"])
            name = str(record["name"]).strip()
            if not name:
                continue
            conn.execute("""
                INSERT INTO performer_catalog(id,source,source_id,name,updated_at,active)
                VALUES (%s,'stashdb',%s,%s,%s,%s)
                ON CONFLICT(source,source_id) DO UPDATE SET
                    name=EXCLUDED.name, updated_at=EXCLUDED.updated_at,
                    active=EXCLUDED.active
            """, (performer_id, str(record["id"]), name, _updated_epoch(record.get("updated")),
                  not bool(record.get("deleted"))))
            conn.execute("DELETE FROM performer_catalog_aliases WHERE performer_id=%s AND origin='stashdb'",
                         (performer_id,))
            for alias in dict.fromkeys([name, *(record.get("aliases") or [])]):
                alias = str(alias or "").strip()
                normalized = normalize_name(alias)
                if not normalized:
                    continue
                conn.execute("""
                    INSERT INTO performer_catalog_aliases(performer_id,alias,normalized,origin,approved)
                    VALUES (%s,%s,%s,'stashdb',TRUE)
                    ON CONFLICT(performer_id,normalized,origin) DO UPDATE SET
                        alias=EXCLUDED.alias,approved=TRUE
                """, (performer_id, alias, normalized))
                alias_count += 1
    logger.info("StashDB catalogue imported: %d people, %d alias spellings", len(records), alias_count)
    return len(records), alias_count
