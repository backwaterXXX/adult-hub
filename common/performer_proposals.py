"""Shared data and decision logic for reviewing performer-match proposals."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Callable

from common import db

logger = logging.getLogger(__name__)


class ProposalError(Exception):
    """A safe, user-facing proposal error with an HTTP status for route adapters."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def list_pending_proposals(db_url: str | None, limit: int = 100, offset: int = 0) -> dict:
    safe_limit = max(1, min(int(limit), 500))
    safe_offset = max(0, min(int(offset), 1_000_000))
    conn = db.connect(db_url)
    try:
        total = conn.execute(
            "SELECT count(*) FROM telegram_performer_proposals WHERE status='pending'"
        ).fetchone()[0]
        rows = conn.execute("""
            SELECT p.id,p.video_id,p.candidate,p.matched_text,p.source_field,p.reason,
                   p.performer_id,v.title
            FROM telegram_performer_proposals p JOIN videos v ON v.id=p.video_id
            WHERE p.status='pending' ORDER BY p.created_at,p.id LIMIT %s OFFSET %s
        """, (safe_limit, safe_offset)).fetchall()
        fields = ("id", "video_id", "candidate", "matched_text", "source_field",
                  "reason", "performer_id", "title")
        return {"items": [dict(zip(fields, row)) for row in rows], "total": int(total),
                "limit": safe_limit, "offset": safe_offset}
    finally:
        conn.close()


def list_pending_groups(db_url: str | None, limit: int = 50, offset: int = 0) -> dict:
    """Summarize repeated review work so one decision can cover its exact group."""
    safe_limit = max(1, min(int(limit), 100))
    safe_offset = max(0, min(int(offset), 1_000_000))
    conn = db.connect(db_url)
    try:
        total = conn.execute("""
            SELECT count(*) FROM (
                SELECT p.normalized,p.reason FROM telegram_performer_proposals p
                LEFT JOIN video_metadata_overrides o ON o.video_id=p.video_id
                WHERE p.status='pending' AND NOT COALESCE(o.fields ? 'performers',FALSE)
                GROUP BY p.normalized,p.reason
            ) groups_
        """).fetchone()[0]
        rows = conn.execute("""
            SELECT p.normalized,p.reason,min(p.candidate),count(*),count(DISTINCT p.video_id),
                   min(p.matched_text),min(v.title)
            FROM telegram_performer_proposals p
            JOIN videos v ON v.id=p.video_id
            LEFT JOIN video_metadata_overrides o ON o.video_id=p.video_id
            WHERE p.status='pending' AND NOT COALESCE(o.fields ? 'performers',FALSE)
            GROUP BY p.normalized,p.reason
            ORDER BY count(*) DESC,min(p.normalized),p.reason
            LIMIT %s OFFSET %s
        """, (safe_limit, safe_offset)).fetchall()
        fields = ("key", "reason", "candidate", "proposals", "videos", "matched_text", "example_title")
        return {"items": [dict(zip(fields, row)) for row in rows], "total": int(total),
                "pending": int(conn.execute("SELECT count(*) FROM telegram_performer_proposals p LEFT JOIN video_metadata_overrides o ON o.video_id=p.video_id WHERE p.status='pending' AND NOT COALESCE(o.fields ? 'performers',FALSE)").fetchone()[0]),
                "limit": safe_limit, "offset": safe_offset}
    finally:
        conn.close()


def review_proposal_group(db_url: str | None, key: str, reason: str, body: dict, *,
                          wake_worker: Callable[[str], None]) -> dict:
    """Apply an explicit operator decision to the current bounded group."""
    canonical = _validate_group_request(key, reason, body)
    ids = _pending_group_proposal_ids(db_url, key, reason)
    decision = dict(body)
    if not ids:
        return {"ok": True, "reviewed": 0, "limit": 100}
    if body.get("action") == "approve" and body.get("canonical"):
        performer_id = _ensure_group_canonical_performer(db_url, canonical)
        decision = {"action": "approve", "performer_id": performer_id}
    reviewed = 0
    refresh_job_ids = []
    for proposal_id in ids:
        result = review_proposal(db_url, proposal_id, decision, wake_worker=wake_worker)
        if result.get("refresh_job_id"):
            refresh_job_ids.append(result["refresh_job_id"])
        reviewed += 1
    return {"ok": True, "reviewed": reviewed, "limit": 100,
            "refresh_job_ids": list(dict.fromkeys(refresh_job_ids))}


def _validate_group_request(key: str, reason: str, body: dict):
    """Validate a group decision and return the stripped canonical name, if one was supplied."""
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise ProposalError(400, "Invalid performer group.")
    if reason not in {"ambiguous_catalog_alias", "possible_typo", "uncatalogued_credit"}:
        raise ProposalError(400, "Invalid proposal reason.")
    canonical = None
    if body.get("action") == "approve":
        performer_id = body.get("performer_id")
        canonical = body.get("canonical")
        if performer_id is not None and (not isinstance(performer_id, str) or not 1 <= len(performer_id) <= 200):
            raise ProposalError(400, "Invalid performer ID.")
        if canonical is not None:
            from common.performers import is_valid_performer_name
            if not isinstance(canonical, str) or not is_valid_performer_name(canonical.strip()):
                raise ProposalError(400, "Enter a valid local performer name.")
            canonical = canonical.strip()
    return canonical


def _pending_group_proposal_ids(db_url: str | None, key: str, reason: str) -> list[int]:
    """Return up to 100 pending proposal ids in the exact normalized group and reason."""
    conn = db.connect(db_url)
    try:
        return [int(row[0]) for row in conn.execute(
            "SELECT p.id FROM telegram_performer_proposals p LEFT JOIN video_metadata_overrides o ON o.video_id=p.video_id "
            "WHERE p.status='pending' AND NOT COALESCE(o.fields ? 'performers',FALSE) AND p.normalized=%s AND p.reason=%s ORDER BY p.id LIMIT 100",
            (key, reason),
        ).fetchall()]
    finally:
        conn.close()


def _ensure_group_canonical_performer(db_url: str | None, canonical: str) -> str:
    """Return the id of the active catalogue performer for canonical, creating a local one if needed."""
    from common.performer_catalog import normalize_name
    conn = db.connect(db_url)
    try:
        with conn:
            normalized = normalize_name(canonical)
            existing = conn.execute("""
                SELECT p.id FROM performer_catalog p
                JOIN performer_catalog_aliases a ON a.performer_id=p.id
                WHERE p.active=TRUE AND a.approved=TRUE AND a.normalized=%s
                ORDER BY p.source='local' DESC,p.id LIMIT 1
            """, (normalized,)).fetchone()
            if existing:
                return str(existing[0])
            performer_id = "local:" + str(uuid.uuid4())
            conn.execute("""
                INSERT INTO performer_catalog(id,source,source_id,name,active)
                VALUES (%s,'local',%s,%s,TRUE)
            """, (performer_id, performer_id.removeprefix("local:"), canonical))
            conn.execute("""
                INSERT INTO performer_catalog_aliases(performer_id,alias,normalized,origin,approved)
                VALUES (%s,%s,%s,'reviewed',TRUE)
                ON CONFLICT(performer_id,normalized,origin) DO NOTHING
            """, (performer_id, canonical, normalized))
            return performer_id
    finally:
        conn.close()


def reconcile_clear_matches(db_url: str | None, after_id: int = 0, limit: int = 1000, *,
                            wake_worker: Callable[[str], None]) -> dict:
    """Resolve only pending credits that now have one clear catalog identity."""
    from common.performer_catalog import load_catalog, prepare_catalog

    safe_after = max(0, min(int(after_id), 2_147_483_647))
    safe_limit = max(1, min(int(limit), 1000))
    conn = db.connect(db_url)
    refreshed = []
    try:
        records = load_catalog(conn)
        prepared = prepare_catalog(records)
        aliases = prepared["index"]
        rows = conn.execute("""
            SELECT p.id,p.video_id,p.candidate,p.matched_text,p.source_field,p.reason,
                   v.title,v.source_text,v.performers
            FROM telegram_performer_proposals p JOIN videos v ON v.id=p.video_id
            LEFT JOIN video_metadata_overrides o ON o.video_id=p.video_id
            WHERE p.status='pending' AND p.id>%s AND NOT COALESCE(o.fields ? 'performers',FALSE)
            ORDER BY p.id LIMIT %s
        """, (safe_after, safe_limit)).fetchall()
        if not rows:
            return {"processed": 0, "resolved": 0, "after_id": safe_after, "has_more": False}
        video_updates: dict[str, tuple[list[str], list[tuple[int, str]]]] = {}
        auto_created: dict[str, dict] = {}
        for proposal_id, video_id, candidate, matched_text, source_field, reason, title, source_text, raw_names in rows:
            video_id = str(video_id)
            if video_id not in video_updates:
                names = raw_names if isinstance(raw_names, list) else json.loads(raw_names or "[]") if isinstance(raw_names, str) else []
                video_updates[video_id] = (list(names), [])
            names, approved = video_updates[video_id]
            selected = _select_clear_match(
                reason, candidate, matched_text, source_field, source_text, title,
                aliases, records, prepared, auto_created,
            )
            if not selected or selected.get("_persisted") is not True:
                continue
            canonical = str(selected.get("name") or "").strip()
            if not canonical:
                continue
            if canonical.casefold() not in {str(name).casefold() for name in names}:
                names.append(canonical)
            approved.append((int(proposal_id), str(selected["id"])))

        with conn:
            resolved = _persist_clear_matches(conn, auto_created, video_updates, refreshed)
        cursor = int(rows[-1][0])
        has_more = len(rows) == safe_limit
    finally:
        conn.close()
    refresh_queued, refresh_job_ids, refresh_error = _enqueue_clear_match_refresh(db_url, refreshed, wake_worker)
    return {"processed": len(rows), "resolved": resolved, "after_id": cursor, "has_more": has_more,
            "refresh_queued": refresh_queued, "refresh_error": refresh_error,
            "refresh_job_ids": list(dict.fromkeys(refresh_job_ids))}


def _select_clear_match(
    reason: str,
    candidate,
    matched_text,
    source_field,
    source_text,
    title,
    aliases: dict,
    records: list,
    prepared: dict,
    auto_created: dict,
) -> dict | None:
    """Pick the single catalogue record a pending proposal clearly refers to, or None."""
    from common.performer_catalog import normalize_name

    key = normalize_name(str(matched_text or candidate or ""))
    matches = aliases.get(key, [])
    unique = {str(record["id"]): record for record in matches}
    selected = next(iter(unique.values())) if len(unique) == 1 else None
    if selected is None and reason == "possible_typo":
        selected = _select_typo_record(key, source_field, source_text, title, records, prepared)
    if selected is None and reason == "uncatalogued_credit":
        selected = _select_uncatalogued_credit_record(key, source_text, aliases, auto_created)
    return selected


def _select_typo_record(key: str, source_field, source_text, title, records: list, prepared: dict) -> dict | None:
    """Resolve a possible-typo proposal through the catalogue matcher when exactly one suggestion agrees."""
    from common.performer_catalog import match_catalog, normalize_name

    _exact, suggestions = match_catalog(
        [(str(source_field or "source"), str(source_text or "")), ("title", str(title or ""))],
        records, prepared=prepared,
    )
    candidates = {str(item.get("performer_id")): item for item in suggestions
                  if item.get("reason") == "possible_typo"
                  and normalize_name(str(item.get("matched_text") or "")) == key
                  and item.get("performer_id")}
    selected_id = next(iter(candidates), None) if len(candidates) == 1 else None
    return next((record for record in records if str(record.get("id")) == selected_id), None)


def _select_uncatalogued_credit_record(key: str, source_text, aliases: dict, auto_created: dict) -> dict | None:
    """Resolve an uncatalogued credit from an explicit labelled cast line, creating a local person once."""
    from common.performer_catalog import normalize_name
    from common.telegram_metadata import extract_telegram_metadata
    from common.performers import is_valid_performer_name

    extracted = extract_telegram_metadata(str(source_text or ""))
    if extracted.get("performer_source") != "labeled_cast":
        return None
    explicit = [str(name).strip() for name in extracted.get("performer_names", [])
                if normalize_name(str(name)) == key]
    if len(explicit) != 1 or not is_valid_performer_name(explicit[0]):
        return None
    person_name = explicit[0]
    person_key = normalize_name(person_name)
    if person_key not in aliases and person_key not in auto_created:
        person_id = "local:" + str(uuid.uuid4())
        auto_created[person_key] = {
            "id": person_id, "name": person_name, "aliases": [person_name],
            "trusted_aliases": [person_name], "_persisted": True,
        }
        return auto_created[person_key]
    if person_key in auto_created:
        return auto_created[person_key]
    return None


def _persist_clear_matches(conn, auto_created: dict, video_updates: dict, refreshed: list) -> int:
    """Write new local performers, updated video lists and approved proposals; return how many proposals changed."""
    from common.performer_catalog import normalize_name

    resolved = 0
    for person in auto_created.values():
        person_id = str(person["id"])
        person_name = str(person["name"])
        conn.execute("""
            INSERT INTO performer_catalog(id,source,source_id,name,active)
            VALUES (%s,'local',%s,%s,TRUE) ON CONFLICT(id) DO NOTHING
        """, (person_id, person_id.removeprefix("local:"), person_name))
        conn.execute("""
            INSERT INTO performer_catalog_aliases(performer_id,alias,normalized,origin,approved)
            VALUES (%s,%s,%s,'telegram_cast',TRUE)
            ON CONFLICT(performer_id,normalized,origin) DO NOTHING
        """, (person_id, person_name, normalize_name(person_name)))
    for video_id, (names, approvals) in video_updates.items():
        if not approvals:
            continue
        conn.execute("UPDATE videos SET performers=%s WHERE id=%s", (json.dumps(names, ensure_ascii=False), video_id))
        for proposal_id, performer_id in approvals:
            changed = conn.execute(
                "UPDATE telegram_performer_proposals SET status='approved',performer_id=%s "
                "WHERE id=%s AND status='pending'", (performer_id, proposal_id),
            ).rowcount
            resolved += changed
        if approvals:
            refreshed.append(video_id)
    return resolved


def _enqueue_clear_match_refresh(db_url: str | None, refreshed: list, wake_worker: Callable[[str], None]) -> tuple[int, list, bool]:
    """Queue performer refresh jobs for the videos that changed; return (queued, job ids, error flag)."""
    refresh_queued = 0
    refresh_job_ids = []
    refresh_error = False
    if refreshed:
        try:
            from video_duplicates import enqueue_job
            for offset in range(0, len(refreshed), 50):
                refresh_job_ids.append(enqueue_job(db_url, "performer_refresh", {"video_ids": refreshed[offset:offset + 50]}))
                refresh_queued += min(50, len(refreshed) - offset)
            wake_worker("jobs")
        except Exception:
            refresh_error = True
    return refresh_queued, refresh_job_ids, refresh_error


def search_catalog(db_url: str | None, query: str = "", limit: int = 100) -> dict:
    safe_limit = max(1, min(int(limit), 200))
    conn = db.connect(db_url)
    try:
        from common.performer_catalog import load_catalog, normalize_name

        records = load_catalog(conn)
        active_ids = {str(row[0]) for row in conn.execute(
            "SELECT id FROM performer_catalog WHERE active=TRUE"
        ).fetchall()}
        active_names = {normalize_name(record["name"]) for record in records
                        if str(record["id"]) in active_ids}
        raw_query = query.strip()
        needle = normalize_name(raw_query)
        if raw_query and not needle:
            return {"items": []}
        matches = []
        for record in records:
            performer_id = str(record["id"])
            # Keep the active catalogue identity when a legacy profile has the
            # same canonical name; otherwise expose the legacy identity so an
            # operator can select it and preserve its existing bio/aliases.
            if performer_id not in active_ids and normalize_name(record["name"]) in active_names:
                continue
            searchable = [record["name"], *(record.get("aliases") or [])]
            if needle and not any(needle in normalize_name(value) for value in searchable):
                continue
            matches.append({"id": performer_id, "name": record["name"]})
        matches.sort(key=lambda item: (item["name"].casefold(), item["id"]))
        return {"items": matches[:safe_limit]}
    finally:
        conn.close()


def review_proposal(
    db_url: str | None,
    proposal_id: int,
    body: dict,
    *,
    wake_worker: Callable[[str], None],
) -> dict:
    """Apply one reviewed proposal, then request refresh metadata asynchronously."""
    action = body.get("action")
    performer_id = body.get("performer_id")
    if action not in {"approve", "reject"}:
        raise ProposalError(400, "Action must be approve or reject.")

    conn = db.connect(db_url)
    try:
        with conn:
            row = _lock_pending_proposal(conn, proposal_id)
            if not row or row[2] != "pending":
                raise ProposalError(404, "Pending proposal not found.")
            video_id = row[0]
            alias = _proposal_alias(row)
            if action == "reject":
                conn.execute("UPDATE telegram_performer_proposals SET status='rejected' WHERE id=%s", (proposal_id,))
                return {"ok": True, "status": "rejected"}
            if row[3]:
                raise ProposalError(409, "This video's performer list is manually overridden; edit that override to change it.")

            performer_id, canonical = _resolve_approval_target(conn, performer_id, body)
            if alias:
                _approve_proposal_alias(conn, performer_id, alias, canonical)
            _apply_approved_video_performers(conn, video_id, alias, canonical)
            conn.execute("""
                UPDATE telegram_performer_proposals SET status='approved',performer_id=%s WHERE id=%s
            """, (performer_id, proposal_id))

        refresh_job_id = _enqueue_proposal_refresh(db_url, video_id, wake_worker)
        return {"ok": True, "status": "approved", "performer": canonical,
                "refresh_job_id": refresh_job_id, "refresh_pending": bool(refresh_job_id)}
    finally:
        conn.close()


def _lock_pending_proposal(conn, proposal_id: int):
    """Lock a proposal row and its video's override flag for the duration of the transaction."""
    return conn.execute("""
        SELECT p.video_id,p.matched_text,p.status,
               COALESCE(o.fields ? 'performers',FALSE),p.candidate,p.reason
        FROM telegram_performer_proposals p
        LEFT JOIN video_metadata_overrides o ON o.video_id=p.video_id
        WHERE p.id=%s FOR UPDATE OF p
    """, (proposal_id,)).fetchone()


def _proposal_alias(row) -> str:
    """Return the alias to attach: the matched text, or the stored candidate for uncatalogued credits."""
    alias = str(row[1] or "").strip()
    stored_candidate = str(row[4] or "").strip() if len(row) > 4 else ""
    stored_reason = row[5] if len(row) > 5 else ""
    if stored_reason == "uncatalogued_credit" and stored_candidate:
        alias = stored_candidate
    return alias


def _resolve_approval_target(conn, performer_id, body: dict) -> tuple[str, str]:
    """Return (performer_id, canonical name) for an approval, creating the performer when needed."""
    if performer_id:
        performer = conn.execute(
            "SELECT name FROM performer_catalog WHERE id=%s AND active=TRUE", (performer_id,)
        ).fetchone()
        if not performer:
            performer_id, performer = _adopt_legacy_performer(conn, performer_id)
        return performer_id, str(performer[0])

    canonical = str(body.get("canonical") or "").strip()
    from common.performers import is_valid_performer_name
    if not is_valid_performer_name(canonical):
        raise ProposalError(400, "Choose a performer or provide a valid local name.")
    from common.performer_catalog import normalize_name
    performer_id = "local:" + str(uuid.uuid4())
    conn.execute("""
        INSERT INTO performer_catalog(id,source,source_id,name,active)
        VALUES (%s,'local',%s,%s,TRUE)
    """, (performer_id, performer_id.removeprefix("local:"), canonical))
    conn.execute("""
        INSERT INTO performer_catalog_aliases(performer_id,alias,normalized,origin,approved)
        VALUES (%s,%s,%s,'reviewed',TRUE)
        ON CONFLICT(performer_id,normalized,origin) DO NOTHING
    """, (performer_id, canonical, normalize_name(canonical)))
    return performer_id, canonical


def _adopt_legacy_performer(conn, performer_id: str) -> tuple[str, tuple]:
    """Copy a legacy local profile into the active catalogue; return its new id and a name row."""
    from common.performer_catalog import load_catalog, normalize_name

    legacy = next((record for record in load_catalog(conn)
                   if str(record["id"]) == str(performer_id)
                   and str(record["id"]).startswith("local:")), None)
    if not legacy:
        raise ProposalError(404, "Performer not found in the local catalogue.")
    canonical = str(legacy["name"])
    local_id = "local:" + str(uuid.uuid4())
    source_id = local_id.removeprefix("local:")
    conn.execute("""
        INSERT INTO performer_catalog(id,source,source_id,name,active)
        VALUES (%s,'local',%s,%s,TRUE)
    """, (local_id, source_id, canonical))
    for legacy_alias in dict.fromkeys([canonical, *(legacy.get("aliases") or [])]):
        conn.execute("""
            INSERT INTO performer_catalog_aliases(performer_id,alias,normalized,origin,approved)
            VALUES (%s,%s,%s,'legacy',TRUE)
            ON CONFLICT(performer_id,normalized,origin) DO NOTHING
        """, (local_id, legacy_alias, normalize_name(legacy_alias)))
    return local_id, (canonical,)


def _approve_proposal_alias(conn, performer_id: str, alias: str, canonical: str) -> None:
    """Attach the proposal alias to the performer and, when safe, to the performer_aliases map."""
    from common.performer_catalog import normalize_name
    conn.execute("""
        INSERT INTO performer_catalog_aliases(performer_id,alias,normalized,origin,approved)
        VALUES (%s,%s,%s,'reviewed',TRUE)
        ON CONFLICT(performer_id,normalized,origin) DO UPDATE SET approved=TRUE
    """, (performer_id, alias, normalize_name(alias)))
    from common.performer_profiles import get_override_fields
    aliases_locked = "aliases" in get_override_fields(conn, canonical)
    if normalize_name(alias) != normalize_name(canonical) and not aliases_locked:
        from common.performers import is_safe_alias
        if is_safe_alias(alias, canonical):
            owner = conn.execute(
                "SELECT canonical FROM performer_aliases WHERE lower(alias)=lower(%s)",
                (alias,),
            ).fetchone()
            if owner and str(owner[0] or "").strip().casefold() != canonical.casefold():
                raise ProposalError(
                    409,
                    f"Alias '{alias}' is already assigned to '{owner[0]}'. "
                    "Merge the two performers or approve this proposal for that performer.",
                )
            conn.execute("""
                INSERT INTO performer_aliases(alias,canonical) VALUES(%s,%s)
                ON CONFLICT(alias) DO UPDATE SET canonical=EXCLUDED.canonical
            """, (alias.casefold(), canonical))


def _apply_approved_video_performers(conn, video_id, alias: str, canonical: str) -> None:
    """Replace the matched alias in the video's performer list with the canonical name."""
    from common.performer_catalog import normalize_name
    current = conn.execute("SELECT performers FROM videos WHERE id=%s FOR UPDATE", (video_id,)).fetchone()
    names = current[0] if current else []
    if isinstance(names, str):
        names = json.loads(names or "[]")
    revised, seen_names = [], set()
    for name in names:
        target = canonical if alias and normalize_name(str(name)) == normalize_name(alias) else str(name)
        if target.casefold() not in seen_names:
            seen_names.add(target.casefold())
            revised.append(target)
    if canonical.casefold() not in seen_names:
        revised.append(canonical)
    if revised != names:
        conn.execute("UPDATE videos SET performers=%s WHERE id=%s", (json.dumps(revised, ensure_ascii=False), video_id))


def _enqueue_proposal_refresh(db_url: str | None, video_id, wake_worker: Callable[[str], None]):
    """Queue a best-effort performer refresh for the video; return the job id or None."""
    refresh_job_id = None
    try:
        from video_duplicates import enqueue_job
        refresh_job_id = enqueue_job(db_url, "performer_refresh", {"video_ids": [video_id]})
        wake_worker("jobs")
    except Exception:
        logger.debug("Ignored exception in review_proposal", exc_info=True)
        # The reviewed catalog change is committed; refresh is best-effort
        # and its absence is reported explicitly for later recovery.
    return refresh_job_id
