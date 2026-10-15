"""ThePornDB (TPDB) performer metadata enrichment fallback.

Fallback source after PBC. Fetches performer data from
api.metadataapi.net REST API when PBC yields an empty bio.
Requires THEPORNDB_API_TOKEN environment variable or config setting.
"""
from __future__ import annotations

import json
import logging
import os
from common import db
from common.external_metadata import entity_key, record_attempt
from common.http_rate_limit import request_with_host_limit, wait_for_host_slot
from common.performer_catalog import normalize_name
from common.performer_profiles import get_override_fields, lock_profile_enrichment
import time
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

try:
    import httpx
except ImportError:
    httpx = None

try:
    from common.performers import is_safe_alias
except ImportError:
    try:
        from performers import is_safe_alias
    except ImportError:
        def is_safe_alias(alias: str, canonical: str = "") -> bool:
            return True

logger = logging.getLogger("theporndb")

TPDB_API_URL = "https://api.theporndb.net/performers"
LEGACY_TPDB_API_URL = "https://api.metadataapi.net/performers"
USER_AGENT = "AdultHubSync/1.0 (theporndb-enrichment)"
TIMEOUT = 15.0
ENV_TOKEN_KEY = "THEPORNDB_API_TOKEN"


def _decode_http_payload(payload) -> Optional[dict]:
    """Best-effort conversion of an http_get result to parsed JSON."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, (bytes, bytearray)):
        try:
            return json.loads(bytes(payload).decode("utf-8", errors="ignore"))
        except Exception:
            return None
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    try:
        if callable(getattr(payload, "json", None)):
            data = payload.json()
            return data if isinstance(data, dict) else None
    except Exception:
        pass
    try:
        content = getattr(payload, "content", None)
        if isinstance(content, (bytes, bytearray)):
            return json.loads(bytes(content).decode("utf-8", errors="ignore"))
        text = getattr(payload, "text", None)
        if isinstance(text, str):
            return json.loads(text)
    except Exception:
        pass
    return None


def search_performer(
    name: str,
    token: Optional[str] = None,
    http_get: Optional[Callable] = None,
    *, raise_on_error: bool = False,
) -> Optional[Dict[str, object]]:
    """Search TPDB for performer details.

    Returns dict with bio, birth, nationality, aliases (list), photo (str), or None.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    api_token = (
        token
        or os.environ.get(ENV_TOKEN_KEY, "").strip()
        or os.environ.get("THEPORNDB_API_KEY", "").strip()
    )
    if not api_token:
        logger.debug("TPDB search skipped: %s not set", ENV_TOKEN_KEY)
        return None

    url = f"{TPDB_API_URL}?q={quote(name.strip(), safe='')}"
    headers = {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
    }
    data = None
    try:
        if http_get is not None:
            wait_for_host_slot(url)
            try:
                raw = http_get(url, headers=headers, timeout=TIMEOUT)
            except TypeError:
                try:
                    raw = http_get(url, timeout=TIMEOUT)
                except TypeError:
                    raw = http_get(url)
            data = _decode_http_payload(raw)
        else:
            if httpx is not None:
                try:
                    resp = request_with_host_limit(
                        httpx, "get", url, headers=headers, timeout=TIMEOUT,
                        follow_redirects=True,
                    )
                    if getattr(resp, "status_code", 200) == 200:
                        data = resp.json()
                except Exception:
                    pass
                if data is None:
                    legacy_url = f"{LEGACY_TPDB_API_URL}?q={quote(name.strip(), safe='')}"
                    try:
                        resp = request_with_host_limit(
                            httpx, "get", legacy_url, headers=headers, timeout=TIMEOUT,
                            follow_redirects=True,
                        )
                        if getattr(resp, "status_code", 200) == 200:
                            data = resp.json()
                    except Exception:
                        pass
            if data is None:
                import urllib.request
                req = urllib.request.Request(url, headers=headers)
                wait_for_host_slot(url)
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        logger.debug("search_performer(%r) request failed: %s", name, e)
        if raise_on_error:
            raise
        return None

    if not isinstance(data, dict):
        return None

    results = data.get("data")
    if not isinstance(results, list) or not results:
        return None

    # Pick the best matching result (exact name match preferred)
    perf = None
    name_clean = normalize_name(name)
    for candidate in results:
        if isinstance(candidate, dict) and normalize_name(candidate.get("name") or "") == name_clean:
            perf = dict(candidate)
            break
    if perf is None:
        for candidate in results:
            if not isinstance(candidate, dict):
                continue
            aliases = candidate.get("aliases") or []
            if isinstance(aliases, str):
                aliases = aliases.split(",")
            if any(normalize_name(alias) == name_clean for alias in aliases):
                perf = dict(candidate)
                break
    if perf is None:
        return None

    # Fetch details if available to obtain extras (birthday, nationality, birthplace) and posters
    perf_id = perf.get("id") or perf.get("slug") or perf.get("_id")
    if perf_id and http_get is None and httpx is not None:
        try:
            detail_url = f"{TPDB_API_URL}/{perf_id}"
            d_resp = request_with_host_limit(
                httpx, "get", detail_url, headers=headers, timeout=TIMEOUT,
                follow_redirects=True,
            )
            if getattr(d_resp, "status_code", 200) == 200:
                d_data = d_resp.json()
                if isinstance(d_data, dict) and isinstance(d_data.get("data"), dict):
                    for k, v in d_data["data"].items():
                        if v is not None:
                            perf[k] = v
        except Exception as e:
            logger.debug("search_performer(%r) detail fetch failed: %s", name, e)

    bio = (perf.get("bio") or perf.get("description") or "").strip()
    extras = perf.get("extras") if isinstance(perf.get("extras"), dict) else {}
    birth = (extras.get("birthday") or perf.get("birthday") or perf.get("date_of_birth") or "").strip()
    if birth and "T" in birth:
        birth = birth.split("T")[0].strip()

    nationality = (extras.get("nationality") or extras.get("birthplace") or perf.get("nationality") or "").strip()

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

    # High-quality photo priority: posters list -> image -> poster -> thumbnail
    photo = ""
    posters = perf.get("posters")
    if isinstance(posters, list) and posters:
        for p_item in posters:
            if isinstance(p_item, dict) and p_item.get("url"):
                photo = str(p_item["url"]).strip()
                break
            elif isinstance(p_item, str) and p_item.strip():
                photo = p_item.strip()
                break
    if not photo:
        photo = (perf.get("image") or perf.get("poster") or perf.get("thumbnail") or "").strip()

    if not bio and not birth and not nationality and not deduped_aliases and not photo:
        return None

    return {
        "bio": bio,
        "birth": birth,
        "nationality": nationality,
        "aliases": deduped_aliases,
        "photo": photo,
    }


def backfill_tpdb(
    conn: db.Connection,
    limit: int = 15,
    sleep: float = 1.0,
    token: Optional[str] = None,
    fetch: Optional[Callable] = None,
) -> Tuple[int, int]:
    """Enrich performers with empty bios or missing metadata from ThePornDB API.

    Returns (checked, updated).
    """
    api_token = (
        token
        or os.environ.get(ENV_TOKEN_KEY, "").strip()
        or os.environ.get("THEPORNDB_API_KEY", "").strip()
    )
    if not api_token and fetch is None:
        logger.debug("backfill_tpdb skipped: no token configured")
        return 0, 0

    try:
        # Select oldest due lookups first so an empty result cannot pin the
        # same alphabetic names at the head of every daily run.
        rows = conn.execute(
            """
            SELECT b.name FROM performer_bios b
            LEFT JOIN performer_photos p ON lower(p.name)=lower(b.name)
            LEFT JOIN external_metadata_attempts a
              ON a.entity_type='performer' AND a.entity_key=lower(b.name) AND a.source='theporndb'
            WHERE (b.bio IS NULL OR b.bio='' OR b.birth IS NULL OR b.birth=''
                   OR b.nationality IS NULL OR b.nationality='' OR p.thumb IS NULL OR p.thumb='')
              AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=CURRENT_TIMESTAMP)
            ORDER BY a.last_attempt_at ASC NULLS FIRST, lower(b.name) ASC
            LIMIT %s
            """,
            (max(0, int(limit)),),
        ).fetchall()
        candidates = [r[0] for r in rows if r and r[0]]
        if len(candidates) < limit and fetch is None:
            extra_needed = limit - len(candidates)
            try:
                if candidates:
                    placeholders = ",".join("%s" for _ in candidates)
                    extra_rows = conn.execute(
                        f"""
                        SELECT b.name FROM performer_bios b
                        LEFT JOIN performer_photos p ON lower(p.name) = lower(b.name)
                        WHERE (b.birth IS NULL OR b.birth = '' OR b.nationality IS NULL OR b.nationality = ''
                               OR p.thumb IS NULL OR p.thumb = '')
                          AND b.name NOT IN ({placeholders})
                        ORDER BY b.updated_at ASC NULLS FIRST, b.name ASC
                        LIMIT %s
                        """,
                        tuple(candidates) + (extra_needed,),
                    ).fetchall()
                else:
                    extra_rows = conn.execute(
                        """
                        SELECT b.name FROM performer_bios b
                        LEFT JOIN performer_photos p ON lower(p.name) = lower(b.name)
                        WHERE (b.birth IS NULL OR b.birth = '' OR b.nationality IS NULL OR b.nationality = ''
                               OR p.thumb IS NULL OR p.thumb = '')
                        ORDER BY b.updated_at ASC NULLS FIRST, b.name ASC
                        LIMIT %s
                        """,
                        (extra_needed,),
                    ).fetchall()
                for er in extra_rows:
                    if er and er[0]:
                        candidates.append(er[0])
            except Exception:
                pass
    except Exception as e:
        logger.warning("backfill_tpdb query failed: %s", e)
        return 0, 0

    checked = 0
    updated = 0

    for index, name in enumerate(candidates):
        if sleep and index > 0:
            try:
                time.sleep(sleep)
            except Exception:
                pass
        checked += 1
        try:
            info = search_performer(name, token=api_token, http_get=fetch, raise_on_error=True)
            if not info:
                record_attempt(conn, "performer", entity_key(name), "theporndb", "no_match")
                continue

            bio = info.get("bio") or ""
            birth = info.get("birth") or ""
            nationality = info.get("nationality") or ""
            aliases = info.get("aliases") or []
            photo = info.get("photo") or ""
            now = int(time.time())
            lock_profile_enrichment(conn, name)
            locked_fields = get_override_fields(conn, name)
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

            with conn:
                try:
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
                except Exception:
                    conn.execute(
                        """
                        UPDATE performer_bios
                        SET bio = CASE WHEN %s THEN bio WHEN %s != '' THEN %s ELSE bio END,
                            birth = CASE WHEN %s != '' THEN %s ELSE birth END,
                            aliases = CASE WHEN %s THEN aliases WHEN %s != '[]' THEN %s ELSE aliases END,
                            updated_at = %s
                        WHERE name = %s
                        """,
                        ("bio" in locked_fields, bio, bio, birth, birth,
                         "aliases" in locked_fields,
                         json.dumps(merged_aliases, ensure_ascii=False),
                         json.dumps(merged_aliases, ensure_ascii=False), now, name),
                    )
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
                if photo and "photo" not in locked_fields:
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
            record_attempt(conn, "performer", entity_key(name), "theporndb", "found")
            updated += 1
            logger.info("TPDB enriched %s (bio=%s, birth=%s, nat=%s, aliases=%d, photo=%s)",
                        name, bool(bio), bool(birth), bool(nationality), len(aliases), bool(photo))
        except Exception as e:
            try:
                record_attempt(conn, "performer", entity_key(name), "theporndb", "error", error=e)
            except Exception:
                logger.debug("Could not save ThePornDB retry state for %r", name)
            logger.warning("backfill_tpdb(%r) failed: %s", name, e)

    if checked:
        logger.info("ThePornDB performer backfill: checked=%d enriched=%d", checked, updated)
    return checked, updated
