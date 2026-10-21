"""Read-only latency and query-plan benchmark for Admin catalog text search.

Run inside the sync image with representative search terms, for example:
    python tools/benchmark_catalog_search.py --term telegram --term performer

Only aggregate timings and plan nodes are printed; search terms and catalog rows
are not included in output. Each query runs in a read-only transaction and has a
short statement timeout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from typing import Any

import psycopg


SEARCH_FIELDS = ("title", "performers", "studio", "plot", "tags")


def _effective_value(field: str, columns: set[str], has_overrides: bool) -> str:
    source = f"v.{field}" if field in columns else "NULL"
    if has_overrides and field in {"title", "plot"}:
        return (
            f"CASE WHEN COALESCE(mo.fields, '{{}}'::jsonb) ? '{field}' "
            f"THEN mo.fields->>'{field}' ELSE {source}::text END"
        )
    return f"{source}::text"


def _search_query(conn: psycopg.Connection) -> tuple[str, int]:
    tables = {row[0] for row in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    ).fetchall()}
    if "videos" not in tables:
        raise RuntimeError("The videos catalog table is unavailable in the current schema.")
    columns = {row[0] for row in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'videos'"
    ).fetchall()}
    has_overrides = "video_metadata_overrides" in tables
    searchable = ["COALESCE(v.id, '')"]
    searchable.extend(
        f"COALESCE({_effective_value(name, columns, has_overrides)}, '')"
        for name in SEARCH_FIELDS if name in columns
    )
    predicates = " OR ".join(f"{expr} ILIKE %s" for expr in searchable)
    override_join = (
        " LEFT JOIN video_metadata_overrides mo ON mo.video_id = v.id"
        if has_overrides else ""
    )
    return f"SELECT count(*) FROM videos v{override_join} WHERE ({predicates})", len(searchable)


def _plan_nodes(plan: dict[str, Any]) -> list[dict[str, Any]]:
    keys = (
        "Node Type", "Relation Name", "Index Name", "Plan Rows", "Actual Rows",
        "Actual Loops", "Shared Hit Blocks", "Shared Read Blocks",
    )
    result = [{key: plan[key] for key in keys if key in plan}]
    for child in plan.get("Plans", []):
        result.extend(_plan_nodes(child))
    return result


def _percentile_95(values: list[float]) -> float:
    return sorted(values)[max(0, math.ceil(0.95 * len(values)) - 1)]


def benchmark(terms: list[str], runs: int, timeout_ms: int) -> dict[str, Any]:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL must be configured in the sync environment.")
    if not 1 <= runs <= 20 or not 500 <= timeout_ms <= 30000:
        raise ValueError("Runs must be 1–20 and timeout must be 500–30000 ms.")

    results: list[dict[str, Any]] = []
    with psycopg.connect(url) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SELECT set_config('statement_timeout', %s, true)", (f"{timeout_ms}ms",))
            query, searchable_field_count = _search_query(conn)
            for term in terms:
                if not term.strip() or len(term) > 200 or "\x00" in term:
                    raise ValueError("Each term must contain 1–200 non-NUL characters.")
                params = tuple(f"%{term.strip()}%" for _ in range(searchable_field_count))
                timings: list[float] = []
                last_plan: dict[str, Any] | None = None
                for _ in range(runs):
                    raw = conn.execute(
                        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + query, params
                    ).fetchone()[0]
                    plan = raw[0]
                    last_plan = plan
                    timings.append(float(plan["Execution Time"]))
                assert last_plan is not None
                results.append({
                    "term_fingerprint": hashlib.sha256(term.strip().encode()).hexdigest()[:12],
                    "runs": runs,
                    "execution_ms_median": round(statistics.median(timings), 3),
                    "execution_ms_p95": round(_percentile_95(timings), 3),
                    "planning_ms_last": round(float(last_plan["Planning Time"]), 3),
                    "plan_nodes_last": _plan_nodes(last_plan["Plan"]),
                })
    return {"read_only": True, "statement_timeout_ms": timeout_ms, "searches": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--term", action="append", required=True,
                        help="Representative catalog search text; repeat for multiple terms")
    parser.add_argument(
        "--runs", type=int, default=5,
        help="EXPLAIN ANALYZE runs per term (default: 5)",
    )
    parser.add_argument("--statement-timeout-ms", type=int, default=5000,
                        help="Per-query PostgreSQL timeout (default: 5000)")
    parser.add_argument("--output", help="Optional path for the JSON benchmark report")
    args = parser.parse_args()
    try:
        report = benchmark(args.term, args.runs, args.statement_timeout_ms)
    except (OSError, psycopg.Error, RuntimeError, ValueError) as exc:
        print(f"Catalog search benchmark failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(encoded)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
