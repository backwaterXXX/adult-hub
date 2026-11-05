#!/usr/bin/env python3
"""Prepare a reviewable Telegram performer reconciliation report.

Default is report-only. Pass --apply to write unambiguous exact catalogue
matches and queue uncertain credits for review.
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sync"))

from common import db
from common.performer_catalog import (load_catalog, match_catalog, normalize_name,
                                      prepare_catalog)
from common.performers import is_non_performer_name
import sync as S

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", str(ROOT / "media" / "adult")))


def _regenerate_nfo(conn, video_id: str, performers: list[str]) -> bool:
    row = conn.execute("""
        SELECT provider,title,slug,duration,thumbnail,categories,tags,studio,first_seen,source_text
        FROM videos WHERE id=%s
    """, (video_id,)).fetchone()
    if not row:
        return False
    provider, title, slug, duration, thumbnail, categories, tags, studio, first_seen, source_text = row
    categories = categories if isinstance(categories, list) else json.loads(categories or "[]")
    tags = tags if isinstance(tags, list) else json.loads(tags or "[]")
    path = MEDIA_DIR / f"{S.stable_media_basename(video_id)}.nfo"
    if not path.exists():
        return False
    video = {"id": video_id, "provider": provider,
             "provider_video_id": video_id.split(":", 1)[-1],
             "title": title or video_id, "slug": slug or "",
             "duration": duration or 0, "thumbnail": thumbnail or "",
             "performers": performers, "plot": source_text or "",
             "performer_thumbs": S.load_performer_thumbs(conn, performers),
             "studio": studio or "", "dateadded": S.format_dateadded(first_seen or 0)}
    S.create_nfo_file(video, set(categories), set(tags), path, conn=conn)
    return True


def _merge_performers(existing: list, matched: list[dict], prepared: dict) -> list[str]:
    """Preserve existing spellings and add only new, exact catalogue identities."""
    index = prepared["index"]
    result: list[str] = []
    seen: set[str] = set()
    existing_ids: set[str] = set()

    def add(value: str) -> None:
        value = str(value or "").strip()
        key = normalize_name(value)
        if value and key and key not in seen:
            seen.add(key)
            result.append(value)

    for old_name in existing if isinstance(existing, list) else []:
        old_name = str(old_name or "").strip()
        if is_non_performer_name(old_name):
            continue
        key = normalize_name(old_name)
        candidates = index.get(key, [])
        if len({row["id"] for row in candidates}) == 1:
            existing_ids.add(candidates[0]["id"])
        add(old_name)
    for record in matched:
        if record["id"] not in existing_ids:
            add(record["name"])
    return result


def _ensure_bio_backup_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_performer_rebuild_bio_backup (
            run_id UUID NOT NULL,
            name TEXT NOT NULL,
            aliases TEXT NOT NULL,
            PRIMARY KEY(run_id,name)
        )
    """)


def _regenerate_nfos(conn, nfo_tasks) -> tuple[int, int]:
    """Regenerate NFOs for (video_id, performers) pairs; return (updated, failed)."""
    updated = failed = 0
    for video_id, performers in nfo_tasks:
        try:
            updated += int(_regenerate_nfo(conn, video_id, performers))
        except Exception:
            failed += 1
    return updated, failed


def _restore_rollback_rows(conn, rows, bio_rows) -> list:
    nfo_tasks = []
    with conn:
        for video_id, old_performers in rows:
            conn.execute("UPDATE videos SET performers=%s WHERE id=%s",
                         (json.dumps(old_performers, ensure_ascii=False), video_id))
            nfo_tasks.append((video_id, old_performers))
        for name, aliases in bio_rows:
            conn.execute("""
                INSERT INTO performer_bios (name,aliases) VALUES (%s,%s)
                ON CONFLICT(name) DO NOTHING
            """, (name, aliases))
    return nfo_tasks


def _refresh_after_rollback(rows, bio_rows) -> bool:
    refreshed = False
    if rows or bio_rows:
        try:
            S.sync_jellyfin_telegram_metadata()
            refreshed = bool(S.trigger_jellyfin_refresh())
        except Exception:
            pass
    return refreshed


def _rollback(conn, rollback: str) -> dict:
    try:
        run_id = str(uuid.UUID(rollback))
    except ValueError:
        raise ValueError("rollback id must be a UUID") from None
    _ensure_bio_backup_table(conn)
    rows = conn.execute("""
        SELECT video_id,old_performers FROM telegram_performer_rebuild_backup
        WHERE run_id=%s ORDER BY video_id
    """, (run_id,)).fetchall()
    bio_rows = conn.execute("""
        SELECT name,aliases FROM telegram_performer_rebuild_bio_backup
        WHERE run_id=%s ORDER BY name
    """, (run_id,)).fetchall()
    nfo_tasks = _restore_rollback_rows(conn, rows, bio_rows)
    nfos_updated, nfos_failed = _regenerate_nfos(conn, nfo_tasks)
    refreshed = _refresh_after_rollback(rows, bio_rows)
    return {"rolled_back_run": run_id, "videos_restored": len(rows),
            "performer_profiles_restored": len(bio_rows),
            "nfos_updated": nfos_updated, "nfos_failed": nfos_failed,
            "jellyfin_refresh_triggered": refreshed}


def _load_catalog_state(conn):
    catalog = load_catalog(conn)
    catalog_index = prepare_catalog(catalog)
    catalog_names = set(catalog_index["index"])
    return catalog, catalog_index, catalog_names


def _fetch_telegram_rows(conn, limit):
    query = """
        SELECT v.id,v.provider,v.title,v.slug,v.duration,v.thumbnail,v.performers,
               v.studio,v.first_seen,v.source_text,v.source_filename,
               COALESCE(o.fields->>'performers','')
        FROM videos v LEFT JOIN video_metadata_overrides o ON o.video_id=v.id
        WHERE v.provider='telegram' ORDER BY v.first_seen DESC NULLS LAST,v.id
    """
    return conn.execute(query + (" LIMIT %s" if limit else ""),
                        (limit,) if limit else None).fetchall()


def _backup_removed_profiles(conn, run_id: str) -> int:
    _ensure_bio_backup_table(conn)
    removed_profiles = conn.execute("""
        SELECT name,aliases FROM performer_bios
        WHERE regexp_replace(lower(name), '[^a-z0-9]', '', 'g') IN ('onlyfans','of')
        ORDER BY name
    """).fetchall()
    for name, aliases in removed_profiles:
        conn.execute("""
            INSERT INTO telegram_performer_rebuild_bio_backup (run_id,name,aliases)
            VALUES (%s,%s,%s) ON CONFLICT(run_id,name) DO NOTHING
        """, (run_id, name, aliases))
        conn.execute("DELETE FROM performer_bios WHERE name=%s", (name,))
    return len(removed_profiles)


def _legacy_cast_proposals(old, catalog_names) -> list:
    proposals = []
    for old_name in old if isinstance(old, list) else []:
        if is_non_performer_name(str(old_name)):
            continue
        old_key = normalize_name(str(old_name))
        if old_key and old_key not in catalog_names:
            proposals.append({"candidate": str(old_name), "matched_text": str(old_name),
                              "source_field": "existing_cast", "reason": "legacy_cast_candidate",
                              "performer_id": None})
    return proposals


def _write_changed_video(conn, run_id: str, vid, old, new) -> None:
    conn.execute("""
        INSERT INTO telegram_performer_rebuild_backup
            (run_id,video_id,old_performers,new_performers)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT(run_id,video_id) DO NOTHING
    """, (run_id, vid, json.dumps(old, ensure_ascii=False),
          json.dumps(new, ensure_ascii=False)))
    conn.execute("UPDATE videos SET performers=%s WHERE id=%s",
                 (json.dumps(new, ensure_ascii=False), vid))


def _queue_proposal(conn, vid, proposal: dict) -> None:
    candidate = proposal["candidate"]
    normalized = normalize_name(candidate)
    conn.execute("""
        INSERT INTO telegram_performer_proposals
            (video_id,candidate,normalized,matched_text,source_field,reason,performer_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(video_id,normalized) DO NOTHING
    """, (vid, candidate, normalized, proposal["matched_text"],
          proposal["source_field"], proposal["reason"], proposal["performer_id"]))


def _process_row(conn, row, *, apply, queue_proposals, run_id, catalog, catalog_index,
                 catalog_names, report, nfo_tasks) -> None:
    (vid, provider, title, slug, duration, thumbnail, raw_performers,
     studio, first_seen, source_text, filename, override) = row
    if override:
        report["manual_override_skipped"] += 1
        return
    old = json.loads(raw_performers or "[]") if isinstance(raw_performers, str) else (raw_performers or [])
    text = source_text or title or ""
    found, proposals = match_catalog(
        [("caption", text), ("filename", filename or ""), ("title", title or "")],
        catalog, prepared=catalog_index)
    new = _merge_performers(old, found, catalog_index)
    proposals.extend(_legacy_cast_proposals(old, catalog_names))
    if new != old:
        report["changed"].append({"video_id": vid, "old": old, "new": new})
        if apply:
            _write_changed_video(conn, run_id, vid, old, new)
            nfo_tasks.append((vid, new))
    else:
        report["unchanged"] += 1
    for proposal in proposals:
        report["proposals"].append({"video_id": vid, **proposal})
        if apply and queue_proposals:
            _queue_proposal(conn, vid, proposal)
            report["proposals_queued"] += 1


def _refresh_jellyfin_after_apply(report: dict) -> None:
    try:
        S.sync_jellyfin_telegram_metadata()
        report["jellyfin_refresh"] = "triggered" if S.trigger_jellyfin_refresh() else "not_confirmed"
    except Exception as exc:
        report["jellyfin_refresh"] = f"failed: {type(exc).__name__}"


def _rebuild(conn, apply: bool, limit, queue_proposals: bool) -> dict:
    catalog, catalog_index, catalog_names = _load_catalog_state(conn)
    rows = _fetch_telegram_rows(conn, limit)
    run_id = str(uuid.uuid4()) if apply else None
    report = {"run_id": run_id, "videos": len(rows), "changed": [], "unchanged": 0,
              "manual_override_skipped": 0, "proposals": [], "limit": limit,
              "proposals_queued": 0, "performer_profiles_removed": 0}
    nfo_tasks: list = []
    with conn:
        if apply:
            report["performer_profiles_removed"] = _backup_removed_profiles(conn, run_id)
        for row in rows:
            _process_row(conn, row, apply=apply, queue_proposals=queue_proposals,
                         run_id=run_id, catalog=catalog, catalog_index=catalog_index,
                         catalog_names=catalog_names, report=report, nfo_tasks=nfo_tasks)
    if apply:
        report["nfos_updated"], report["nfos_failed"] = _regenerate_nfos(conn, nfo_tasks)
    if apply and report["changed"]:
        _refresh_jellyfin_after_apply(report)
    return report


def run(apply: bool = False, rollback: str | None = None,
        limit: int | None = None, queue_proposals: bool = False) -> dict:
    conn = db.connect((os.getenv("DATABASE_URL") or "").strip() or None)
    try:
        if rollback:
            return _rollback(conn, rollback)
        return _rebuild(conn, apply, limit, queue_proposals)
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply exact matches; uncertain proposals stay report-only")
    parser.add_argument("--queue-proposals", action="store_true",
                        help="also persist uncertain suggestions (optional, can add many review rows)")
    parser.add_argument("--rollback", help="restore performer lists from an applied run UUID")
    parser.add_argument("--limit", type=int,
                        help="preview only the N most recent Telegram entries (report-only)")
    parser.add_argument("--output", help="write JSON report to this file instead of stdout")
    args = parser.parse_args()
    if args.apply and args.rollback:
        parser.error("--apply and --rollback cannot be combined")
    if args.limit is not None and (args.apply or args.rollback or args.limit < 1):
        parser.error("--limit requires report-only mode and a positive number")
    if args.queue_proposals and not args.apply:
        parser.error("--queue-proposals requires --apply")
    result = run(args.apply, args.rollback, args.limit, args.queue_proposals)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
