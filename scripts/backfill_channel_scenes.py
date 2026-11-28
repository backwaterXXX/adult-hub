#!/usr/bin/env python3
"""Preview or apply exact-title scene matches for one Telegram channel.

The regular scene backfill runs across the whole catalog. This script lets an
operator measure a bounded batch for the largest source first. It only accepts
the same strict, title-plus-runtime matches used by the normal pipeline; the
default is a dry run.
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from common import db
from common.scene_titles import SceneLookupUnavailable
from common.telegram_metadata import extract_telegram_metadata
from sync.scene_scraper_text_sources import search_scene_by_exact_title


def _db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return url.replace("@postgres:5432", "@127.0.0.1:5432")


ATTEMPT_SOURCE = "telegram_caption_title_exact_v1"


def _candidate_rows(conn, channel: str, limit: int, newest: bool = False,
                    retry_no_match: bool = False):
    order = "v.first_seen DESC NULLS LAST, v.id DESC" if newest else "v.first_seen ASC NULLS FIRST, v.id ASC"
    attempt_filter = (
        "AND (a.entity_key IS NULL OR a.outcome='no_match')"
        if retry_no_match else "AND a.entity_key IS NULL"
    )
    return conn.execute(
        f"""SELECT v.id, v.source_text, v.duration
            FROM videos v
            LEFT JOIN external_metadata_attempts a
              ON a.entity_type='video' AND a.entity_key=v.id AND a.source=%s
            WHERE v.provider='telegram' AND v.channel_name=%s
              AND v.source_text IS NOT NULL AND btrim(v.source_text)<>''
              AND COALESCE((v.performer_resolution->>'priority')::integer,0)<70
              AND v.scene_match_external_id IS NULL
              {attempt_filter}
            ORDER BY {order}
            LIMIT %s""",
        (ATTEMPT_SOURCE, channel, max(1, int(limit))),
    ).fetchall()


def _search_with_retries(title: str, duration: int, video_id: str, *,
                         retries: int, delay: float):
    for attempt in range(max(0, int(retries)) + 1):
        try:
            return search_scene_by_exact_title(title, duration=duration, duration_tolerance=2)
        except SceneLookupUnavailable as exc:
            if attempt >= max(0, int(retries)):
                print(f"  RETRY LATER {video_id} | {title[:80]} | {exc}")
                return None
            time.sleep(max(0.0, float(delay)))
    return None


def _counts(conn, channel: str) -> tuple[int, int]:
    row = conn.execute(
        """SELECT count(*) FILTER (WHERE performers='[]'::jsonb),
                  count(*) FILTER (WHERE scene_match_external_id IS NOT NULL)
           FROM videos WHERE provider='telegram' AND channel_name=%s""",
        (channel,),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--channel", required=True, help="exact videos.channel_name")
    ap.add_argument("--limit", type=int, default=20, help="maximum videos to query")
    ap.add_argument("--apply", action="store_true", help="apply accepted matches (default: dry run)")
    ap.add_argument("--sleep", type=float, default=0.5, help="delay between lookups")
    ap.add_argument("--newest", action="store_true", help="sample newest videos first")
    ap.add_argument("--retry-no-match", action="store_true",
                    help="also retry rows whose last exact-title attempt was no_match")
    ap.add_argument("--retries", type=int, default=2,
                    help="retries after a transient catalogue error")
    ap.add_argument("--retry-sleep", type=float, default=15.0,
                    help="seconds between transient-error retries")
    args = ap.parse_args()

    media_dir = Path(os.getenv("MEDIA_DIR") or "media/adult")
    conn = db.connect(_db_url())
    try:
        before_cast, before_matched = _counts(conn, args.channel)
        rows = _candidate_rows(
            conn, args.channel, args.limit,
            newest=args.newest, retry_no_match=args.retry_no_match,
        )
        print(f"Channel: {args.channel}")
        print(f"Before: no cast={before_cast}, matched scenes={before_matched}")
        print(f"Candidates inspected: {len(rows)}")

        accepted = []
        for index, (video_id, source_text, duration) in enumerate(rows):
            if index and args.sleep > 0:
                time.sleep(args.sleep)
            metadata = extract_telegram_metadata(str(source_text or ""))
            title = str(metadata.get("title") or "").strip()
            if not title:
                continue
            scene = _search_with_retries(
                title, int(duration or 0), str(video_id),
                retries=args.retries, delay=args.retry_sleep,
            )
            if scene is None:
                continue
            accepted.append((str(video_id), title, scene))
            cast = ", ".join(str(name) for name in (scene.performers or []) if name)
            print(f"  MATCH {video_id} | {title} | {scene.source} "
                  f"| cast: {cast[:100]}")

        print(f"Accepted matches: {len(accepted)}")
        if args.apply and accepted:
            from sync.scene_scrapers import _apply_matched_scene, record_attempt

            now = int(time.time())
            applied = 0
            for video_id, _title, scene in accepted:
                applied += int(_apply_matched_scene(
                    conn, media_dir, video_id, scene, http_client=None, now=now,
                ))
                if record_attempt:
                    record_attempt(conn, "video", video_id, ATTEMPT_SOURCE, "found")
            after_cast, after_matched = _counts(conn, args.channel)
            print(f"Applied matches: {applied}")
            print(f"After: no cast={after_cast}, matched scenes={after_matched}")
        else:
            print("Dry run only; pass --apply to write.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
