#!/usr/bin/env python3
"""Validate reviewed labels and convert the worksheet into benchmark JSONL."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path


from resolver.providers.telegram_messages import (
    _filename_to_title,
    _filter_caption_for_title,
    parse_caption_title,
)


REQUIRED_FIELDS = {
    "review_id", "split", "video_id", "source_text", "source_filename",
    "true_performer_names", "true_performer_ids", "cast_coverage", "label_confidence",
}
VALID_COVERAGE = {"complete", "partial", "unknown"}
VALID_CONFIDENCE = {"high", "medium", "low"}


def _parts(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def _aligned_ids(value: str, count: int) -> list[str]:
    raw = str(value or "")
    if not raw.strip():
        return [""] * count
    parts = [part.strip() for part in raw.split(";")]
    if len(parts) != count:
        raise ValueError("performer ID entries must align positionally with performer names")
    return parts


def _unescape_sheet(value: str) -> str:
    text = str(value or "")
    if len(text) > 1 and text[0] == "'" and text[1] in "=+-@\t\r":
        return text[1:]
    return text


def _load_catalog(catalog_path: Path) -> tuple[list, set[str]]:
    raw_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog = raw_catalog.get("performers") if isinstance(raw_catalog, dict) else raw_catalog
    if not isinstance(catalog, list) or not catalog:
        raise ValueError("catalog snapshot must contain a nonempty performers array")
    if any(not isinstance(item, dict) or not str(item.get("id") or "").startswith("stashdb:")
           for item in catalog):
        raise ValueError("catalog snapshot may contain only external stashdb identities")
    return catalog, {str(item["id"]) for item in catalog}


def _check_header(fieldnames) -> None:
    missing = REQUIRED_FIELDS - set(fieldnames or ())
    if missing:
        raise ValueError("review worksheet is missing required fields: " + ", ".join(sorted(missing)))
    if "performers" in (fieldnames or ()):
        raise ValueError("refusing a worksheet that contains the current performer cast")


def _read_video_id(row: dict, line_number: int, seen: set[str]) -> str:
    video_id = str(row.get("video_id") or "").strip()
    if not video_id or video_id in seen:
        raise ValueError(f"line {line_number} has a missing or duplicate video_id")
    seen.add(video_id)
    return video_id


def _aligned_names_and_ids(row: dict, line_number: int) -> tuple[list[str], list[str]]:
    names = _parts(row.get("true_performer_names") or "")
    try:
        ids = _aligned_ids(row.get("true_performer_ids") or "", len(names))
    except ValueError as exc:
        raise ValueError(f"line {line_number}: {exc}") from exc
    return names, ids


def _truth_for_label(row: dict, line_number: int, names: list[str], ids: list[str],
                     coverage: str, confidence: str, valid_ids: set[str]) -> tuple[str, list[str]]:
    if confidence == "low":
        return "unknown", []
    if coverage == "unknown":
        if any(ids):
            raise ValueError(f"line {line_number}: unknown cast cannot have performer IDs")
        return "unknown", []
    if not names:
        raise ValueError(f"line {line_number}: complete/partial labels need performer names")
    unknown = [performer_id for performer_id in ids if performer_id and performer_id not in valid_ids]
    if unknown:
        raise ValueError(f"line {line_number}: a performer ID is missing from the supplied catalogue")
    # An actor missing from the StashDB snapshot remains part of
    # ground truth. The row-local ID exposes that catalogue gap in
    # recall/full-cast metrics instead of silently dropping them.
    truth_ids = [performer_id or f"unmapped:{row['review_id']}:{index}"
                 for index, performer_id in enumerate(ids, 1)]
    return coverage, truth_ids


def _benchmark_row(row: dict, video_id: str, truth_status: str, truth_ids: list[str]) -> dict:
    source_text = _unescape_sheet(row.get("source_text") or "")
    filename = _unescape_sheet(row.get("source_filename") or "")
    filename_title = _filename_to_title(filename)
    fallback = filename_title or f"Telegram {video_id}"
    title = parse_caption_title(_filter_caption_for_title(source_text), fallback)
    return {
        "video_id": video_id,
        "split": str(row.get("split") or "dev").strip(),
        "source_text": source_text,
        "filename_title": filename_title,
        "title": title,
        "truth_ids": truth_ids,
        "truth_status": truth_status,
    }


def _read_labelled_rows(reader, valid_ids: set[str]) -> tuple[list[dict], int, int]:
    rows: list[dict] = []
    seen: set[str] = set()
    skipped_unlabeled = 0
    low_confidence = 0
    for line_number, row in enumerate(reader, 2):
        video_id = _read_video_id(row, line_number, seen)
        coverage = str(row.get("cast_coverage") or "").strip().casefold()
        confidence = str(row.get("label_confidence") or "").strip().casefold()
        if not coverage and not confidence:
            skipped_unlabeled += 1
            continue
        if coverage not in VALID_COVERAGE or confidence not in VALID_CONFIDENCE:
            raise ValueError(f"line {line_number} has invalid cast_coverage or label_confidence")
        names, ids = _aligned_names_and_ids(row, line_number)
        if confidence == "low":
            low_confidence += 1
        truth_status, truth_ids = _truth_for_label(
            row, line_number, names, ids, coverage, confidence, valid_ids)
        rows.append(_benchmark_row(row, video_id, truth_status, truth_ids))
    return rows, skipped_unlabeled, low_confidence


def _build_summary(rows: list[dict], skipped_unlabeled: int, low_confidence: int, catalog: list) -> dict:
    splits = {row["split"] for row in rows}
    if not rows:
        raise ValueError("no reviewed rows with usable labels were found")
    if not splits <= {"dev", "holdout"}:
        raise ValueError("split values must be dev or holdout")
    return {
        "usable_rows": len(rows),
        "skipped_unlabeled_rows": skipped_unlabeled,
        "low_confidence_rows_not_scored": low_confidence,
        "catalog_performers": len(catalog),
        "by_split": {name: sum(row["split"] == name for row in rows) for name in sorted(splits)},
        "by_truth_status": {
            status: sum(row["truth_status"] == status for row in rows)
            for status in sorted(VALID_COVERAGE)
        },
    }


def prepare(sheet: Path, catalog_path: Path) -> tuple[list[dict], dict]:
    catalog, valid_ids = _load_catalog(catalog_path)
    with sheet.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        _check_header(reader.fieldnames)
        rows, skipped_unlabeled, low_confidence = _read_labelled_rows(reader, valid_ids)
    summary = _build_summary(rows, skipped_unlabeled, low_confidence, catalog)
    return rows, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worksheet", type=Path)
    parser.add_argument("catalog", type=Path, help="External StashDB-only catalogue snapshot JSON")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        rows, summary = prepare(args.worksheet, args.catalog)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    except FileExistsError:
        print("Output already exists; choose a new path.", file=sys.stderr)
        return 1
    except (OSError, ValueError, json.JSONDecodeError, csv.Error) as exc:
        print(f"Could not prepare benchmark corpus: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({**summary, "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
