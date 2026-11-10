"""Helpers shared by the performer metadata providers (StashDB, ThePornDB, PBC).

Each provider keeps its endpoint, field mapping and SQL. This module holds the
logic those modules used to copy: decoding HTTP results, choosing a search hit
by name or alias, and the alias, photo and pause helpers. Decoders take the
caller's logger so log records keep the provider's channel name.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import List, Optional

from common import db
from common.performer_catalog import normalize_name

from common.performers import is_safe_alias


def decode_bytes_payload(payload, log: logging.Logger) -> Optional[dict]:
    try:
        return json.loads(bytes(payload).decode("utf-8", errors="ignore"))
    except Exception:
        log.debug("Ignored exception in _decode_http_payload", exc_info=True)
        return None


def decode_str_payload(payload: str, log: logging.Logger) -> Optional[dict]:
    try:
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        log.debug("Ignored exception in _decode_http_payload", exc_info=True)
        return None


def decode_response_object(payload, log: logging.Logger) -> Optional[dict]:
    """Decode a response-like object (httpx / requests / mocks) via .json(), .content or .text."""
    try:
        if callable(getattr(payload, "json", None)):
            data = payload.json()
            return data if isinstance(data, dict) else None
    except Exception:
        log.debug("Ignored exception in _decode_http_payload", exc_info=True)
    try:
        content = getattr(payload, "content", None)
        if isinstance(content, (bytes, bytearray)):
            return json.loads(bytes(content).decode("utf-8", errors="ignore"))
        text = getattr(payload, "text", None)
        if isinstance(text, str):
            return json.loads(text)
    except Exception:
        log.debug("Ignored exception in _decode_http_payload", exc_info=True)
    return None


def decode_http_payload(payload, log: logging.Logger) -> Optional[dict]:
    """Best-effort conversion of an HTTP result (dict, bytes, str or response-like) to parsed JSON."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        return decode_bytes_payload(payload, log)
    if isinstance(payload, str):
        return decode_str_payload(payload, log)
    return decode_response_object(payload, log)


def resolve_api_token(token: Optional[str], env_key: str, fallback_env_key: str) -> str:
    """Explicit token, else ``env_key``, else ``fallback_env_key`` (stripped)."""
    return (
        token
        or os.environ.get(env_key, "").strip()
        or os.environ.get(fallback_env_key, "").strip()
    )


def match_by_name_or_alias(candidates: list, name_clean: str) -> Optional[dict]:
    """Return the first candidate whose name, then whose alias, normalizes to ``name_clean``."""
    for candidate in candidates:
        if isinstance(candidate, dict) and normalize_name(candidate.get("name") or "") == name_clean:
            return candidate
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        aliases = candidate.get("aliases") or []
        if isinstance(aliases, str):
            aliases = aliases.split(",")
        if any(normalize_name(alias) == name_clean for alias in aliases):
            return candidate
    return None


def performer_aliases(perf: dict, name_clean: str) -> List[str]:
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


def pause_between_requests(sleep: float, index: int, log: logging.Logger, where: str) -> None:
    if sleep and index > 0:
        try:
            time.sleep(sleep)
        except Exception:
            log.debug("Ignored exception in %s", where, exc_info=True)


def merge_stored_aliases(conn: db.Connection, name: str, aliases: list, locked_fields) -> list:
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


def link_aliases(conn: db.Connection, name: str, aliases: list, locked_fields) -> None:
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


def store_photo(conn: db.Connection, name: str, photo: str, now: int) -> None:
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
