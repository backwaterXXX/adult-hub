"""Short-lived catalog result and exact-count caches."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from typing import Any, Sequence


_COUNT_CACHE_TTL_SECONDS = float(os.getenv("CATALOG_COUNT_CACHE_TTL_SECONDS", "30"))
_COUNT_CACHE_MAX_ENTRIES = 512
_COUNT_CACHE: dict[tuple[str, str, tuple], tuple[float, int]] = {}
_COUNT_CACHE_LOCK = threading.Lock()

# Taxonomy (categories, studios, tags) and channel counts are catalog-wide: every
# signed-in user sees the same list, and the sync process only rewrites them
# between passes. Rebuilding them unnest the whole catalog and count channel
# membership, so a short shared snapshot keeps repeat screen loads off the CPU.
_CATALOG_RESULT_CACHE_TTL_SECONDS = float(
    os.getenv("CATALOG_RESULT_CACHE_TTL_SECONDS", "60")
)
_CATALOG_RESULT_CACHE_MAX_ENTRIES = 256
_CATALOG_RESULT_CACHE: dict[tuple, tuple[float, Any]] = {}
_CATALOG_RESULT_CACHE_LOCK = threading.Lock()


def catalog_result_cache_key(conn: Any, kind: str, *parts: Any) -> tuple:
    """Identify one catalog-wide read by database, kind and request shape."""
    database = str(getattr(getattr(conn, "info", None), "dbname", "") or "")
    return (database, kind, *parts)


def catalog_result_cache_get(key: tuple) -> Any | None:
    """Return a cached catalog read while it is younger than the TTL."""
    ttl = _CATALOG_RESULT_CACHE_TTL_SECONDS
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _CATALOG_RESULT_CACHE_LOCK:
        cached = _CATALOG_RESULT_CACHE.get(key)
        if cached is None or now - cached[0] >= ttl:
            return None
        value = cached[1]
    from resolver.client_catalog import PaginatedTaxonomy

    if isinstance(value, PaginatedTaxonomy):
        return replace(value, items=list(value.items))
    if isinstance(value, dict):
        return dict(value)
    return value


def catalog_result_cache_put(key: tuple, value: Any) -> None:
    """Remember one catalog-wide read for the next request."""
    if _CATALOG_RESULT_CACHE_TTL_SECONDS <= 0:
        return
    with _CATALOG_RESULT_CACHE_LOCK:
        if len(_CATALOG_RESULT_CACHE) >= _CATALOG_RESULT_CACHE_MAX_ENTRIES:
            _CATALOG_RESULT_CACHE.clear()
        _CATALOG_RESULT_CACHE[key] = (time.monotonic(), value)


def _freeze_params(params: Sequence[Any]) -> tuple:
    """Return a hashable form of the query parameters for the cache key."""
    frozen = []
    for value in params:
        if isinstance(value, (list, tuple, set)):
            frozen.append(tuple(value))
        else:
            frozen.append(value)
    return tuple(frozen)


def catalog_total(conn: Any, sql: str, params: Sequence[Any], *, cache: bool = True) -> int:
    """Return the exact row count for one filter set, reusing a short-lived result.

    The page itself is index-ordered while ``count(*)`` scans the filtered
    catalog, so the total dominates the request. A client paging through the
    same filter set asks for the same total repeatedly, so a short TTL removes
    that work without changing the contract. Only the total is cached.
    """
    ttl = _COUNT_CACHE_TTL_SECONDS if cache else 0.0
    if ttl <= 0:
        return int(conn.execute(sql, params).fetchone()[0])
    database = str(getattr(getattr(conn, "info", None), "dbname", "") or "")
    key = (database, sql, _freeze_params(params))
    now = time.monotonic()
    with _COUNT_CACHE_LOCK:
        cached = _COUNT_CACHE.get(key)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
    total = int(conn.execute(sql, params).fetchone()[0])
    with _COUNT_CACHE_LOCK:
        if len(_COUNT_CACHE) >= _COUNT_CACHE_MAX_ENTRIES:
            _COUNT_CACHE.clear()
        _COUNT_CACHE[key] = (now, total)
    return total


def clear_query_caches() -> None:
    """Drop cached totals and catalog-wide result snapshots."""
    with _COUNT_CACHE_LOCK:
        _COUNT_CACHE.clear()
    with _CATALOG_RESULT_CACHE_LOCK:
        _CATALOG_RESULT_CACHE.clear()
