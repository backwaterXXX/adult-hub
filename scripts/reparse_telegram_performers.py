#!/usr/bin/env python3
"""Reparse performer names from video titles using improved multi-part title parsing."""
import json
import os
from common import db
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(BASE_DIR / "sync"))
sys.path.insert(0, str(BASE_DIR / "resolver"))

from common.performers import is_valid_performer_name, NATIONALITY_PREFIXES
from common.performer_titles import parse_title_performers_with_known_map
import sync as S

DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None
MEDIA_DIR = BASE_DIR / "media" / "adult"


def _name_is_word_subset(short_name: str, long_name: str) -> bool:
    short_words = str(short_name or "").lower().split()
    long_words = str(long_name or "").lower().split()
    return bool(short_words) and any(
        long_words[index:index + len(short_words)] == short_words
        for index in range(len(long_words) - len(short_words) + 1)
    )


def run():
    conn = db.connect(DB_URL)
    known_names = [
        r[0]
        for r in conn.execute("SELECT name FROM performer_bios").fetchall()
        if is_valid_performer_name(r[0])
    ]
    known_map = {n.lower(): n for n in known_names}
    known_map.update({
        str(alias).strip().lower(): str(canonical).strip()
        for alias, canonical in conn.execute(
            "SELECT alias, canonical FROM performer_aliases"
        ).fetchall()
        if str(alias).strip() and str(canonical).strip()
    })

    rows = conn.execute(
        "SELECT id, provider, title, slug, duration, thumbnail, performers, categories, tags, studio, first_seen "
        "FROM videos WHERE provider = 'telegram'"
    ).fetchall()

    videos_updated = 0
    nfos_updated = 0

    print(f"Scanning {len(rows)} Telegram videos...")
    with conn:
        for row in rows:
            (
                vid_id,
                prov,
                title,
                slug,
                dur,
                thumb,
                perfs_raw,
                cats_raw,
                tags_raw,
                studio,
                first_seen,
            ) = row

            try:
                old_perfs = json.loads(perfs_raw or "[]")
            except Exception:
                old_perfs = []

            raw_title = title or ""
            new_perfs = parse_title_performers_with_known_map(raw_title, known_map)

            if not new_perfs:
                continue

            old_multi = sum(1 for p in old_perfs if len(p.split()) >= 2)
            new_multi = sum(1 for p in new_perfs if len(p.split()) >= 2)

            should_update = False
            if new_perfs != old_perfs:
                if not old_perfs:
                    should_update = True
                elif new_multi > old_multi:
                    should_update = True
                elif new_multi == old_multi and len(new_perfs) > len(old_perfs):
                    should_update = True
                elif all(len(p.split()) == 1 for p in old_perfs) and new_multi > 0:
                    should_update = True
                elif any(
                    old_name.lower() not in {name.lower() for name in new_perfs}
                    and (
                        any(_name_is_word_subset(old_name, new_name) for new_name in new_perfs)
                        or any(_name_is_word_subset(new_name, old_name) for new_name in new_perfs)
                    )
                    for old_name in old_perfs
                ):
                    should_update = True
                elif any(
                    any(old_name.lower().startswith(p + " ") for p in NATIONALITY_PREFIXES)
                    for old_name in old_perfs
                ):
                    should_update = True

            if should_update:
                videos_updated += 1
                conn.execute(
                    "UPDATE videos SET performers = %s WHERE id = %s",
                    (json.dumps(new_perfs, ensure_ascii=False), vid_id),
                )

                nfo_path = MEDIA_DIR / f"{S.stable_media_basename(vid_id)}.nfo"
                if nfo_path.exists():
                    try:
                        tag_set = set(json.loads(tags_raw)) if tags_raw else set()
                    except Exception:
                        tag_set = set()
                    try:
                        genres = set(json.loads(cats_raw)) if cats_raw else set()
                    except Exception:
                        genres = set()

                    video_dict = {
                        "id": vid_id,
                        "provider": prov,
                        "provider_video_id": vid_id.split(":", 1)[-1],
                        "title": title or vid_id,
                        "slug": slug or "",
                        "duration": dur or 0,
                        "thumbnail": thumb or "",
                        "performers": new_perfs,
                        "performer_thumbs": S.load_performer_thumbs(conn, new_perfs),
                        "studio": studio or "",
                        "dateadded": S.format_dateadded(first_seen or time.time()),
                    }
                    try:
                        S.create_nfo_file(video_dict, genres, tag_set, nfo_path)
                        nfos_updated += 1
                    except Exception:
                        pass

    conn.close()
    print(
        f"Database & NFO update complete: {videos_updated} videos updated, {nfos_updated} NFOs regenerated."
    )

if __name__ == "__main__":
    run()
