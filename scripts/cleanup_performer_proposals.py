#!/usr/bin/env python3
"""Cleanup pending performer proposals in telegram_performer_proposals.

Strips aggregator prefixes (OnlyFans, OF), studio prefixes (TimTales, Tim Tales),
bracketed tags and nationality demonyms from candidate proposals. Rejects
pure studios, non-performers, and demonym descriptors.
"""
from __future__ import annotations

import argparse
import collections
import os


from common import db
from common.performer_catalog import clean_credit_candidate, normalize_name
from common.performers import (
    STUDIO_NAMES,
    STUDIO_SUFFIXES,
    SINGLE_WORD_STOPWORDS,
    TITLE_STOPWORDS,
    is_non_performer_name,
    is_valid_performer_name,
)


def evaluate_proposal(cand: str) -> tuple[str, str]:
    """Returns (cleaned_candidate, status_code)."""
    cleaned = clean_credit_candidate(cand)
    if not cleaned:
        return "", "empty"

    low = cleaned.lower()
    if is_non_performer_name(cleaned) or low in {"onlyfans", "of"}:
        return "", "aggregator"
    if low in STUDIO_NAMES or any(low.endswith(sfx) for sfx in STUDIO_SUFFIXES):
        return "", "studio"
    if low in SINGLE_WORD_STOPWORDS or low in TITLE_STOPWORDS:
        return "", "stopword"

    words = cleaned.split()
    explicit_name_shape = (
        2 <= len(words) <= 4 and all(word[:1].isupper() for word in words)
    ) or (len(words) == 1 and any(c.isupper() for c in cleaned[1:]))

    if not explicit_name_shape or not is_valid_performer_name(cleaned):
        return "", "invalid_name"

    return cleaned, "ok"


def _fetch_rows_and_index(cursor):
    cursor.execute("""
        SELECT id, video_id, candidate, normalized, matched_text
        FROM telegram_performer_proposals
        WHERE status = 'pending'
        ORDER BY id ASC
    """)
    rows = cursor.fetchall()

    cursor.execute("SELECT video_id, normalized FROM telegram_performer_proposals")
    all_by_video: dict[str, set[str]] = collections.defaultdict(set)
    for vid, norm in cursor.fetchall():
        all_by_video[vid].add(norm)
    return rows, all_by_video


def _classify_row(pid, vid, cand, norm, all_by_video):
    """Return (bucket, item); bucket is reject, update, conflict, or unchanged."""
    cleaned, status = evaluate_proposal(cand)
    if status != "ok":
        return "reject", (pid, cand, status)
    new_norm = normalize_name(cleaned)
    if not new_norm:
        return "reject", (pid, cand, "empty_norm")
    if cleaned == cand and new_norm == norm:
        return "unchanged", None
    if new_norm == norm:
        return "update", (pid, cleaned, new_norm, cand)
    if new_norm in all_by_video[vid]:
        return "conflict", (pid, cand, cleaned, vid, new_norm)
    all_by_video[vid].add(new_norm)
    return "update", (pid, cleaned, new_norm, cand)


def _classify_rows(rows, all_by_video):
    to_reject: list[tuple[int, str, str]] = []
    to_update: list[tuple[int, str, str, str]] = []
    to_delete_conflict: list[tuple[int, str, str, str, str]] = []
    unchanged = 0

    for pid, vid, cand, norm, _matched in rows:
        bucket, item = _classify_row(pid, vid, cand, norm, all_by_video)
        if bucket == "unchanged":
            unchanged += 1
        elif bucket == "reject":
            to_reject.append(item)
        elif bucket == "update":
            to_update.append(item)
        else:
            to_delete_conflict.append(item)
    return to_reject, to_update, to_delete_conflict, unchanged


def _print_summary(apply, rows, unchanged, to_update, to_reject, to_delete_conflict) -> None:
    print("=" * 60)
    print("TELEGRAM PERFORMER PROPOSALS CLEANUP")
    print(f"Mode: {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Total pending proposals examined: {len(rows)}")
    print(f"Unchanged: {unchanged}")
    print(f"To update (cleaned candidate name): {len(to_update)}")
    print(f"To reject (pure studio / stopword / aggregator / invalid): {len(to_reject)}")
    print(f"To reject / deduplicate (conflict with existing proposal): {len(to_delete_conflict)}")
    print("=" * 60)

    if to_update:
        print("\nSample updates (first 10):")
        for pid, cleaned, new_norm, old_cand in to_update[:10]:
            print(f"  [{pid}] {old_cand!r} -> {cleaned!r} (norm: {new_norm})")

    if to_reject:
        print("\nSample rejections (first 10):")
        for pid, old_cand, reason in to_reject[:10]:
            print(f"  [{pid}] {old_cand!r} -> rejected ({reason})")

    if to_delete_conflict:
        print("\nSample deduplication conflicts (first 10):")
        for pid, old_cand, cleaned, vid, new_norm in to_delete_conflict[:10]:
            print(f"  [{pid}] {old_cand!r} -> clean {cleaned!r} already exists on video {vid}")


def _apply_changes(conn, to_update, to_reject, to_delete_conflict) -> None:
    print("\nApplying changes to database...")
    with conn:
        for pid, cleaned, new_norm, _ in to_update:
            conn.execute("""
                UPDATE telegram_performer_proposals
                SET candidate = %s, normalized = %s
                WHERE id = %s
            """, (cleaned, new_norm, pid))

        for pid, _, reason in to_reject:
            conn.execute("""
                UPDATE telegram_performer_proposals
                SET status = 'rejected'
                WHERE id = %s
            """, (pid,))

        for pid, _, _, _, _ in to_delete_conflict:
            conn.execute("""
                UPDATE telegram_performer_proposals
                SET status = 'rejected'
                WHERE id = %s
            """, (pid,))

    print("Successfully updated database!")


def run_cleanup(db_url: str | None = None, apply: bool = False) -> None:
    conn = db.connect(db_url)
    cursor = conn.cursor()

    rows, all_by_video = _fetch_rows_and_index(cursor)
    to_reject, to_update, to_delete_conflict, unchanged = _classify_rows(rows, all_by_video)

    _print_summary(apply, rows, unchanged, to_update, to_reject, to_delete_conflict)

    if not apply:
        print("\nDry run completed. Run with --apply to commit changes.")
        return

    _apply_changes(conn, to_update, to_reject, to_delete_conflict)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean up telegram_performer_proposals")
    parser.add_argument("--apply", action="store_true", help="Apply changes to the database")
    args = parser.parse_args()

    db_url = (os.getenv("DATABASE_URL") or "").strip() or None
    run_cleanup(db_url=db_url, apply=args.apply)


if __name__ == "__main__":
    main()
