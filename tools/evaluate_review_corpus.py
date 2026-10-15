#!/usr/bin/env python3
"""Summarize human labels in fuzzy-performer or duplicate-video review CSVs.

Metrics describe only the labeled rows in the supplied stratified worksheet;
they are not automatically representative of the full catalog.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


TRUE_VALUES = {"1", "true", "yes", "y"}
FALSE_VALUES = {"0", "false", "no", "n"}


def _label(value: str | None) -> bool | None:
    normalized = str(value or "").strip().casefold()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return None


def _matrix(rows: list[dict], *, truth_field: str, positive_sources: set[str], negative_sources: set[str]) -> dict:
    counts = Counter(tp=0, fp=0, fn=0, tn=0, unlabeled=0)
    for row in rows:
        source = str(row.get("sample_kind") or row.get("review_source") or "")
        if source in positive_sources:
            predicted = True
        elif source in negative_sources:
            predicted = False
        else:
            continue
        truth = _label(row.get(truth_field))
        if truth is None:
            counts["unlabeled"] += 1
        elif predicted and truth:
            counts["tp"] += 1
        elif predicted and not truth:
            counts["fp"] += 1
        elif not predicted and truth:
            counts["fn"] += 1
        else:
            counts["tn"] += 1

    labeled = counts["tp"] + counts["fp"] + counts["fn"] + counts["tn"]
    precision_denominator = counts["tp"] + counts["fp"]
    recall_denominator = counts["tp"] + counts["fn"]
    counts["labeled"] = labeled
    counts["precision"] = round(counts["tp"] / precision_denominator, 4) if precision_denominator else None
    counts["recall_within_reviewed_strata"] = (
        round(counts["tp"] / recall_denominator, 4) if recall_denominator else None
    )
    return dict(counts)


def evaluate(path: Path, kind: str) -> dict:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fields = set(reader.fieldnames or ())

    truth_field = "same_person" if kind == "fuzzy" else "same_scene"
    if truth_field not in fields:
        raise ValueError(f"CSV is missing the {truth_field!r} label column")

    if kind == "fuzzy":
        confusion = _matrix(
            rows,
            truth_field=truth_field,
            positive_sources={"candidate"},
            negative_sources={"near_miss"},
        )
        return {
            "kind": kind,
            "rows": len(rows),
            "confusion_on_labeled_review_rows": confusion,
            "scope": "Stratified candidate and near-miss sample only; recall is limited to reviewed near-miss strata.",
        }

    sources = {str(row.get("review_source") or "unknown") for row in rows}
    per_source = defaultdict(list)
    for row in rows:
        per_source[str(row.get("review_source") or "unknown")].append(row)

    current = _matrix(
        rows,
        truth_field=truth_field,
        positive_sources={"current_suggestion"},
        negative_sources=set(),
    )
    historical = {}
    for source in sorted(sources - {"current_suggestion"}):
        decision_positive = source == "historical_hide"
        historical[source] = _matrix(
            per_source[source],
            truth_field=truth_field,
            positive_sources={source} if decision_positive else set(),
            negative_sources=set() if decision_positive else {source},
        )
    return {
        "kind": kind,
        "rows": len(rows),
        "current_suggestions": current,
        "historical_decisions": historical,
        "scope": "Current suggestions and historical decisions are reported separately; reviewed sample only.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("fuzzy", "duplicates"), required=True)
    parser.add_argument("csv_path", type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(args.csv_path, args.kind)
    except (OSError, csv.Error, ValueError) as exc:
        print(f"Could not evaluate review corpus: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
