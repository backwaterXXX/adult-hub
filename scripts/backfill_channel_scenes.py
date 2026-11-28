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
import re
import time
from pathlib import Path

from common import db
from common.performers import strip_scene_suffix
from common.telegram_metadata import extract_telegram_metadata
from sync.scene_scraper_text_sources import search_scene_by_exact_title


def _db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set")
    return url.replace("@postgres:5432", "@127.0.0.1:5432")


ATTEMPT_SOURCE = "telegram_caption_title_exact_v1"
_YEAR_PARENS_RE = re.compile(r"\s*\((?:19|20)\d{2}\)")
_DASH_PARTS_RE = re.compile(r"\s+[–—\-]\s+")


def _title_variants(title: str) -> list[str]:
    """Return conservative title spellings to try against external catalogs."""
    base = _YEAR_PARENS_RE.sub("", str(title or "")).strip()
    base = strip_scene_suffix(base).strip()
    variants = [base]
    parts = [part.strip() for part in _DASH_PARTS_RE.split(base) if part.strip()]
    if len(parts) > 1:
        variants.append(" - ".join(parts[1:]))
        variants.append(parts[-1])
    seen = set()
    result = []
    for value in variants:
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _find_scene(title: str, duration: int):
    for variant in _title_variants(title):
        scene = search_scene_by_exact_title(
            variant, duration=duration, duration_tolerance=2,
        )
        if scene is not None:
            return variant, scene
    return None, None


def _candidate_rows(conn, channel: str, limit: int, newest: bool = False):
    order = "v.first_seen DESC NULLS LAST, v.id DESC" if newest else "v.first_seen ASC NULLS FIRST, v.id ASC"
    return conn.execute(
        f"""SELECT v.id, v.source_text, v.duration
            FROM videos v
            LEFT JOIN external_metadata_attempts a
              ON a.entity_type='video' AND a.entity_key=v.id AND a.source=%s
            WHERE v.provider='telegram' AND v.channel_name=%s
              AND v.source_text IS NOT NULL AND btrim(v.source_text)<>''
              AND COALESCE((v.performer_resolution->>'priority')::integer,0)<70
              AND v.scene_match_external_id IS NULL
              AND a.entity_key IS NULL
            ORDER BY {order}
            LIMIT %s""",
        (ATTEMPT_SOURCE, channel, max(1, int(limit))),
    ).fetchall()


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
    args = ap.parse_args()

    media_dir = Path(os.getenv("MEDIA_DIR") or "media/adult")
    conn = db.connect(_db_url())
    try:
        before_cast, before_matched = _counts(conn, args.channel)
        rows = _candidate_rows(conn, args.channel, args.limit, newest=args.newest)
        print(f"Channel: {args.channel}")
        print(f"Before: no cast={before_cast}, matched scenes={before_matched}")
        print(f"Candidates inspected: {len(rows)}")

        accepted = []
        for index, (video_id, source_text, duration) in enumerate(rows):
            if index and args.sleep > 0:
                time.sleep(args.sleep)
            metadata = extract_telegram_metadata(str(source_text or ""))
            title = str(metadata.get("title") or "").strip()
            searched, scene = _find_scene(title, int(duration or 0)) if title else (None, None)
            if scene is None:
                continue
            accepted.append((str(video_id), searched or title, scene))
            cast = ", ".join(str(name) for name in (scene.performers or []) if name)
            print(f"  MATCH {video_id} | {searched or title} | {scene.source} "
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
