#!/usr/bin/env python3
"""Report PostgreSQL database size and the largest tables."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from common.db_usage import read_db_usage


def _size(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TB"


def _db_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        return url.replace("@postgres:5432", "@127.0.0.1:5432")
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().replace("@postgres:5432", "@127.0.0.1:5432")
    raise SystemExit("DATABASE_URL is not set")


def print_report(report: dict) -> None:
    if not report.get("available"):
        print("Database usage is unavailable.")
        return
    print(f"Database: {_size(report['database_bytes'])}")
    print(f"{'table':40} {'total':>10} {'table':>10} {'index':>10} {'toast':>10} {'rows':>10}")
    for table in report["tables"]:
        print(f"{table['name'][:40]:40} {_size(table['total_bytes']):>10} "
              f"{_size(table['table_bytes']):>10} {_size(table['index_bytes']):>10} "
              f"{_size(table['toast_bytes']):>10} {table['live_rows']:>10}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = read_db_usage(_db_url(), limit=args.limit)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0 if report.get("available") else 1


if __name__ == "__main__":
    raise SystemExit(main())
