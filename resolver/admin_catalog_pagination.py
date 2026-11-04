"""Cursor encoding and SQL ordering helpers for the Admin catalog."""
from __future__ import annotations

import base64
import json
import math
from datetime import datetime, timezone
from typing import Any, Callable


SORTS = {
    "title": "lower(COALESCE(v.title, '')) ASC, v.id ASC",
    "first_seen": "v.first_seen DESC NULLS LAST, v.id ASC",
    "last_seen": "v.last_seen DESC NULLS LAST, v.id ASC",
    "duration": "v.duration DESC NULLS LAST, v.id ASC",
}


def sort_clause(
    sort: str, columns: set[str], has_overrides: bool,
    *, effective_text_fn: Callable[[str, set[str], bool], str],
) -> str:
    field = {"title": "title", "first_seen": "first_seen", "last_seen": "last_seen", "duration": "duration"}[sort]
    if field not in columns:
        return "v.id ASC"
    if field == "title" and has_overrides:
        return f"lower(COALESCE({effective_text_fn('title', columns, True)}, '')), v.id ASC"
    # Keep a stable tie-breaker even when several rows share the same value.
    return SORTS[sort]


def sort_value_expression(
    sort: str, columns: set[str], has_overrides: bool,
    *, effective_text_fn: Callable[[str, set[str], bool], str],
) -> str:
    field = {"title": "title", "first_seen": "first_seen", "last_seen": "last_seen", "duration": "duration"}[sort]
    if field not in columns:
        return "v.id"
    if sort == "title":
        return f"lower(COALESCE({effective_text_fn('title', columns, has_overrides)}, ''))"
    return f"v.{field}"


def cursor_value_expression(
    sort: str, columns: set[str], has_overrides: bool,
    *, effective_text_fn: Callable[[str, set[str], bool], str],
) -> str:
    expression = sort_value_expression(
        sort, columns, has_overrides, effective_text_fn=effective_text_fn,
    )
    field = {"title": "title", "first_seen": "first_seen", "last_seen": "last_seen", "duration": "duration"}[sort]
    if sort in {"first_seen", "last_seen"} and field in columns:
        # Keep microseconds intact; the shared row adapter otherwise normalizes timestamps to epoch seconds.
        return f"to_char({expression} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
    return expression


def decode_catalog_cursor(token: str, sort: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token or len(token) > 8192:
        raise ValueError("Invalid catalog cursor")
    try:
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Invalid catalog cursor") from exc
    if (not isinstance(payload, dict) or set(payload) != {"sort", "value", "id"}
            or payload["sort"] != sort or not isinstance(payload["id"], str)
            or not payload["id"] or len(payload["id"]) > 1000):
        raise ValueError("Invalid catalog cursor")
    _normalize_cursor_value(sort, payload)
    return payload


def _normalize_cursor_value(sort: str, payload: dict) -> None:
    """Check the cursor value for its sort key and store timestamp values as epoch floats."""
    value = payload["value"]
    if sort in {"first_seen", "last_seen"} and value is not None:
        try:
            if type(value) in {int, float} and math.isfinite(value):
                payload["value"] = float(value)
            elif isinstance(value, str):
                instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    instant = instant.replace(tzinfo=timezone.utc)
                payload["value"] = instant.timestamp()
            else:
                raise ValueError
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError("Invalid catalog cursor") from exc
    elif sort == "duration" and value is not None and type(value) is not int:
        raise ValueError("Invalid catalog cursor")
    elif sort == "title" and not isinstance(value, str):
        raise ValueError("Invalid catalog cursor")


def encode_catalog_cursor(sort: str, row) -> str:
    value = row["__sort_value"]
    if isinstance(value, datetime):
        value = value.isoformat()
    raw = json.dumps({"sort": sort, "value": value, "id": str(row["id"])},
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def cursor_condition(
    sort: str, columns: set[str], has_overrides: bool,
    cursor: dict[str, Any], direction: str,
    *, effective_text_fn: Callable[[str, set[str], bool], str],
) -> tuple[str, list[Any]]:
    expression = sort_value_expression(
        sort, columns, has_overrides, effective_text_fn=effective_text_fn,
    )
    value = cursor["value"]
    video_id = cursor["id"]
    # All supported non-title sorts are DESC NULLS LAST; missing columns fall back to id ASC.
    field = {"title": "title", "first_seen": "first_seen", "last_seen": "last_seen", "duration": "duration"}[sort]
    descending = sort != "title" and field in columns
    if descending and value is None:
        if direction == "after":
            return f"({expression} IS NULL AND v.id > %s)", [video_id]
        return f"({expression} IS NOT NULL OR ({expression} IS NULL AND v.id < %s))", [video_id]
    if descending:
        operator, id_operator = ("<", ">") if direction == "after" else (">", "<")
        clause = f"({expression} {operator} %s OR ({expression} = %s AND v.id {id_operator} %s)"
        if direction == "after":
            clause += f" OR {expression} IS NULL"
        return clause + ")", [value, value, video_id]
    operator, id_operator = (">", ">") if direction == "after" else ("<", "<")
    return f"({expression} {operator} %s OR ({expression} = %s AND v.id {id_operator} %s))", [value, value, video_id]


def reverse_sort_clause(
    sort: str, columns: set[str], has_overrides: bool,
    *, effective_text_fn: Callable[[str, set[str], bool], str],
) -> str:
    expression = sort_value_expression(
        sort, columns, has_overrides, effective_text_fn=effective_text_fn,
    )
    field = {"title": "title", "first_seen": "first_seen", "last_seen": "last_seen", "duration": "duration"}[sort]
    if sort != "title" and field in columns:
        return f"{expression} ASC NULLS FIRST, v.id DESC"
    return f"{expression} DESC, v.id DESC"
