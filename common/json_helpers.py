"""JSON decoding helpers for performer and video metadata columns.

The functions here are the bodies that several modules previously repeated.
Variants with different fallback rules stay in their own modules.
"""

from __future__ import annotations

import json


def json_list_or_empty(raw) -> list:
    """Decode a JSON array text. Undecodable or empty input gives [].

    Valid JSON that is not an array is returned as decoded, not replaced.
    """
    try:
        return json.loads(raw or "[]")
    except Exception:
        return []


def json_set_or_empty(raw) -> set:
    """Decode a JSON array text into a set. Empty or undecodable input gives an empty set."""
    try:
        return set(json.loads(raw)) if raw else set()
    except Exception:
        return set()


def json_object(value) -> dict:
    """Return a dict from a decoded object or from its JSON text. Anything else gives {}."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    return value if isinstance(value, dict) else {}
