#!/usr/bin/env python3
"""Compare Telegram performer candidate methods on an independently labeled JSONL set.

The benchmark deliberately ignores any existing/current cast mapping. Each input
row supplies independent ground-truth IDs. The StashDB catalogue can be provided
once with ``--catalog`` instead of repeated in every row. Only candidate IDs
emitted by methods below are scored.

JSONL row format (all IDs are strings)::

    {
      "video_id": "telegram:...",
      "split": "dev" | "holdout",
      "source_text": "original Telegram caption",
      "filename_title": "...",
      "title": "...",
      "truth_ids": ["stashdb:..."],
      "truth_status": "complete" | "partial" | "unknown",
      "scene_candidates": [{"source": "theporndb", "performer_ids": ["..."]}],
      "oshash_candidates": [{"source": "stashdb", "performer_ids": ["..."]}],
      "phash_candidates": [{"source": "theporndb", "performer_ids": ["..."]}]
    }

The three candidate arrays are optional and should contain cached, versioned
results from separately executed provider lookups. They are not fetched here.
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.performer_catalog import match_catalog, normalize_name  # noqa: E402
from common.performer_titles import parse_title_performers_with_known_map  # noqa: E402


def _ids(values) -> set[str]:
    if not isinstance(values, list):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def _catalog_names(catalog: list[dict]) -> tuple[dict[str, str], dict[str, set[str]]]:
    names: dict[str, str] = {}
    ids_by_name: dict[str, set[str]] = defaultdict(set)
    for row in catalog:
        if not isinstance(row, dict):
            continue
        person_id = str(row.get("id") or "").strip()
        display = str(row.get("name") or "").strip()
        if not person_id or not display:
            continue
        names.setdefault(display.casefold(), display)
        for value in [display, *(row.get("aliases") or [])]:
            key = normalize_name(str(value or ""))
            if key:
                ids_by_name[key].add(person_id)
    return names, ids_by_name


def _resolve_names(values: list[str], ids_by_name: dict[str, set[str]]) -> set[str]:
    result: set[str] = set()
    for value in values:
        found = ids_by_name.get(normalize_name(value), set())
        # Ambiguous same-name aliases do not count as an identification.
        if len(found) == 1:
            result.update(found)
    return result


def _exact_catalog(row: dict) -> set[str]:
    records, _proposals = match_catalog(
        [
            ("caption", str(row.get("source_text") or "")),
            ("filename", str(row.get("filename_title") or "")),
            ("title", str(row.get("title") or "")),
        ],
        row.get("catalog") or [],
    )
    return {str(record["id"]) for record in records if record.get("id")}


def _title_parser(row: dict) -> set[str]:
    names, ids_by_name = _catalog_names(row.get("catalog") or [])
    alias_targets: dict[str, set[str]] = defaultdict(set)
    for record in row.get("catalog") or []:
        if not isinstance(record, dict):
            continue
        display = str(record.get("name") or "").strip()
        if not display:
            continue
        for alias in [display, *(record.get("aliases") or [])]:
            alias_targets[str(alias).casefold()].add(display)
    # Avoid handing homonymous aliases to the parser as if they were unique.
    known = {alias: next(iter(targets)) for alias, targets in alias_targets.items()
             if len(targets) == 1}
    parsed = parse_title_performers_with_known_map(str(row.get("title") or ""), known)
    return _resolve_names(parsed, ids_by_name)


def _cached_candidates(row: dict, field: str) -> set[str]:
    result: set[str] = set()
    values = row.get(field) or []
    if not isinstance(values, list):
        return result
    for candidate in values:
        if isinstance(candidate, dict):
            result.update(_ids(candidate.get("performer_ids")))
    return result


def _measure(rows: list[dict], methods: dict[str, Callable[[dict], set[str]]], split: str) -> dict:
    results = {}
    selected = [row for row in rows if str(row.get("split") or "dev") == split]
    for method_name, method in methods.items():
        recall_tp = recall_fn = precision_tp = precision_fp = 0
        full_correct = full_total = covered = scored = 0
        durations: list[float] = []
        per_status: dict[str, dict[str, int]] = defaultdict(lambda: {"rows": 0, "covered": 0})
        for row in selected:
            started = time.perf_counter()
            predicted = method(row)
            durations.append(time.perf_counter() - started)
            status = str(row.get("truth_status") or "unknown")
            per_status[status]["rows"] += 1
            if predicted:
                covered += 1
                per_status[status]["covered"] += 1
            if status not in {"complete", "partial"}:
                continue
            truth = _ids(row.get("truth_ids"))
            if truth:
                scored += 1
                recall_tp += len(predicted & truth)
                recall_fn += len(truth - predicted)
            # A partial label lists known people, not all negative identities.
            # Only complete casts can support a precision denominator.
            if status == "complete":
                precision_tp += len(predicted & truth)
                precision_fp += len(predicted - truth)
            if status == "complete":
                full_total += 1
                full_correct += int(predicted == truth)
        results[method_name] = {
            "rows": len(selected),
            "rows_with_candidates": covered,
            "candidate_coverage": round(covered / len(selected), 4) if selected else None,
            "labeled_rows_scored": scored,
            "person_precision_on_complete_casts": (
                round(precision_tp / (precision_tp + precision_fp), 4)
                if precision_tp + precision_fp else None
            ),
            "known_person_recall": round(recall_tp / (recall_tp + recall_fn), 4)
            if recall_tp + recall_fn else None,
            "full_cast_exact_accuracy": round(full_correct / full_total, 4) if full_total else None,
            "complete_cast_rows": full_total,
            "mean_seconds_per_row": round(statistics.fmean(durations), 6) if durations else None,
            "p95_seconds_per_row": round(sorted(durations)[min(len(durations) - 1, int(len(durations) * .95))], 6)
            if durations else None,
            "truth_status_rows": dict(per_status),
        }
    return results


def _load_global_catalog(catalog_path: Path | None) -> list | None:
    if not catalog_path:
        return None
    raw_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    global_catalog = raw_catalog.get("performers") if isinstance(raw_catalog, dict) else raw_catalog
    if not isinstance(global_catalog, list) or not global_catalog:
        raise ValueError("catalog snapshot must contain a nonempty performers array")
    if any(not isinstance(item, dict) or not str(item.get("id") or "").startswith("stashdb:")
           for item in global_catalog):
        raise ValueError("catalog snapshot may contain only external stashdb identities")
    return global_catalog


def _parse_corpus_line(line: str, line_number: int, global_catalog: list | None) -> dict:
    try:
        row = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON on line {line_number}") from exc
    if not isinstance(row, dict):
        raise ValueError(f"Line {line_number} must contain a JSON object")
    if not row.get("video_id"):
        raise ValueError(f"Line {line_number} needs video_id")
    if "catalog" not in row and global_catalog is not None:
        row["catalog"] = global_catalog
    if not isinstance(row.get("catalog"), list):
        raise ValueError(f"Line {line_number} needs a catalog or --catalog snapshot")
    return row


def _load_corpus_rows(path: Path, global_catalog: list | None) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            rows.append(_parse_corpus_line(line, line_number, global_catalog))
    return rows


def _build_methods() -> dict[str, Callable[[dict], set[str]]]:
    return {
        "exact_catalog_text": _exact_catalog,
        "title_regex_parser": _title_parser,
        "scene_catalog_candidates": lambda row: _cached_candidates(row, "scene_candidates"),
        "oshash_catalog_candidates": lambda row: _cached_candidates(row, "oshash_candidates"),
        "phash_catalog_candidates": lambda row: _cached_candidates(row, "phash_candidates"),
    }


def _select_splits(rows: list[dict], split: str) -> list[str]:
    available_splits = sorted({str(row.get("split") or "dev") for row in rows})
    if split != "all" and split not in available_splits:
        raise ValueError(f"requested split {split!r} is missing from the corpus")
    return available_splits if split == "all" else [split]


def benchmark(path: Path, *, catalog_path: Path | None = None, split: str = "dev") -> dict:
    global_catalog = _load_global_catalog(catalog_path)
    rows = _load_corpus_rows(path, global_catalog)
    methods = _build_methods()
    splits = _select_splits(rows, split)
    report = {
        "input": str(path),
        "rows": len(rows),
        "evaluated_split": split,
        "splits": {
            split: _measure(rows, methods, split)
            for split in splits
        },
        "catalog_snapshot": str(catalog_path) if catalog_path else "embedded_per_row",
        "catalog_performers": len(global_catalog) if global_catalog is not None else None,
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "limits": [
            "Only independent truth_ids with truth_status complete/partial are scored.",
            "unknown rows are counted for candidate coverage, never as negative labels.",
            "Current video cast is ignored by every method.",
            "Candidate arrays must be generated and versioned outside this benchmark.",
            "Aggregate metrics describe this corpus; stratified sampling and blind review are required for population claims.",
        ],
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path, help="Independent, locally stored JSONL corpus")
    parser.add_argument("--catalog", type=Path, help="StashDB-only catalogue snapshot JSON")
    parser.add_argument("--output", type=Path, help="Write JSON report here (default: stdout)")
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="dev",
                        help="Evaluate dev by default; use holdout only after freezing configuration")
    args = parser.parse_args()
    try:
        report = benchmark(args.corpus, catalog_path=args.catalog, split=args.split)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
