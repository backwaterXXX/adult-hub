#!/usr/bin/env python3
"""Merge duplicates across providers (canonical + denylist).

Merge rules (conservative):
  - STRONG from find_dupes.py (2+ shared + duration ~=), OR
  - WEAK with matching title (token overlap > --title-overlap).

The rest (other scenes with the same performers) remains untouched.

Keeper priority is supplied by the optional extension; unknown providers
sort after registered providers. Keeper gets tags
"Duplicate: <Provider>", dropped items go to purge + denylist (sync will
not add them back).

Usage:
    python3 -m sync.merge_dupes --dry-run [--db ...]
    python3 -m sync.merge_dupes --apply   # ONLY inside sync container (writes to DB)
"""

import argparse
import csv
import json
import os
import re
from common import db
import sys
from collections import defaultdict
from pathlib import Path


DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./media/adult"))

# Built-in source defaults live here. Unknown providers sort last
# (rank 9) with their raw name as display.
PROVIDER_RANK = {
    "telegram": 2,
}

PROVIDER_DISPLAY = {
    "telegram": "Telegram",
}


def tokens(title: str):
    return set(re.findall(r"[a-z]{3,}", (title or "").lower()))


def title_overlap(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


def _read_report_rows(csv_path: Path) -> list:
    with open(csv_path, encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _find_root(parent: dict, x):
    while parent[x] != x:
        parent[x] = parent[parent[x]]
        x = parent[x]
    return x


def _union_sets(parent: dict, a, b) -> None:
    parent.setdefault(a, a)
    parent.setdefault(b, b)
    parent[_find_root(parent, a)] = _find_root(parent, b)


def _pair_should_merge(members: list, title_overlap_min: float) -> bool:
    level = members[0]["level"]
    durs = [int(m.get("duration_s", m.get("czas_s", 0)) or 0) for m in members]
    unk = any(d == 0 for d in durs)
    t0 = members[0].get("title", members[0].get("tytul", ""))
    t1 = members[1].get("title", members[1].get("tytul", ""))
    t_overlap = title_overlap(t0, t1)

    # Guard: even STRONG matches must share some title tokens (min title_overlap_min or >= 0.35)
    # to prevent false positives from cards with leaked actor tags.
    if level == "STRONG":
        ok = t_overlap >= min(title_overlap_min, 0.35)
    else:
        ok = t_overlap >= title_overlap_min and (unk or abs(durs[0] - durs[1]) <= 180)

    if ok:
        from resolver.video_duplicate_scoring import _has_different_season_markers
        if _has_different_season_markers(t0, t1):
            ok = False
    return ok


def load_groups(csv_path: Path, title_overlap_min: float):
    """Returns list of clusters to merge (union-find over pairs)."""
    rows = _read_report_rows(csv_path)
    by_group = defaultdict(list)
    for r in rows:
        gid = r.get("group", r.get("grupa"))
        by_group[gid].append(r)
    parent = {}

    for gid, members in by_group.items():
        if len(members) != 2:
            continue
        if _pair_should_merge(members, title_overlap_min):
            _union_sets(parent, members[0]["video_id"], members[1]["video_id"])
    clusters = defaultdict(list)
    for vid in parent:
        clusters[_find_root(parent, vid)].append(vid)
    return [sorted(set(c)) for c in clusters.values() if len(set(c)) > 1]


def _build_merge_plan(clusters, db_ids) -> list:
    plan = []
    for cluster in clusters:
        present = [v for v in cluster if v in db_ids]
        if len(present) < 2:
            continue
        present.sort(key=lambda v: (PROVIDER_RANK.get(v.split(":")[0], 9), v))
        keeper, dropped = present[0], present[1:]
        plan.append((keeper, dropped))
    return plan


def _print_plan(plan) -> None:
    print(f"Clusters to merge: {len(plan)} "
          f"({sum(len(d) for _, d in plan)} videos to remove)")
    for keeper, dropped in plan:
        print(f"  KEEP [{keeper.split(':')[0]}] {keeper}")
        for d in dropped:
            print(f"    DROP [{d.split(':')[0]}] {d}")


def _keeper_tag_set(tags, dropped) -> set:
    try:
        tag_set = set(json.loads(tags)) if tags else set()
    except Exception:
        tag_set = set()
    for d in dropped:
        tag_set.add(f"Duplicate: {PROVIDER_DISPLAY.get(d.split(':')[0], d.split(':')[0])}")
    return tag_set


def _keeper_genres(cats) -> set:
    try:
        return set(json.loads(cats)) if cats else set()
    except Exception:
        return set()


def _keeper_performers(perfs) -> list:
    try:
        return json.loads(perfs) if perfs else []
    except Exception:
        return []


def _write_keeper_tags(conn, S, idx, keeper, video, genres, tag_set, media_dir) -> None:
    try:
        nfo_path = media_dir / f"{S.stable_media_basename(keeper)}.nfo"
        S.create_nfo_file(video, genres, tag_set, nfo_path)
        conn.execute("UPDATE videos SET tags = %s WHERE id = %s",
                     (json.dumps(sorted(tag_set)), keeper))
        if (idx + 1) % 50 == 0:
            conn.commit()
    except Exception as e:
        print(f"TAG-FAIL {keeper}: {e}")


def _merge_one_cluster(conn, S, idx, keeper, dropped, media_dir) -> int:
    """Tag the keeper, purge the dropped items, and return how many were removed."""
    # 1. Tagi dubletu na keeperze (DB + NFO).
    try:
        row = conn.execute(
            "SELECT provider, title, slug, duration, thumbnail, performers,"
            " categories, tags, studio, first_seen FROM videos WHERE id = %s",
            (keeper,)).fetchone()
    except Exception as e:
        print(f"SKIP {keeper}: {e}")
        return 0
    if not row:
        print(f"SKIP {keeper}: disappeared from DB in the meantime")
        return 0
    (prov, title, slug, dur, thumb, perfs, cats, tags, studio,
     first_seen) = row
    tag_set = _keeper_tag_set(tags, dropped)
    genres = _keeper_genres(cats)
    perf_list = _keeper_performers(perfs)
    video = {"id": keeper, "provider": prov, "provider_video_id": keeper.split(":", 1)[-1],
             "title": title or keeper, "slug": slug or "", "duration": dur or 0,
             "thumbnail": thumb or "", "performers": perf_list,
             "performer_thumbs": S.load_performer_thumbs(conn, perf_list),
             "studio": studio or "",
             "dateadded": S.format_dateadded(first_seen or __import__("time").time())}
    _write_keeper_tags(conn, S, idx, keeper, video, genres, tag_set, media_dir)
    # 2. Purge dropped + denylist (no reappear in sync).
    # Explicit user-invoked dedup: force bypasses the run-success signal
    # (there is no sync run here) but still preserves disabled sources.
    S.denylist_ids(conn, dropped, f"merged into {keeper}")
    return S.purge_videos(conn, media_dir, dropped, f"merged into {keeper}", force=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge duplicates across providers")
    ap.add_argument("--csv", default="dupes_report.csv")
    ap.add_argument("--db-url", default=DB_URL)
    ap.add_argument("--media", default=str(MEDIA_DIR))
    ap.add_argument("--title-overlap", type=float, default=0.35)
    ap.add_argument("--apply", action="store_true",
                    help="Apply (default: dry-run)")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        csv_path = Path("/tmp/dupes.csv")
    clusters = load_groups(csv_path, args.title_overlap)
    conn = db.connect(args.db_url or None)
    try:
        db_ids = {r[0] for r in conn.execute("SELECT id FROM videos").fetchall()}
    finally:
        conn.close()

    plan = _build_merge_plan(clusters, db_ids)
    _print_plan(plan)

    if not args.apply:
        print("\nDry-run. Run with --apply INSIDE sync container.")
        return 0

    from sync import sync as S
    media_dir = Path(args.media).resolve()
    conn = S.init_db(args.db_url or None)
    total_dropped = 0
    for idx, (keeper, dropped) in enumerate(plan):
        total_dropped += _merge_one_cluster(conn, S, idx, keeper, dropped, media_dir)
    print(f"Removed: {total_dropped}")
    conn.commit()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
