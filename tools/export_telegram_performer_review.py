#!/usr/bin/env python3
"""Export a blind, stratified Telegram performer labeling worksheet.

This is a read-only export. It intentionally never selects or writes the
current performer cast, legacy aliases, proposals, or external match results.
Keep the resulting CSV local: it contains Telegram captions and post links.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
from collections import defaultdict
from pathlib import Path


from common.db import connect


FIELDS = (
    "review_id", "split", "stratum", "video_id", "telegram_post_url",
    "stashdb_scene_url", "stashdb_scene_id", "scene_match_basis",
    "scene_match_hash", "scene_match_confidence",
    "source_text", "source_text_origin", "source_filename", "duration_seconds",
    "true_performer_names", "true_performer_ids", "cast_coverage",
    "label_confidence", "review_notes",
)
VIDEO_ID_RE = re.compile(r"^telegram:(-?\d+):(\d+):[^:]+$")
STRATA = (
    "scene_reference",
    "short_clip",
    "no_source_text",
    "no_filename",
    "long_text",
)


def _origin(source_text: str, plot: str) -> str:
    if source_text and plot and source_text == plot:
        return "uncertain_may_be_backfilled_from_plot"
    if source_text:
        return "stored_source_text_origin_unverified"
    return "missing"


def _stratum(row: dict) -> str:
    if row["scene_external_id"]:
        return "scene_reference"
    if row["duration_seconds"] is not None and 0 < int(row["duration_seconds"]) < 600:
        return "short_clip"
    if not row["source_text"]:
        return "no_source_text"
    if not row["source_filename"]:
        return "no_filename"
    return "long_text"


def _group_key(row: dict) -> str:
    # Keep known copies of the same external scene/exact byte sequence in one
    # split. Approximate pHash is deliberately not used to group clips.
    return str(row["scene_external_id"] or row["oshash"] or row["video_id"])


def _telegram_url(video_id: str) -> str:
    match = VIDEO_ID_RE.fullmatch(video_id)
    if not match:
        return ""
    channel_id, message_id = match.groups()
    public_channel_id = channel_id[4:] if channel_id.startswith("-100") else channel_id.lstrip("-")
    return f"https://t.me/c/{public_channel_id}/{message_id}"


def _spreadsheet_safe(value: str) -> str:
    """Prevent caption/filename text from being interpreted as a CSV formula."""
    text = str(value or "")
    if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def load_rows(db_url: str | None) -> list[dict]:
    conn = connect(db_url)
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout='30s'")
        rows = conn.execute(
            """SELECT v.id,v.source_text,v.source_filename,v.title,v.duration,v.plot,
                      v.scene_match_external_id,osh.fingerprint,ph.fingerprint
                 FROM videos v
                 LEFT JOIN video_fingerprints osh
                   ON osh.video_id=v.id AND osh.algorithm='OSHASH'
                 LEFT JOIN video_fingerprints ph
                   ON ph.video_id=v.id AND ph.algorithm='PHASH'
                WHERE v.provider='telegram'
                ORDER BY v.id"""
        ).fetchall()
        conn.rollback()
    finally:
        conn.close()

    result = []
    for raw in rows:
        video_id, source_text, source_filename, title, duration, plot, scene_id, oshash, phash = raw
        source_text = str(source_text or "")
        item = {
            "video_id": str(video_id),
            "source_text": source_text,
            "source_filename": str(source_filename or ""),
            "current_title": str(title or ""),
            "duration_seconds": duration,
            "source_text_origin": _origin(source_text, str(plot or "")),
            "scene_external_id": str(scene_id or ""),
            "oshash": str(oshash or ""),
            "phash": str(phash or ""),
        }
        item["stratum"] = _stratum(item)
        result.append(item)
    return result


def build_sample(rows: list[dict], per_stratum: int, seed: int) -> tuple[list[dict], dict]:
    rng = random.Random(seed)
    by_stratum: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_stratum[row["stratum"]].append(row)

    selected: list[dict] = []
    counts = {}
    for stratum in STRATA:
        candidates = sorted(by_stratum.get(stratum, []), key=lambda row: row["video_id"])
        # Sample scene/hash groups first to avoid repeated copies dominating a
        # stratum, then select one video from each chosen group.
        by_group: dict[str, list[dict]] = defaultdict(list)
        for row in candidates:
            by_group[_group_key(row)].append(row)
        groups = sorted(by_group)
        rng.shuffle(groups)
        chosen = []
        for group in groups:
            if len(chosen) >= per_stratum:
                break
            chosen.append(rng.choice(by_group[group]))
        selected.extend(chosen)
        counts[stratum] = {"available": len(candidates), "exported": len(chosen)}

    selected.sort(key=lambda row: (row["stratum"], row["video_id"]))
    # Balance each stratum across development and holdout. Since one video is
    # selected per scene/hash group, duplicate copies cannot leak across splits.
    odd_strata = [stratum for stratum in STRATA
                  if len([row for row in selected if row["stratum"] == stratum]) % 2]
    random.Random(f"{seed}:holdout-balance").shuffle(odd_strata)
    extra_holdout = len(selected) // 2 - sum(
        len([row for row in selected if row["stratum"] == stratum]) // 2
        for stratum in STRATA
    )
    ceil_holdout_strata = set(odd_strata[:extra_holdout])
    for stratum in STRATA:
        stratum_rows = [row for row in selected if row["stratum"] == stratum]
        group_keys = sorted({_group_key(row) for row in stratum_rows})
        random.Random(f"{seed}:{stratum}").shuffle(group_keys)
        holdout_count = len(group_keys) // 2 + int(stratum in ceil_holdout_strata)
        holdout_groups = set(group_keys[:holdout_count])
        for row in stratum_rows:
            row["split"] = "holdout" if _group_key(row) in holdout_groups else "dev"
    summary = {
        "available_telegram_videos": len(rows),
        "exported": len(selected),
        "by_stratum": counts,
        "by_split": {
            name: sum(1 for row in selected if row["split"] == name)
            for name in ("dev", "holdout")
        },
        "seed": seed,
    }
    return selected, summary


def write_csv(rows: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(output, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "review_id": hashlib.sha256(row["video_id"].encode("utf-8")).hexdigest()[:16],
                "split": row["split"],
                "stratum": row["stratum"],
                "video_id": row["video_id"],
                "telegram_post_url": _telegram_url(row["video_id"]),
                "stashdb_scene_url": "",
                "stashdb_scene_id": "",
                "scene_match_basis": "",  # oshash | phash | title/studio | manual
                "scene_match_hash": "",  # only when a fingerprint was used
                "scene_match_confidence": "",  # high | medium | low
                "source_text": _spreadsheet_safe(row["source_text"]),
                "source_text_origin": row["source_text_origin"],
                "source_filename": _spreadsheet_safe(row["source_filename"]),
                "duration_seconds": row["duration_seconds"] if row["duration_seconds"] is not None else "",
                "true_performer_names": "",
                "true_performer_ids": "",
                "cast_coverage": "",  # complete | partial | unknown
                "label_confidence": "",  # high | medium | low
                "review_notes": "",
            })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL") or None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-stratum", type=int, default=125)
    parser.add_argument("--seed", type=int, default=20261015)
    args = parser.parse_args()
    if args.per_stratum < 1:
        parser.error("--per-stratum must be at least 1")
    try:
        rows = load_rows(args.db_url)
        sample, summary = build_sample(rows, args.per_stratum, args.seed)
        write_csv(sample, args.output)
    except FileExistsError:
        print("Output already exists; choose a new path so labels are not overwritten.", file=sys.stderr)
        return 1
    except Exception as exc:
        # Avoid including DB URI or raw Telegram content in errors.
        print(f"Could not export review worksheet: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps({**summary, "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
