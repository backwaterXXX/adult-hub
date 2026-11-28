"""Catalog taxonomy queries for categories, studios, and tags."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from resolver.catalog_cache import (
    catalog_result_cache_get as _catalog_result_cache_get,
    catalog_result_cache_key as _catalog_result_cache_key,
    catalog_result_cache_put as _catalog_result_cache_put,
)

if TYPE_CHECKING:
    from resolver.client_catalog import PaginatedTaxonomy


def _catalog():
    """Resolve shared catalog helpers lazily to avoid an import cycle."""
    from resolver import client_catalog
    return client_catalog


def _taxonomy_total_from_rows(
    rows: Sequence[Any],
    offset: int,
    conn: Any,
    count_sql: str,
    count_params: Sequence[Any],
) -> int:
    """Read the windowed total, falling back to a count for pages past the end."""
    if rows:
        first = rows[0]
        return int(first[2] if isinstance(first, (list, tuple)) else first["total_count"])
    if offset > 0:
        total_row = conn.execute(count_sql, count_params).fetchone()
        return int(total_row[0] if total_row else 0)
    return 0


def query_catalog_categories(
    conn: Any,
    *,
    q: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> PaginatedTaxonomy:
    """List video categories, reusing the shared snapshot for one TTL."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    disabled_set = _catalog()._extract_disabled_channel_ids()
    key = _catalog_result_cache_key(
        conn, "categories", q or "", offset, limit, tuple(sorted(disabled_set))
    )
    cached = _catalog_result_cache_get(key)
    if cached is not None:
        return cached
    result = _query_catalog_categories_uncached(conn, q=q, offset=offset, limit=limit)
    _catalog_result_cache_put(key, result)
    return result


def _query_catalog_categories_uncached(
    conn: Any,
    *,
    q: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> PaginatedTaxonomy:
    """List video categories with distinct visible movie counts and deterministic ordering."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    tables = _catalog()._get_existing_tables(conn)
    columns = _catalog()._get_table_columns(conn, "videos")
    has_overrides = "video_metadata_overrides" in tables
    join_overrides = " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id" if has_overrides else ""

    disabled_set = _catalog()._extract_disabled_channel_ids()
    conditions, params = _catalog()._build_visibility_conditions(
        tables, columns, disabled_channel_ids=disabled_set or None, table_alias="v"
    )
    where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""

    cat_expr = _catalog()._effective_value("categories", columns, has_overrides)

    and_conds = []
    and_params = []
    if q and q.strip():
        and_conds.append("trim(cat.name) ILIKE %s ESCAPE '\\'")
        and_params.append(f"%{_catalog()._escape_like_pattern(q.strip())}%")

    and_sql = (" AND " + " AND ".join(and_conds)) if and_conds else ""

    count_sql = f"""
        SELECT COUNT(DISTINCT trim(cat.name))
        FROM (
            SELECT jsonb_array_elements_text(
                CASE WHEN jsonb_typeof({cat_expr}) = 'array' THEN {cat_expr} ELSE '[]'::jsonb END
            ) AS name
            FROM videos v{join_overrides}
            {where_sql}
        ) cat
        WHERE trim(cat.name) <> '' {and_sql}
    """
    # One unnest/group pass returns the page and the total together; the
    # separate ``COUNT(DISTINCT ...)`` query doubled the work for every call.
    items_sql = f"""
        SELECT name, item_count, count(*) OVER () AS total_count
        FROM (
            SELECT
                trim(cat.name) AS name,
                COUNT(DISTINCT cat.id) AS item_count
            FROM (
                SELECT
                    v.id,
                    jsonb_array_elements_text(
                        CASE WHEN jsonb_typeof({cat_expr}) = 'array' THEN {cat_expr} ELSE '[]'::jsonb END
                    ) AS name
                FROM videos v{join_overrides}
                {where_sql}
            ) cat
            WHERE trim(cat.name) <> '' {and_sql}
            GROUP BY trim(cat.name)
        ) grouped
        ORDER BY item_count DESC, name ASC
        LIMIT %s OFFSET %s
    """
    query_params = params + and_params + [limit, offset]
    rows = conn.execute(items_sql, query_params).fetchall()

    items = [
        _catalog().TaxonomyItem(
            name=str(r[0] if isinstance(r, (list, tuple)) else r["name"]).strip(),
            count=int(r[1] if isinstance(r, (list, tuple)) else r["item_count"]),
        )
        for r in rows
    ]
    total_count = _taxonomy_total_from_rows(rows, offset, conn, count_sql, params + and_params)

    return _catalog().PaginatedTaxonomy(
        items=items,
        total_count=total_count,
        offset=offset,
        limit=limit,
        has_more=(offset + len(items) < total_count),
    )


def query_catalog_studios(
    conn: Any,
    *,
    q: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> PaginatedTaxonomy:
    """List studios, reusing the shared snapshot for one TTL."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    disabled_set = _catalog()._extract_disabled_channel_ids()
    key = _catalog_result_cache_key(
        conn, "studios", q or "", offset, limit, tuple(sorted(disabled_set))
    )
    cached = _catalog_result_cache_get(key)
    if cached is not None:
        return cached
    result = _query_catalog_studios_uncached(conn, q=q, offset=offset, limit=limit)
    _catalog_result_cache_put(key, result)
    return result


def _query_catalog_studios_uncached(
    conn: Any,
    *,
    q: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> PaginatedTaxonomy:
    """List studios with distinct visible movie counts and deterministic ordering."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    tables = _catalog()._get_existing_tables(conn)
    columns = _catalog()._get_table_columns(conn, "videos")
    has_overrides = "video_metadata_overrides" in tables
    join_overrides = " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id" if has_overrides else ""

    disabled_set = _catalog()._extract_disabled_channel_ids()
    conditions, params = _catalog()._build_visibility_conditions(
        tables, columns, disabled_channel_ids=disabled_set or None, table_alias="v"
    )
    studio_expr = _catalog()._effective_value("studio", columns, has_overrides)

    conditions.append(f"{studio_expr} IS NOT NULL AND trim({studio_expr}::text) <> ''")

    if q and q.strip():
        conditions.append(f"{studio_expr}::text ILIKE %s ESCAPE '\\'")
        params.append(f"%{_catalog()._escape_like_pattern(q.strip())}%")

    where_sql = "WHERE " + " AND ".join(conditions)

    count_sql = f"""
        SELECT COUNT(DISTINCT trim({studio_expr}::text))
        FROM videos v{join_overrides}
        {where_sql}
    """
    items_sql = f"""
        SELECT name, item_count, count(*) OVER () AS total_count
        FROM (
            SELECT
                trim({studio_expr}::text) AS name,
                COUNT(DISTINCT v.id) AS item_count
            FROM videos v{join_overrides}
            {where_sql}
            GROUP BY trim({studio_expr}::text)
        ) grouped
        ORDER BY item_count DESC, name ASC
        LIMIT %s OFFSET %s
    """
    query_params = params + [limit, offset]
    rows = conn.execute(items_sql, query_params).fetchall()

    items = [
        _catalog().TaxonomyItem(
            name=str(r[0] if isinstance(r, (list, tuple)) else r["name"]).strip(),
            count=int(r[1] if isinstance(r, (list, tuple)) else r["item_count"]),
        )
        for r in rows
    ]
    total_count = _taxonomy_total_from_rows(rows, offset, conn, count_sql, params)

    return _catalog().PaginatedTaxonomy(
        items=items,
        total_count=total_count,
        offset=offset,
        limit=limit,
        has_more=(offset + len(items) < total_count),
    )


def query_catalog_tags(
    conn: Any,
    *,
    q: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> PaginatedTaxonomy:
    """List tags, reusing the shared snapshot for one TTL."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    disabled_set = _catalog()._extract_disabled_channel_ids()
    key = _catalog_result_cache_key(
        conn, "tags", q or "", offset, limit, tuple(sorted(disabled_set))
    )
    cached = _catalog_result_cache_get(key)
    if cached is not None:
        return cached
    result = _query_catalog_tags_uncached(conn, q=q, offset=offset, limit=limit)
    _catalog_result_cache_put(key, result)
    return result


def _query_catalog_tags_uncached(
    conn: Any,
    *,
    q: str | None = None,
    offset: int = 0,
    limit: int = 100,
) -> PaginatedTaxonomy:
    """List tags with distinct visible movie counts and deterministic ordering."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    tables = _catalog()._get_existing_tables(conn)
    columns = _catalog()._get_table_columns(conn, "videos")
    has_overrides = "video_metadata_overrides" in tables
    join_overrides = " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id" if has_overrides else ""

    disabled_set = _catalog()._extract_disabled_channel_ids()
    conditions, params = _catalog()._build_visibility_conditions(
        tables, columns, disabled_channel_ids=disabled_set or None, table_alias="v"
    )
    where_sql = "WHERE " + " AND ".join(conditions) if conditions else ""

    tags_expr = _catalog()._effective_value("tags", columns, has_overrides)

    and_conds = []
    and_params = []
    if q and q.strip():
        and_conds.append("trim(tag.name) ILIKE %s ESCAPE '\\'")
        and_params.append(f"%{_catalog()._escape_like_pattern(q.strip())}%")

    and_sql = (" AND " + " AND ".join(and_conds)) if and_conds else ""

    count_sql = f"""
        SELECT COUNT(DISTINCT trim(tag.name))
        FROM (
            SELECT jsonb_array_elements_text(
                CASE WHEN jsonb_typeof({tags_expr}) = 'array' THEN {tags_expr} ELSE '[]'::jsonb END
            ) AS name
            FROM videos v{join_overrides}
            {where_sql}
        ) tag
        WHERE trim(tag.name) <> '' {and_sql}
    """
    items_sql = f"""
        SELECT name, item_count, count(*) OVER () AS total_count
        FROM (
            SELECT
                trim(tag.name) AS name,
                COUNT(DISTINCT tag.id) AS item_count
            FROM (
                SELECT
                    v.id,
                    jsonb_array_elements_text(
                        CASE WHEN jsonb_typeof({tags_expr}) = 'array' THEN {tags_expr} ELSE '[]'::jsonb END
                    ) AS name
                FROM videos v{join_overrides}
                {where_sql}
            ) tag
            WHERE trim(tag.name) <> '' {and_sql}
            GROUP BY trim(tag.name)
        ) grouped
        ORDER BY item_count DESC, name ASC
        LIMIT %s OFFSET %s
    """
    query_params = params + and_params + [limit, offset]
    rows = conn.execute(items_sql, query_params).fetchall()

    items = [
        _catalog().TaxonomyItem(
            name=str(r[0] if isinstance(r, (list, tuple)) else r["name"]).strip(),
            count=int(r[1] if isinstance(r, (list, tuple)) else r["item_count"]),
        )
        for r in rows
    ]
    total_count = _taxonomy_total_from_rows(rows, offset, conn, count_sql, params + and_params)

    return _catalog().PaginatedTaxonomy(
        items=items,
        total_count=total_count,
        offset=offset,
        limit=limit,
        has_more=(offset + len(items) < total_count),
    )


