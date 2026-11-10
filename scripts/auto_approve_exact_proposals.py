#!/usr/bin/env python3
"""Auto-approve unambiguous exact catalog matches in telegram_performer_proposals.

For every pending proposal where the candidate unambiguously matches exactly one
active multi-word performer in performer_catalog:
1. Updates proposal status to 'approved' and sets performer_id.
2. Adds the canonical performer name to videos.performers (preserving existing performers).
3. Regenerates the corresponding NFO file if it exists.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from common import db
from common.performer_catalog import normalize_name
from common.performer_cleanup_support import regenerate_telegram_nfo
from common.performers import is_valid_performer_name
from sync import sync as S

MEDIA_DIR = Path(os.getenv("MEDIA_DIR", str(ROOT / "media" / "adult")))


def _regenerate_nfo(conn, video_id: str, performers: list[str]) -> bool:
    if S is None:
        return False
    return regenerate_telegram_nfo(conn, video_id, performers, media_dir=MEDIA_DIR, sync_api=S)


def _try_regenerate_nfo(conn, video_id: str, performers: list[str]) -> bool:
    try:
        return _regenerate_nfo(conn, video_id, performers)
    except Exception:
        return False


def _load_catalog_index(cursor) -> dict[str, list[tuple[str, str]]]:
    cursor.execute("SELECT id, name FROM performer_catalog WHERE active = TRUE")
    cat_rows = cursor.fetchall()

    cat_by_norm: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for cid, name in cat_rows:
        if is_valid_performer_name(name) and len(name.split()) >= 2:
            cat_by_norm[normalize_name(name)].append((cid, name))
    return cat_by_norm


def _fetch_pending(cursor) -> list:
    cursor.execute("""
        SELECT id, video_id, candidate
        FROM telegram_performer_proposals
        WHERE status = 'pending'
        ORDER BY id ASC
    """)
    return cursor.fetchall()


def _match_pending(pending, cat_by_norm):
    unambiguous_by_video: dict[str, list[tuple[int, str, str, str]]] = collections.defaultdict(list)
    total_matches = 0
    for pid, vid, cand in pending:
        cand_norm = normalize_name(cand)
        matches = cat_by_norm.get(cand_norm, [])
        unique_ids = {m[0] for m in matches}
        if len(unique_ids) == 1:
            cid, cname = matches[0]
            unambiguous_by_video[vid].append((pid, cand, cid, cname))
            total_matches += 1
    return unambiguous_by_video, total_matches


def _print_header(apply: bool, pending, unambiguous_by_video, total_matches) -> None:
    print("=" * 65)
    print("AUTO-APPROVE EXACT CATALOG PERFORMER PROPOSALS")
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Total pending proposals examined: {len(pending)}")
    print(f"Unambiguous exact catalog matches found: {total_matches}")
    print(f"Unique videos affected: {len(unambiguous_by_video)}")
    print("=" * 65)


def _print_samples(unambiguous_by_video) -> None:
    sample_count = 0
    print("\nSample matches (first 15):")
    for vid, items in list(unambiguous_by_video.items())[:15]:
        for pid, cand, cid, cname in items:
            sample_count += 1
            print(f"  [{pid}] Candidate: {cand!r} -> Match: {cname} ({cid})")
            print(f"        Video: {vid}")
            if sample_count >= 15:
                break
        if sample_count >= 15:
            break


def _current_performers(cur_video) -> list:
    current_performers = cur_video[0] or []
    if isinstance(current_performers, str):
        try:
            current_performers = json.loads(current_performers)
        except Exception:
            current_performers = []
    return current_performers


def _approve_items(conn, items, current_performers) -> tuple[list, bool]:
    """Mark each proposal approved and merge new names; return (performers, changed)."""
    seen_lower = {p.casefold() for p in current_performers if p}
    new_performers = list(current_performers)
    video_changed = False

    for pid, cand, cid, cname in items:
        conn.execute("""
            UPDATE telegram_performer_proposals
            SET status = 'approved', performer_id = %s
            WHERE id = %s
        """, (cid, pid))

        if cname.casefold() not in seen_lower:
            new_performers.append(cname)
            seen_lower.add(cname.casefold())
            video_changed = True
    return new_performers, video_changed


def _apply_approvals(conn, unambiguous_by_video) -> tuple[int, int, int]:
    approved_count = 0
    videos_updated = 0
    nfos_updated = 0

    with conn:
        for vid, items in unambiguous_by_video.items():
            cur_video = conn.execute("SELECT performers FROM videos WHERE id = %s FOR UPDATE", (vid,)).fetchone()
            if not cur_video:
                continue

            new_performers, video_changed = _approve_items(
                conn, items, _current_performers(cur_video)
            )
            approved_count += len(items)

            if video_changed:
                conn.execute("UPDATE videos SET performers = %s WHERE id = %s",
                             (json.dumps(new_performers, ensure_ascii=False), vid))
                videos_updated += 1
                if _try_regenerate_nfo(conn, vid, new_performers):
                    nfos_updated += 1

    return approved_count, videos_updated, nfos_updated


def _refresh_jellyfin() -> None:
    try:
        if S is not None and hasattr(S, "trigger_jellyfin_refresh") and S.trigger_jellyfin_refresh():
            print("Triggered Jellyfin library refresh.")
    except Exception as exc:
        print(f"Jellyfin refresh skipped/failed: {exc}")


def run(db_url: str | None = None, apply: bool = False) -> None:
    conn = db.connect(db_url)
    cursor = conn.cursor()

    cat_by_norm = _load_catalog_index(cursor)
    pending = _fetch_pending(cursor)
    unambiguous_by_video, total_matches = _match_pending(pending, cat_by_norm)

    _print_header(apply, pending, unambiguous_by_video, total_matches)

    if not total_matches:
        print("No matching proposals to approve.")
        return

    _print_samples(unambiguous_by_video)

    if not apply:
        print("\nDry-run completed. Run with --apply to commit approvals and update videos.")
        return

    print("\nApplying approvals and updating videos...")
    approved_count, videos_updated, nfos_updated = _apply_approvals(conn, unambiguous_by_video)

    print(f"Successfully approved {approved_count} proposals!")
    print(f"Updated {videos_updated} videos with new performers.")
    print(f"Regenerated {nfos_updated} NFO files.")

    _refresh_jellyfin()


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-approve exact catalog performer proposals")
    parser.add_argument("--apply", action="store_true", help="Apply changes to the database and videos")
    args = parser.parse_args()

    db_url = (os.getenv("DATABASE_URL") or "").strip() or None
    run(db_url=db_url, apply=args.apply)


if __name__ == "__main__":
    main()
