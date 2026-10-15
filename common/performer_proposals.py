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
