#!/usr/bin/env python3
"""Report suspected duplicates BETWEEN providers (without auto-merge).

Rule (strong): >=2 shared performers + both durations known and diff <= limit.
Rule (weak): >=2 shared performers, but duration unknown or diff exceeds limit
  (for manual review).

Usage:
    python3 -m sync.find_dupes [--min-shared 2] [--max-dur-diff 60]
        [--db-url DATABASE_URL] [--output dupes.csv] [--format csv|txt]
"""

import argparse
import csv
import json
import os
from common import db
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

DB_URL = (os.getenv("DATABASE_URL") or "").strip() or None


def load_videos(db_url=None):
    conn = db.connect(db_url or DB_URL)
    try:
        rows = conn.execute(
            "SELECT id, provider, title, duration, performers FROM videos"
        ).fetchall()
    finally:
        conn.close()
    videos = []
    for vid, prov, title, dur, perf_raw in rows:
        try:
            perfs = json.loads(perf_raw) if perf_raw else []
        except Exception:
            perfs = []
        perf_set = {str(p).strip().lower() for p in perfs if str(p).strip()}
        if len(perf_set) < 2:
            continue  # need min. 2 for comparison
        try:
            dur = int(dur or 0)
        except (TypeError, ValueError):
            dur = 0
        videos.append({
            "id": vid, "provider": prov, "title": title or "",
            "duration": dur, "performers": perf_set,
        })
    return videos


def find_groups(videos, min_shared: int, max_dur_diff: int):
    # Inverted index: performer -> video (avoids O(n^2)).
    by_perf = defaultdict(set)
    for i, v in enumerate(videos):
        for p in v["performers"]:
            by_perf[p].add(i)
    pair_shared = defaultdict(set)
    for p, idxs in by_perf.items():
        for a, b in combinations(sorted(idxs), 2):
            pair_shared[(a, b)].add(p)
    groups = []
    for (a, b), shared in pair_shared.items():
        if len(shared) < min_shared:
            continue
        va, vb = videos[a], videos[b]
        if va["provider"] == vb["provider"]:
            continue  # internal duplicates within provider are handled by sync dedup
        da, db = va["duration"], vb["duration"]
        if da > 0 and db > 0 and abs(da - db) <= max_dur_diff:
            level = "STRONG"
        else:
            level = "WEAK"
        groups.append({
            "level": level,
            "shared": sorted(shared),
            "videos": [va, vb],
        })
    # Strong first, then by number of shared performers.
    groups.sort(key=lambda g: (g["level"] != "STRONG", -len(g["shared"])))
    return groups


def fmt_dur(sec: int) -> str:
    if sec <= 0:
        return "?"
    return f"{sec // 60}min"


def main() -> int:
    ap = argparse.ArgumentParser(description="Cross-provider duplicate report")
    ap.add_argument("--min-shared", type=int, default=2)
    ap.add_argument("--max-dur-diff", type=int, default=60)
    ap.add_argument("--db-url", default=DB_URL)
    ap.add_argument("--output", default="dupes_report.csv")
    ap.add_argument("--format", choices=["csv", "txt"], default="csv")
    args = ap.parse_args()

    videos = load_videos(args.db_url or None)
    print(f"Videos with >=2 performers: {len(videos)}")
    groups = find_groups(videos, args.min_shared, args.max_dur_diff)
    strong = sum(1 for g in groups if g["level"] == "STRONG")
    print(f"Pairs: {len(groups)} (STRONG: {strong}, WEAK: {len(groups) - strong})")

    out = Path(args.output)
    if args.format == "txt" or out.suffix == ".txt":
        with out.open("w", encoding="utf-8") as f:
            for i, g in enumerate(groups, 1):
                f.write(f"### {i}. [{g['level']}] shared: {', '.join(g['shared'])}\n")
                for v in g["videos"]:
                    f.write(f"  - [{v['provider']}] {v['title'][:90]} "
                            f"({fmt_dur(v['duration'])}) :: {v['id']}\n")
                f.write("\n")
    else:
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["group", "level", "shared", "provider",
                        "title", "duration_s", "video_id"])
            for i, g in enumerate(groups, 1):
                for v in g["videos"]:
                    w.writerow([i, g["level"], "; ".join(g["shared"]),
                                v["provider"], v["title"], v["duration"], v["id"]])
    print(f"Saved: {out}")
    print("\nTOP 15 (for manual review):")
    for i, g in enumerate(groups[:15], 1):
        print(f"{i}. [{g['level']}] {', '.join(g['shared'])}")
        for v in g["videos"]:
            print(f"    [{v['provider']}] {v['title'][:80]} ({fmt_dur(v['duration'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
