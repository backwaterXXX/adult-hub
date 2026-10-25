"""Shared data and decision logic for reviewing performer-match proposals."""

from __future__ import annotations

import json
import uuid
from typing import Callable

from common import db


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
    if not isinstance(key, str) or not 1 <= len(key) <= 200:
        raise ProposalError(400, "Invalid performer group.")
    if reason not in {"ambiguous_catalog_alias", "possible_typo", "uncatalogued_credit"}:
        raise ProposalError(400, "Invalid proposal reason.")
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
    conn = db.connect(db_url)
    try:
        ids = [int(row[0]) for row in conn.execute(
            "SELECT p.id FROM telegram_performer_proposals p LEFT JOIN video_metadata_overrides o ON o.video_id=p.video_id "
            "WHERE p.status='pending' AND NOT COALESCE(o.fields ? 'performers',FALSE) AND p.normalized=%s AND p.reason=%s ORDER BY p.id LIMIT 100",
            (key, reason),
        ).fetchall()]
    finally:
        conn.close()
    decision = dict(body)
    if not ids:
        return {"ok": True, "reviewed": 0, "limit": 100}
    if body.get("action") == "approve" and body.get("canonical"):
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
                    performer_id = str(existing[0])
                else:
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
        finally:
            conn.close()
        decision = {"action": "approve", "performer_id": performer_id}
    reviewed = 0
    for proposal_id in ids:
        review_proposal(db_url, proposal_id, decision, wake_worker=wake_worker)
        reviewed += 1
    return {"ok": True, "reviewed": reviewed, "limit": 100}


def reconcile_clear_matches(db_url: str | None, after_id: int = 0, limit: int = 1000, *,
                            wake_worker: Callable[[str], None]) -> dict:
    """Resolve only pending credits that now have one clear catalog identity."""
    from common.performer_catalog import load_catalog, normalize_name, prepare_catalog, match_catalog
    from common.performers import is_valid_performer_name
    from common.telegram_metadata import extract_telegram_metadata

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
            key = normalize_name(str(matched_text or candidate or ""))
            matches = aliases.get(key, [])
            unique = {str(record["id"]): record for record in matches}
            selected = next(iter(unique.values())) if len(unique) == 1 else None
            if selected is None and reason == "possible_typo":
                _exact, suggestions = match_catalog(
                    [(str(source_field or "source"), str(source_text or "")), ("title", str(title or ""))],
                    records, prepared=prepared,
                )
                candidates = {str(item.get("performer_id")): item for item in suggestions
                              if item.get("reason") == "possible_typo"
                              and normalize_name(str(item.get("matched_text") or "")) == key
                              and item.get("performer_id")}
                selected_id = next(iter(candidates), None) if len(candidates) == 1 else None
                selected = next((record for record in records if str(record.get("id")) == selected_id), None)
            if selected is None and reason == "uncatalogued_credit":
                extracted = extract_telegram_metadata(str(source_text or ""))
                if extracted.get("performer_source") == "labeled_cast":
                    explicit = [str(name).strip() for name in extracted.get("performer_names", [])
                                if normalize_name(str(name)) == key]
                    if len(explicit) == 1 and is_valid_performer_name(explicit[0]):
                        person_name = explicit[0]
                        person_key = normalize_name(person_name)
                        if person_key not in aliases and person_key not in auto_created:
                            person_id = "local:" + str(uuid.uuid4())
                            auto_created[person_key] = {
                                "id": person_id, "name": person_name, "aliases": [person_name],
                                "trusted_aliases": [person_name], "_persisted": True,
                            }
                            selected = auto_created[person_key]
                        elif person_key in auto_created:
                            selected = auto_created[person_key]
            if not selected or selected.get("_persisted") is not True:
                continue
            canonical = str(selected.get("name") or "").strip()
            if not canonical:
                continue
            if canonical.casefold() not in {str(name).casefold() for name in names}:
                names.append(canonical)
            approved.append((int(proposal_id), str(selected["id"])))

        resolved = 0
        with conn:
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
        cursor = int(rows[-1][0])
        has_more = len(rows) == safe_limit
    finally:
        conn.close()
    refresh_queued = 0
    refresh_error = False
    if refreshed:
        try:
            from video_duplicates import enqueue_job
            for offset in range(0, len(refreshed), 50):
                enqueue_job(db_url, "performer_refresh", {"video_ids": refreshed[offset:offset + 50]})
                refresh_queued += min(50, len(refreshed) - offset)
            wake_worker("jobs")
        except Exception:
            refresh_error = True
    return {"processed": len(rows), "resolved": resolved, "after_id": cursor, "has_more": has_more,
            "refresh_queued": refresh_queued, "refresh_error": refresh_error}


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
            row = conn.execute("""
                SELECT p.video_id,p.matched_text,p.status,
                       COALESCE(o.fields ? 'performers',FALSE),p.candidate,p.reason
                FROM telegram_performer_proposals p
                LEFT JOIN video_metadata_overrides o ON o.video_id=p.video_id
                WHERE p.id=%s FOR UPDATE OF p
            """, (proposal_id,)).fetchone()
            if not row or row[2] != "pending":
                raise ProposalError(404, "Pending proposal not found.")
            video_id = row[0]
            alias = str(row[1] or "").strip()
            stored_candidate = str(row[4] or "").strip() if len(row) > 4 else ""
            stored_reason = row[5] if len(row) > 5 else ""
            if stored_reason == "uncatalogued_credit" and stored_candidate:
                alias = stored_candidate
            if action == "reject":
                conn.execute("UPDATE telegram_performer_proposals SET status='rejected' WHERE id=%s", (proposal_id,))
                return {"ok": True, "status": "rejected"}
            if row[3]:
                raise ProposalError(409, "This video's performer list is manually overridden; edit that override to change it.")

            if performer_id:
                performer = conn.execute(
                    "SELECT name FROM performer_catalog WHERE id=%s AND active=TRUE", (performer_id,)
                ).fetchone()
                if not performer:
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
                    performer_id = local_id
                    performer = (canonical,)
                canonical = str(performer[0])
            else:
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

            if alias:
                from common.performer_catalog import normalize_name
                conn.execute("""
                    INSERT INTO performer_catalog_aliases(performer_id,alias,normalized,origin,approved)
                    VALUES (%s,%s,%s,'reviewed',TRUE)
                    ON CONFLICT(performer_id,normalized,origin) DO UPDATE SET approved=TRUE
                """, (performer_id, alias, normalize_name(alias)))
                from common.performer_profiles import get_override_fields
                aliases_locked = "aliases" in get_override_fields(conn, canonical)
                if normalize_name(alias) != normalize_name(canonical) and not aliases_locked:
                    conn.execute("""
                        INSERT INTO performer_aliases(alias,canonical) VALUES(%s,%s)
                        ON CONFLICT(alias) DO UPDATE SET canonical=EXCLUDED.canonical
                    """, (alias.casefold(), canonical))

            current = conn.execute("SELECT performers FROM videos WHERE id=%s FOR UPDATE", (video_id,)).fetchone()
            names = current[0] if current else []
            if isinstance(names, str):
                names = json.loads(names or "[]")
            from common.performer_catalog import normalize_name
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
            conn.execute("""
                UPDATE telegram_performer_proposals SET status='approved',performer_id=%s WHERE id=%s
            """, (performer_id, proposal_id))

        refresh_job_id = None
        try:
            from video_duplicates import enqueue_job
            refresh_job_id = enqueue_job(db_url, "performer_refresh", {"video_ids": [video_id]})
            wake_worker("jobs")
        except Exception:
            # The reviewed catalog change is committed; refresh is best-effort
            # and its absence is reported explicitly for later recovery.
            pass
        return {"ok": True, "status": "approved", "performer": canonical,
                "refresh_job_id": refresh_job_id, "refresh_pending": bool(refresh_job_id)}
    finally:
        conn.close()
