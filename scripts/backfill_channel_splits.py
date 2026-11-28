#!/usr/bin/env python3
"""Preview or apply combined-runtime slug matches for split parts in one channel.

The normal split matcher runs across the whole catalog. This script measures a
bounded batch for one source, using the same strict title, runtime, and cast
evidence rules. It does not invent metadata for incomplete part groups.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from common import db
from common.scene_titles import title_segments
from common.telegram_metadata import extract_telegram_metadata
from sync.scene_scraper_models import PART_MARKER_SQL
from sync.scene_scraper_split_parts import (
    _eligible_split_group,
    _group_split_parts,
    _search_split_group,
    parse_split_part_title,
    search_split_scene,
)

ATTEMPT_SOURCE = "telegram_split_title_duration_v1"


def _db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return url.replace("@postgres:5432", "@127.0.0.1:5432")


def _candidate_rows(conn, channel: str, limit: int, force: bool = False):
    attempt_column = (
        "TRUE" if force
        else "(a.next_attempt_at IS NULL OR a.next_attempt_at<=CURRENT_TIMESTAMP)"
    )
    attempt_filter = "" if force else (
        "AND (a.next_attempt_at IS NULL OR a.next_attempt_at<=CURRENT_TIMESTAMP)"
    )
    return conn.execute(
        f"""SELECT v.id,v.title,v.source_filename,v.source_text,v.duration,
                  v.performer_resolution,v.scene_match_external_id,
                  {attempt_column}
           FROM videos v
           LEFT JOIN external_metadata_attempts a
             ON a.entity_type='video' AND a.entity_key=v.id AND a.source=%s
           WHERE v.provider='telegram' AND v.channel_name=%s
             {attempt_filter}
             AND {PART_MARKER_SQL}
           ORDER BY v.first_seen ASC,v.id ASC
           LIMIT %s""",
        (ATTEMPT_SOURCE, channel, max(2000, int(limit) * 100)),
    ).fetchall()


def _has_title_cast(parts) -> bool:
    """True when a title segment looks like a cast list (A & B, A and B)."""
    return any(
        "&" in segment or "," in segment or " and " in segment.casefold()
        for segment in title_segments(parts[0]["title"])
    )


def _matched_parts(conn, channel: str) -> int:
    return int(conn.execute(
        "SELECT count(*) FROM videos WHERE provider='telegram' AND channel_name=%s "
        "AND scene_match_external_id IS NOT NULL", (channel,)
    ).fetchone()[0])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", required=True, help="exact videos.channel_name")
    ap.add_argument("--limit", type=int, default=20, help="maximum complete groups to search")
    ap.add_argument("--apply", action="store_true", help="apply accepted matches (default: dry run)")
    ap.add_argument("--sleep", type=float, default=0.5, help="delay between group lookups")
    ap.add_argument("--show", type=int, default=20, help="matches to print")
    ap.add_argument("--force", action="store_true",
                    help="retry groups even when their retry cooldown has not elapsed")
    ap.add_argument("--cast-only", action="store_true",
                    help="only search groups whose title contains a cast segment")
    args = ap.parse_args()

    media_dir = Path(os.getenv("MEDIA_DIR") or "media/adult")
    conn = db.connect(_db_url())
    try:
        before = _matched_parts(conn, args.channel)
        rows = _candidate_rows(conn, args.channel, args.limit, force=args.force)
        groups = _group_split_parts(rows, extract_telegram_metadata, parse_split_part_title)
        eligible = []
        for parts_by_number in groups.values():
            group = _eligible_split_group(parts_by_number)
            if group is None:
                continue
            if args.cast_only and not _has_title_cast(group[0]):
                continue
            eligible.append(group)

        print(f"Channel: {args.channel}")
        print(f"Matched parts before: {before}")
        print(f"Candidate rows: {len(rows)}; complete groups: {len(eligible)}")

        accepted = []
        for index, (parts, total_duration) in enumerate(eligible[:max(0, args.limit)]):
            if index and args.sleep > 0:
                time.sleep(args.sleep)
            title = parts[0]["title"]
            source_texts = [text for part in parts
                            for text in (part["source_text"], part["source_filename"])]
            cast = list(dict.fromkeys(
                name for part in parts for name in part["explicit_cast"] if str(name).strip()
            ))
            scene = search_split_scene(
                title, duration=total_duration, source_texts=source_texts,
                caption_performers=cast, duration_tolerance=30,
            )
            if scene is None:
                continue
            accepted.append((parts, total_duration, scene))
            cast_text = ", ".join(scene.performers[:6])
            print(f"  MATCH {title[:90]} | {total_duration}s -> {scene.title!r} "
                  f"({scene.duration}s, {scene.source}) | {cast_text}")

        print(f"Accepted groups: {len(accepted)}")
        if args.apply and accepted:
            now = int(time.time())
            updated = 0
            for parts, total_duration, scene in accepted:
                updated += _search_split_group(
                    conn, media_dir, parts, total_duration, now, None,
                    ATTEMPT_SOURCE, lambda *_args, _scene=scene, **_kwargs: _scene,
                )
            print(f"Updated parts: {updated}")
            print(f"Matched parts after: {_matched_parts(conn, args.channel)}")
        else:
            print("Dry run only; pass --apply to write.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
