"""Read-only PostgreSQL queries and projections for the Admin catalog."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

from common import db
from admin_catalog_serialization import _local_file_status, _resolve_local_artwork_path, _serialize_video
from admin_catalog_pagination import (
    cursor_condition as _cursor_condition_impl,
    cursor_value_expression as _cursor_value_expression_impl,
    encode_catalog_cursor as _encode_catalog_cursor,
    reverse_sort_clause as _reverse_sort_clause_impl,
    sort_clause as _sort_clause_impl,
    sort_value_expression as _sort_value_expression_impl,
)


_MISSING_FIELDS = {"title", "performers", "studio", "plot", "thumbnail"}
_VIDEO_OPTIONAL_COLUMNS = {
    "provider", "title", "duration", "file_size_bytes", "thumbnail", "performers", "categories",
    "tags", "playable", "studio", "plot", "date_released", "first_seen", "last_seen",
    "scene_match_source", "scene_match_external_id", "scene_match_url",
}


class CatalogUnavailable(Exception):
    """The database or required catalog table cannot serve this request."""


def _missing_local_artwork_ids(conn, media_root: str | os.PathLike[str]) -> list[str] | None:
    """Return videos whose registered stream has no sibling artwork file."""
    try:
        root = Path(media_root).resolve()
        if not stat.S_ISDIR(root.stat().st_mode):
            return None
        with os.scandir(root):
            pass
        rows = conn.execute("SELECT video_id,file_path FROM synced_files").fetchall()
    except (OSError, TypeError, ValueError):
        return None

    missing: set[str] = set()
    for row in rows:
        video_id, raw_path = row[0], row[1]
        if (not isinstance(raw_path, str) or not raw_path or raw_path in {".", ".."}
                or "/" in raw_path or "\\" in raw_path
                or any(ord(char) < 32 for char in raw_path)
                or not raw_path.lower().endswith(".strm")):
            continue
        try:
            artwork_info = (root / f"{raw_path[:-5]}.jpg").lstat()
        except FileNotFoundError:
            missing.add(str(video_id))
        except OSError:
            continue
        else:
            if stat.S_ISLNK(artwork_info.st_mode) or not stat.S_ISREG(artwork_info.st_mode):
                missing.add(str(video_id))
    return sorted(missing)




def _missing_clause(field: str, columns: set[str]) -> str:
    if field not in columns:
        return "TRUE"
    if field in {"performers", "tags"}:
        return f"(v.{field} IS NULL OR v.{field} = '[]'::jsonb)"
    return f"NULLIF(BTRIM(COALESCE(v.{field}::text, '')), '') IS NULL"


def _effective_text(field: str, columns: set[str], has_overrides: bool) -> str:
    source = f"v.{field}" if field in columns else "NULL"
    if has_overrides and field in {"title", "plot"}:
        return f"CASE WHEN COALESCE(mo.fields, '{{}}'::jsonb) ? '{field}' THEN mo.fields->>'{field}' ELSE {source}::text END"
    return f"{source}::text"


def _effective_value(field: str, columns: set[str], has_overrides: bool) -> str:
    source = f"v.{field}" if field in columns else "NULL"
    if not has_overrides:
        return source
    if field in {"performers", "tags"}:
        return f"CASE WHEN COALESCE(mo.fields, '{{}}'::jsonb) ? '{field}' THEN mo.fields->'{field}' ELSE {source} END"
    if field in {"title", "plot", "studio", "date_released"}:
        return f"CASE WHEN COALESCE(mo.fields, '{{}}'::jsonb) ? '{field}' THEN mo.fields->>'{field}' ELSE {source}::text END"
    return source


def _missing_effective_clause(field: str, columns: set[str], has_overrides: bool) -> str:
    if field in {"title", "plot", "studio", "date_released"}:
        return f"NULLIF(BTRIM(COALESCE({_effective_value(field, columns, has_overrides)}::text, '')), '') IS NULL"
    if field in {"performers", "tags"}:
        expr = _effective_value(field, columns, has_overrides)
        return f"({_effective_value(field, columns, has_overrides)} IS NULL OR {expr} = '[]'::jsonb)"
    return _missing_clause(field, columns)


def _availability_clauses(columns: set[str], has_dead_streams: bool) -> dict[str, str]:
    dead = "d.video_id IS NOT NULL" if has_dead_streams else "FALSE"
    playable = "v.playable" if "playable" in columns else "NULL"
    return {
        "available": f"({playable} = 1 AND NOT ({dead}))",
        "unavailable": f"({playable} = 0 OR ({dead}))",
        "unknown": f"({playable} IS NULL AND NOT ({dead}))",
    }


def _tables_and_columns(conn) -> tuple[set[str], set[str]]:
    try:
        tables = set(db.table_names(conn))
        if "videos" not in tables:
            raise CatalogUnavailable("The catalog table is unavailable.")
        columns = set(db.table_columns(conn, "videos"))
        if "id" not in columns:
            raise CatalogUnavailable("The catalog table is unavailable.")
        return tables, columns
    except CatalogUnavailable:
        raise
    except Exception as exc:
        raise CatalogUnavailable from exc


def _select_video_fields(columns: set[str], has_overrides: bool = False) -> str:
    expressions = ["v.id"]
    for col in sorted(_VIDEO_OPTIONAL_COLUMNS):
        if col in {"title", "plot", "studio", "date_released", "performers", "tags"}:
            expressions.append(f"{_effective_value(col, columns, has_overrides)} AS {col}")
        else:
            expressions.append(f"v.{col}" if col in columns else f"NULL AS {col}")
    expressions.extend([
        ("v.title" if "title" in columns else "NULL") + " AS source_title",
        ("v.plot" if "plot" in columns else "NULL") + " AS source_plot",
        ("v.studio" if "studio" in columns else "NULL") + " AS source_studio",
        ("v.performers" if "performers" in columns else "NULL") + " AS source_performers",
        ("v.tags" if "tags" in columns else "NULL") + " AS source_tags",
        ("v.date_released" if "date_released" in columns else "NULL") + " AS source_date_released",
        "COALESCE(mo.fields, '{}'::jsonb) AS metadata_overrides" if has_overrides else "'{}'::jsonb AS metadata_overrides",
        "COALESCE(mo.revision, 0) AS metadata_revision" if has_overrides else "0 AS metadata_revision",
    ])
    return ", ".join(expressions)




def _sort_clause(sort: str, columns: set[str], has_overrides: bool = False) -> str:
    return _sort_clause_impl(sort, columns, has_overrides, effective_text_fn=_effective_text)


def _sort_value_expression(sort: str, columns: set[str], has_overrides: bool) -> str:
    return _sort_value_expression_impl(
        sort, columns, has_overrides, effective_text_fn=_effective_text,
    )


def _cursor_value_expression(sort: str, columns: set[str], has_overrides: bool) -> str:
    return _cursor_value_expression_impl(
        sort, columns, has_overrides, effective_text_fn=_effective_text,
    )


def _cursor_condition(sort: str, columns: set[str], has_overrides: bool,
                      cursor: dict[str, Any], direction: str) -> tuple[str, list[Any]]:
    return _cursor_condition_impl(
        sort, columns, has_overrides, cursor, direction,
        effective_text_fn=_effective_text,
    )


def _reverse_sort_clause(sort: str, columns: set[str], has_overrides: bool) -> str:
    return _reverse_sort_clause_impl(
        sort, columns, has_overrides, effective_text_fn=_effective_text,
    )


def _run_catalog_query(
    db_url: str | None,
    query: dict[str, Any],
    media_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    try:
        conn = db.connect(db_url)
    except Exception as exc:
        raise CatalogUnavailable from exc
    conn.row_factory = db.Record
    try:
        tables, columns = _tables_and_columns(conn)
        has_files = "synced_files" in tables
        has_dead = "dead_streams" in tables
        has_overrides = "video_metadata_overrides" in tables
        missing_local_artwork = (
            _missing_local_artwork_ids(conn, media_root)
            if "thumbnail" in query["missing"] and has_files and media_root is not None
            else None
        )
        params: list[Any] = []
        conditions: list[str] = []

        q = query["q"]
        if q:
            searchable = ["COALESCE(v.id, '')"]
            searchable.extend(
                f"COALESCE({_effective_value(name, columns, has_overrides)}::text, '')" if name in {"title", "plot", "performers", "studio", "tags"}
                else f"COALESCE(v.{name}::text, '')"
                for name in ("title", "performers", "studio", "plot", "tags") if name in columns
            )
            if searchable:
                conditions.append("(" + " OR ".join(f"{expr} ILIKE %s" for expr in searchable) + ")")
                params.extend([f"%{q}%"] * len(searchable))

        channel_id = query["channel_id"]
        if channel_id:
            # IDs are composite strings. Exact prefix matching avoids LIKE
            # wildcard interpretation and keeps studio as production metadata.
            conditions.append("left(v.id, length(%s)) = %s")
            prefix = f"telegram:{channel_id}:"
            params.extend([prefix, prefix])

        for field in ("studio",):
            value = query[field]
            if value:
                if field not in columns:
                    conditions.append("FALSE")
                else:
                    conditions.append(f"COALESCE({_effective_value(field, columns, has_overrides)}::text, '') ILIKE %s")
                    params.append(f"%{value}%")

        performer = query["performer"]
        if performer:
            if "performers" not in columns:
                conditions.append("FALSE")
            else:
                conditions.append(f"COALESCE({_effective_value('performers', columns, has_overrides)}::text, '') ILIKE %s")
                params.append(f"%{performer}%")

        tag = query["tag"]
        if tag:
            if "tags" not in columns:
                conditions.append("FALSE")
            else:
                tag_values = _effective_value("tags", columns, has_overrides)
                conditions.append(f"EXISTS (SELECT 1 FROM jsonb_array_elements_text(CASE "
                                  f"WHEN jsonb_typeof({tag_values}) = 'array' THEN {tag_values} ELSE '[]'::jsonb END) t(value) "
                                  "WHERE t.value ILIKE %s)")
                params.append(f"%{tag}%")

        availability = query["availability"]
        if availability != "all":
            condition = _availability_clauses(columns, has_dead).get(availability)
            if condition:
                conditions.append(condition)

        for field in query["missing"]:
            if field == "thumbnail" and missing_local_artwork is not None:
                conditions.append(
                    f"({_missing_effective_clause(field, columns, has_overrides)} OR v.id = ANY(%s))"
                )
                params.append(missing_local_artwork)
            else:
                conditions.append(_missing_effective_clause(field, columns, has_overrides))

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        join_dead = " LEFT JOIN dead_streams d ON d.video_id = v.id" if has_dead else ""
        join_overrides = " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id" if has_overrides else ""
        cursor_data = query.get("cursor_data")
        cursor_direction = query.get("cursor_direction", "after")
        total_exact = bool(query.get("total_exact", True))
        total = None
        if total_exact:
            count_row = conn.execute(
                f"SELECT count(*) AS total FROM videos v{join_overrides}{join_dead}{where}", params
            ).fetchone()
            total = int(count_row["total"] if hasattr(count_row, "keys") else count_row[0])

        row_conditions = list(conditions)
        row_params = list(params)
        if cursor_data is not None:
            cursor_filter, cursor_params = _cursor_condition(
                query["sort"], columns, has_overrides, cursor_data, cursor_direction,
            )
            row_conditions.append(cursor_filter)
            row_params.extend(cursor_params)
        row_where = " WHERE " + " AND ".join(row_conditions) if row_conditions else ""
        cursor_mode = cursor_data is not None or not total_exact
        fields = _select_video_fields(columns, has_overrides)
        if cursor_mode:
            fields += ", " + _cursor_value_expression(query["sort"], columns, has_overrides) + " AS __sort_value"
        file_join = (
            " LEFT JOIN LATERAL (SELECT count(*) AS file_count, "
            "array_agg(DISTINCT category ORDER BY category) FILTER (WHERE category IS NOT NULL) AS file_categories "
            "FROM synced_files WHERE video_id = v.id) sf ON TRUE"
            if has_files else ""
        )
        file_fields = (", COALESCE(sf.file_count, 0) AS file_count, sf.file_categories AS file_categories"
                       if has_files else ", 0 AS file_count, NULL AS file_categories")
        has_local = "(COALESCE(sf.file_count, 0) > 0) AS has_local_files" if has_files else "FALSE AS has_local_files"
        dead_field = "(d.video_id IS NOT NULL) AS dead_stream" if has_dead else "FALSE AS dead_stream"
        unavailable_fields = (", d.reason AS unavailable_reason, d.detected_at AS unavailable_detected_at"
                              if has_dead else ", NULL AS unavailable_reason, NULL AS unavailable_detected_at")
        order = (
            _reverse_sort_clause(query["sort"], columns, has_overrides)
            if cursor_data is not None and cursor_direction == "before"
            else _sort_clause(query["sort"], columns, has_overrides)
        )
        rows = conn.execute(
            f"SELECT {fields}{file_fields}, {has_local}, {dead_field}{unavailable_fields} FROM videos v{join_overrides}{join_dead}{file_join}{row_where} "
            f"ORDER BY {order} LIMIT %s OFFSET %s",
            [*row_params, query["limit"] + (1 if cursor_mode else 0),
             0 if cursor_data is not None else query["offset"]],
        ).fetchall()
        has_extra = len(rows) > query["limit"] if cursor_mode else False
        has_previous = False
        if cursor_mode:
            rows = rows[:query["limit"]]
            if cursor_data is not None and cursor_direction == "before":
                rows.reverse()
                has_previous = has_extra
                has_more = bool(rows)
            else:
                has_previous = bool(rows) and (cursor_data is not None or query["offset"] > 0)
                has_more = has_extra
        else:
            has_more = query["offset"] + len(rows) < total
        items = [_serialize_video(row, detail=False) for row in rows]
        next_cursor = _encode_catalog_cursor(query["sort"], rows[-1]) if cursor_mode and has_more and rows else None
        previous_cursor = _encode_catalog_cursor(query["sort"], rows[0]) if cursor_mode and has_previous and rows else None
        return {
            "ok": True,
            "items": items,
            "page": {
                "limit": query["limit"], "offset": query["offset"],
                "total": total, "total_exact": total_exact, "has_more": has_more,
                "has_previous": has_previous, "next_cursor": next_cursor,
                "previous_cursor": previous_cursor,
            },
        }
    except CatalogUnavailable:
        raise
    except Exception as exc:
        raise CatalogUnavailable from exc
    finally:
        conn.close()









def _get_video(db_url: str | None, video_id: str, media_root: str | os.PathLike[str] = "/media/adult") -> dict[str, Any] | None:
    try:
        conn = db.connect(db_url)
    except Exception as exc:
        raise CatalogUnavailable from exc
    conn.row_factory = db.Record
    try:
        tables, columns = _tables_and_columns(conn)
        has_files = "synced_files" in tables
        has_overrides = "video_metadata_overrides" in tables
        fields = _select_video_fields(columns, has_overrides)
        file_join = (
            " LEFT JOIN LATERAL (SELECT count(*) AS file_count, "
            "array_agg(DISTINCT category ORDER BY category) FILTER (WHERE category IS NOT NULL) AS file_categories "
            "FROM synced_files WHERE video_id = v.id) sf ON TRUE"
            if has_files else ""
        )
        file_fields = (", COALESCE(sf.file_count, 0) AS file_count, sf.file_categories AS file_categories, "
                       "(COALESCE(sf.file_count, 0) > 0) AS has_local_files" if has_files
                       else ", 0 AS file_count, NULL AS file_categories, FALSE AS has_local_files")
        join_dead = " LEFT JOIN dead_streams d ON d.video_id = v.id" if "dead_streams" in tables else ""
        join_overrides = " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id" if has_overrides else ""
        dead_field = "(d.video_id IS NOT NULL) AS dead_stream" if "dead_streams" in tables else "FALSE AS dead_stream"
        unavailable_fields = (", d.reason AS unavailable_reason, d.detected_at AS unavailable_detected_at"
                              if "dead_streams" in tables else ", NULL AS unavailable_reason, NULL AS unavailable_detected_at")
        row = conn.execute(
            f"SELECT {fields}{file_fields}, {dead_field}{unavailable_fields} FROM videos v{join_overrides}{file_join}{join_dead} WHERE v.id = %s",
            (video_id,),
        ).fetchone()
        if row is None:
            return None
        registered_files = []
        if has_files:
            registered_files = [entry["file_path"] if hasattr(entry, "keys") else entry[0]
                                for entry in conn.execute(
                                    "SELECT file_path FROM synced_files WHERE video_id = %s ORDER BY file_path",
                                    (video_id,),
                                ).fetchall()]
        local_files = _local_file_status(registered_files, media_root)
        video = _serialize_video(row, detail=True, local_files=local_files)
        fingerprints = {"oshash": None, "phash": None}
        if "video_fingerprints" in tables:
            for algorithm, fingerprint in conn.execute(
                "SELECT algorithm, fingerprint FROM video_fingerprints "
                "WHERE video_id=%s AND algorithm IN ('OSHASH','PHASH')", (video_id,)
            ).fetchall():
                key = str(algorithm or "").lower()
                value = str(fingerprint or "").lower()
                if key in fingerprints and re.fullmatch(r"[0-9a-f]{16}", value):
                    fingerprints[key] = value
        video.update(fingerprints)
        if "video_sources" in tables:
            source_rows = conn.execute(
                "SELECT provider, provider_ref, channel_id, priority, is_active "
                "FROM video_sources WHERE video_id = %s ORDER BY is_active DESC, priority, id",
                (video_id,),
            ).fetchall()
            video["sources"] = [{
                "provider": source["provider"],
                "provider_ref": source["provider_ref"],
                "channel_id": source["channel_id"],
                "priority": int(source["priority"] or 0),
                "active": bool(source["is_active"]),
            } for source in source_rows]
        return video
    except CatalogUnavailable:
        raise
    except Exception as exc:
        raise CatalogUnavailable from exc
    finally:
        conn.close()


def _get_video_artwork_path(db_url: str | None, video_id: str, media_root: str | os.PathLike[str] = "/media/adult") -> Path | None:
    """Find the local artwork file Path for video_id if it exists, else None."""
    if not video_id or not isinstance(video_id, str):
        return None
    try:
        conn = db.connect(db_url)
    except Exception:
        return None
    try:
        tables = db.table_names(conn)
        if "synced_files" not in tables:
            return None
        rows = conn.execute(
            "SELECT file_path FROM synced_files WHERE video_id = %s ORDER BY file_path",
            (video_id,),
        ).fetchall()
        file_names = [entry["file_path"] if hasattr(entry, "keys") else entry[0] for entry in rows]
        return _resolve_local_artwork_path(file_names, media_root)
    except Exception:
        return None
    finally:
        conn.close()


def _get_target_artwork_path(db_url: str | None, video_id: str, media_root: str | os.PathLike[str] = "/media/adult") -> Path | None:
    """Find the destination Path where artwork for video_id should be saved."""
    if not video_id or not isinstance(video_id, str):
        return None
    try:
        conn = db.connect(db_url)
    except Exception:
        return None
    try:
        tables = db.table_names(conn)
        if "synced_files" not in tables:
            return None
        rows = conn.execute(
            "SELECT file_path FROM synced_files WHERE video_id = %s ORDER BY file_path",
            (video_id,),
        ).fetchall()
        for row in rows:
            raw_name = row["file_path"] if hasattr(row, "keys") else row[0]
            if isinstance(raw_name, str) and raw_name.lower().endswith(".strm"):
                stem = raw_name[:-5]
                root = Path(media_root).resolve()
                candidate = (root / f"{stem}.jpg").resolve()
                if candidate.parent == root:
                    return candidate
        return None
    except Exception:
        return None
    finally:
        conn.close()
