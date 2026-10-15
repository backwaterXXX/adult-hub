#!/usr/bin/env python3
"""Export duplicate-video suggestions and historical decisions for review.

The CSV is a human review worksheet, not a statistically unbiased estimate of
model quality. It is read-only and does not change decisions or scoring rules.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for source_root in (ROOT / "resolver", ROOT):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from common.db import connect  # noqa: E402
from video_duplicates import scan_duplicate_index  # noqa: E402


FIELDS = (
    "review_id",
    "review_source",
    "category",
    "evidence",
    "score",
    "rules_version",
    "video_a_id",
    "title_a",
    "provider_a",
    "duration_a",
    "performers_a",
    "studio_a",
    "video_b_id",
    "title_b",
    "provider_b",
    "duration_b",
    "performers_b",
    "studio_b",
    "same_scene",
    "preferred_keeper_id",
    "reviewer",
    "notes",
)


def _row_id(left_id: str, right_id: str, source: str) -> str:
    pair = "\0".join(sorted((left_id, right_id))) + "\0" + source
    return hashlib.sha256(pair.encode("utf-8")).hexdigest()[:16]


def _json_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _pair_record(source: str, category: str, evidence: str, score, rules_version,
                 left: dict, right: dict) -> dict:
    left_id = str(left.get("id") or "")
    right_id = str(right.get("id") or "")
    return {
        "review_id": _row_id(left_id, right_id, source),
        "review_source": source,
        "category": category or "",
        "evidence": evidence or "",
        "score": score if score is not None else "",
        "rules_version": rules_version if rules_version is not None else "",
        "video_a_id": left_id,
        "title_a": str(left.get("title") or ""),
        "provider_a": str(left.get("provider") or ""),
        "duration_a": left.get("duration") if left.get("duration") is not None else "",
        "performers_a": _json_cell(left.get("performers")),
        "studio_a": str(left.get("studio") or ""),
        "video_b_id": right_id,
        "title_b": str(right.get("title") or ""),
        "provider_b": str(right.get("provider") or ""),
        "duration_b": right.get("duration") if right.get("duration") is not None else "",
        "performers_b": _json_cell(right.get("performers")),
        "studio_b": str(right.get("studio") or ""),
        "same_scene": "",
        "preferred_keeper_id": "",
        "reviewer": "",
        "notes": "",
    }


def _history_rows(db_url: str | None) -> tuple[list[dict], list[dict]]:
    conn = connect(db_url or None)
    try:
        decisions = conn.execute(
            """SELECT d.keeper_id,d.loser_id,d.rules_version,d.snapshot,
                      a.provider,a.title,a.duration,a.performers,a.studio
                 FROM video_duplicate_decisions d
                 LEFT JOIN videos a ON a.id=d.keeper_id
                 WHERE d.state='active'"""
        ).fetchall()
        dismissals = conn.execute(
            """SELECT d.left_id,d.right_id,
                      a.provider,a.title,a.duration,a.performers,a.studio,
                      b.provider,b.title,b.duration,b.performers,b.studio
                 FROM video_duplicate_dismissals d
                 JOIN videos a ON a.id=d.left_id
                 JOIN videos b ON b.id=d.right_id
                 WHERE d.active=1"""
        ).fetchall()
    finally:
        conn.close()

    hidden_rows = []
    for row in decisions:
        keeper_id, loser_id, rules_version, raw_snapshot, *fields = row
        left = {"id": keeper_id, **dict(zip(("provider", "title", "duration", "performers", "studio"), fields))}
        if isinstance(raw_snapshot, str):
            try:
                raw_snapshot = json.loads(raw_snapshot)
            except (TypeError, ValueError):
                raw_snapshot = None
        video_snapshot = raw_snapshot.get("video") if isinstance(raw_snapshot, dict) else None
        right = dict(video_snapshot) if isinstance(video_snapshot, dict) else {}
        right["id"] = loser_id
        hidden_rows.append(_pair_record("historical_hide", "accepted_hide", "user_decision", "", rules_version,
                                        left, right))

    dismissed_rows = []
    for row in dismissals:
        left_id, right_id, *fields = row
        left = {"id": left_id, **dict(zip(("provider", "title", "duration", "performers", "studio"), fields[:5]))}
        right = {"id": right_id, **dict(zip(("provider", "title", "duration", "performers", "studio"), fields[5:]))}
        dismissed_rows.append(_pair_record("historical_dismissal", "dismissed", "user_decision", "", "",
                                           left, right))
    return hidden_rows, dismissed_rows


def _sample(rows_by_stratum: dict[tuple, list[dict]], per_stratum: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    selected = []
    for stratum in sorted(rows_by_stratum, key=str):
        rows = sorted(rows_by_stratum[stratum], key=lambda row: row["review_id"])
        selected.extend(rng.sample(rows, min(per_stratum, len(rows))))
    return selected


def build_corpus(db_url: str | None, per_stratum: int, seed: int) -> tuple[list[dict], dict]:
    scan = scan_duplicate_index(db_url)
    if not scan.get("complete") or scan.get("candidate_limit_reached"):
        raise RuntimeError("Candidate scan was truncated; refusing to export an incomplete sample")

    current_strata: dict[tuple, list[dict]] = defaultdict(list)
    for pair in scan.get("pairs", []):
        if pair.get("category") == "Same file":
            continue
        row = _pair_record(
            "current_suggestion", pair.get("category"), pair.get("evidence"), pair.get("score"),
            pair.get("rules_version"), pair.get("left") or {}, pair.get("right") or {},
        )
        current_strata[(row["category"], row["evidence"])].append(row)

    hidden_rows, dismissed_rows = _history_rows(db_url)
    hidden_strata: dict[tuple, list[dict]] = defaultdict(list)
    for row in hidden_rows:
        version = row["rules_version"] if row["rules_version"] != "" else "legacy"
        hidden_strata[(version, row["provider_a"], row["provider_b"])].append(row)

    dismissed_strata: dict[tuple, list[dict]] = defaultdict(list)
    for row in dismissed_rows:
        dismissed_strata[(row["provider_a"], row["provider_b"])].append(row)

    result = _sample(current_strata, per_stratum, seed)
    result += _sample(hidden_strata, per_stratum, seed + 1)
    result += _sample(dismissed_strata, per_stratum, seed + 2)
    result.sort(key=lambda row: (row["review_source"], row["review_id"]))
    summary = {
        "current_suggestions": sum(map(len, current_strata.values())),
        "active_hides": len(hidden_rows),
        "active_dismissals": len(dismissed_rows),
        "exported": len(result),
        "rules_version": scan.get("rules_version"),
        "videos_scanned": scan.get("scanned"),
    }
    return result, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL") or None)
    parser.add_argument("--output", type=Path, required=True, help="CSV output path; keep it local")
    parser.add_argument("--per-stratum", type=int, default=25)
    parser.add_argument("--seed", type=int, default=20261012)
    args = parser.parse_args()
    if args.per_stratum < 1:
        parser.error("--per-stratum must be at least 1")
    try:
        rows, summary = build_corpus(args.db_url, args.per_stratum, args.seed)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    except Exception as exc:
        print(f"Could not export duplicate review corpus: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("Exported duplicate review corpus: " + json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
