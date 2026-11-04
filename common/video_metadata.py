"""Shared rules for source metadata and persistent manual overrides."""

from __future__ import annotations

import hashlib
import json

from psycopg.types.json import Jsonb

from common import db


EDITABLE_TEXT_FIELDS = {
    "title": 500,
    "plot": 10_000,
    "studio": 300,
}
EDITABLE_LIST_FIELDS = {"performers", "tags"}
EDITABLE_DATE_FIELDS = {"date_released"}
EDITABLE_FIELDS = set(EDITABLE_TEXT_FIELDS) | EDITABLE_LIST_FIELDS | EDITABLE_DATE_FIELDS


class MetadataValidationError(ValueError):
    pass


def metadata_source_fingerprint(source: dict) -> str:
    fields = ("title", "plot", "studio", "performers", "tags", "date_released")
    canonical = json.dumps({field: source.get(field) for field in fields}, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_override_patch(patch: object, reset_fields: object = ()) -> tuple[dict[str, str], list[str]]:
    """Validate one explicit patch and reset list without collapsing empty values.

    A present empty string is an intentional override. Resetting a field is a
    separate operation that removes its key so the latest source value wins.
    """
    if not isinstance(patch, dict):
        raise MetadataValidationError("Metadata changes must be an object.")
    if not isinstance(reset_fields, (list, tuple)):
        raise MetadataValidationError("Reset fields must be a list.")
    if any(not isinstance(field, str) for field in reset_fields):
        raise MetadataValidationError("Reset fields must be field names.")
    unknown = (set(patch) | set(reset_fields)) - EDITABLE_FIELDS
    if unknown:
        raise MetadataValidationError("One or more fields cannot be edited.")
    if set(patch) & set(reset_fields):
        raise MetadataValidationError("A field cannot be edited and reset at the same time.")

    normalized: dict[str, object] = {}
    for field, value in patch.items():
        if field in EDITABLE_TEXT_FIELDS:
            normalized[field] = _normalize_text_field(field, value)
        elif field in EDITABLE_LIST_FIELDS:
            normalized[field] = _normalize_list_field(field, value)
        else:
            normalized[field] = _normalize_date_field(value)
    resets = list(dict.fromkeys(reset_fields))
    return normalized, resets


def _normalize_text_field(field: str, value: object) -> str:
    if not isinstance(value, str):
        raise MetadataValidationError(f"{field} must be text.")
    value = value.strip()
    if len(value) > EDITABLE_TEXT_FIELDS[field]:
        raise MetadataValidationError(f"{field} is too long.")
    return value


def _normalize_list_field(field: str, value: object) -> list[str]:
    if (not isinstance(value, list) or len(value) > 100
            or any(not isinstance(item, str) for item in value)):
        raise MetadataValidationError(f"{field} must be a list of at most 100 text values.")
    items = [item.strip() for item in value]
    if any(not item or len(item) > 200 for item in items):
        raise MetadataValidationError(f"{field} values must contain 1 to 200 characters.")
    return list(dict.fromkeys(items))


def _normalize_date_field(value: object) -> str:
    if not isinstance(value, str):
        raise MetadataValidationError("date_released must be a date or empty text.")
    value = value.strip()
    if value:
        try:
            from datetime import date
            date.fromisoformat(value)
        except ValueError as exc:
            raise MetadataValidationError("date_released must use YYYY-MM-DD.") from exc
        if len(value) != 10:
            raise MetadataValidationError("date_released must use YYYY-MM-DD.")
    return value


def effective_value(source_value, overrides: dict, field: str):
    """Resolve one value by key presence, allowing empty overrides to clear it."""
    return overrides[field] if field in overrides else source_value


def resolve_metadata(source: dict, overrides: object = None) -> dict:
    """Return source/effective values and override provenance for editable fields."""
    stored = overrides if isinstance(overrides, dict) else {}
    effective = dict(source)
    fields = {}
    source_fields = EDITABLE_FIELDS
    for field in sorted(source_fields):
        source_value = source.get(field)
        if field in EDITABLE_LIST_FIELDS and isinstance(source_value, str):
            try:
                source_value = json.loads(source_value)
            except (TypeError, ValueError):
                source_value = []
            if not isinstance(source_value, list):
                source_value = []
        overridden = field in stored
        value = effective_value(source_value, stored, field)
        effective[field] = value
        fields[field] = {
            "source": source_value,
            "effective": value,
            "overridden": overridden,
        }
    effective["metadata_fields"] = fields
    return effective


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    return value if isinstance(value, dict) else {}


def load_metadata_state(conn, video_id: str) -> dict | None:
    """Load raw source fields and the override revision for one catalog item."""
    return load_metadata_states(conn, [video_id]).get(str(video_id))


def load_metadata_states(conn, video_ids: list[str]) -> dict[str, dict]:
    """Load source metadata and overrides for a bounded batch in two reads."""
    ids = list(dict.fromkeys(str(video_id) for video_id in video_ids))
    if not ids:
        return {}
    columns = db.table_columns(conn, "videos")
    source_rows = conn.execute("SELECT * FROM videos WHERE id=ANY(%s)", (ids,)).fetchall()
    sources = {str(row[columns.index("id")]): dict(zip(columns, row)) for row in source_rows}
    override_rows = conn.execute(
        "SELECT video_id,fields,revision,updated_at FROM video_metadata_overrides "
        "WHERE video_id=ANY(%s)", (ids,),
    ).fetchall()
    overrides_by_video = {
        str(video_id): (fields, revision, updated_at)
        for video_id, fields, revision, updated_at in override_rows
    }
    states = {}
    for video_id in ids:
        source = sources.get(video_id)
        if source is None:
            continue
        record = overrides_by_video.get(video_id)
        overrides = _json_object(record[0]) if record else {}
        revision = int(record[1]) if record else 0
        states[video_id] = {
            "video_id": video_id,
            "revision": revision,
            "overrides": overrides,
            "source": source,
            "updated_at": record[2] if record else None,
            "metadata": resolve_metadata(source, overrides),
        }
    return states


def update_metadata_state(conn, video_id: str, expected_revision: int,
                          patch: dict, reset_fields=()) -> dict:
    """Save a validated override change under the catalog row lock.

    The caller owns the surrounding transaction and must commit or roll it
    back. An absent override row starts at revision zero; every accepted write
    increments the monotonic revision, including a reset to inherited values.
    """
    normalized, resets = validate_override_patch(patch, reset_fields)
    columns = db.table_columns(conn, "videos")
    source_row = conn.execute("SELECT * FROM videos WHERE id=%s FOR UPDATE", (video_id,)).fetchone()
    if source_row is None:
        return {"outcome": "not_found"}
    source = dict(zip(columns, source_row))
    record = conn.execute(
        "SELECT fields,revision FROM video_metadata_overrides WHERE video_id=%s FOR UPDATE",
        (video_id,),
    ).fetchone()
    current = _json_object(record[0]) if record else {}
    revision = int(record[1]) if record else 0
    if int(expected_revision) != revision:
        return {"outcome": "stale", "revision": revision,
                "metadata": resolve_metadata(source, current)}
    updated = dict(current)
    updated.update(normalized)
    for field in resets:
        updated.pop(field, None)
    next_revision = revision + 1
    conn.execute(
        "INSERT INTO video_metadata_overrides (video_id,fields,revision,updated_at) "
        "VALUES (%s,%s,%s,CURRENT_TIMESTAMP) ON CONFLICT(video_id) DO UPDATE SET "
        "fields=EXCLUDED.fields,revision=EXCLUDED.revision,updated_at=EXCLUDED.updated_at",
        (video_id, Jsonb(updated), next_revision),
    )
    return {"outcome": "saved", "revision": next_revision,
            "overrides": updated, "metadata": resolve_metadata(source, updated)}


def preview_metadata_update(db_url: str | None, video_id: str, expected_revision: int,
                            patch: dict, reset_fields=()) -> dict:
    """Build a revision-checked before/after preview without changing data."""
    normalized, resets = validate_override_patch(patch, reset_fields)
    if not normalized and not resets:
        raise MetadataValidationError("Choose at least one metadata change.")
    conn = db.connect(db_url)
    try:
        state = load_metadata_state(conn, video_id)
        if state is None:
            return {"outcome": "not_found"}
        if int(expected_revision) != state["revision"]:
            return {"outcome": "stale", "revision": state["revision"]}
        next_overrides = dict(state["overrides"])
        next_overrides.update(normalized)
        for field in resets:
            next_overrides.pop(field, None)
        after = resolve_metadata(state["source"], next_overrides)
        fields = {}
        for field in (*normalized.keys(), *resets):
            fields[field] = {"before": state["metadata"].get(field), "after": after.get(field)}
        return {"outcome": "previewed", "video_id": video_id,
                "revision": state["revision"], "next_revision": state["revision"] + 1,
                "fields": fields, "changes": normalized, "reset_fields": resets,
                "source_fingerprint": metadata_source_fingerprint(state["source"])}
    finally:
        conn.close()
