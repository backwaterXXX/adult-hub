#!/usr/bin/env python3
"""Export only the external StashDB catalogue for offline evaluation.

Legacy performer bios, video casts, local people, and human-created aliases are
excluded. The result is a local snapshot; keep it with other private review
artifacts rather than committing it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.db import connect  # noqa: E402


def export_catalog(db_url: str | None) -> dict:
    conn = connect(db_url)
    try:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout='30s'")
        people = conn.execute(
            """SELECT id,source_id,name FROM performer_catalog
                WHERE active=TRUE AND source='stashdb' ORDER BY source_id"""
        ).fetchall()
        aliases = conn.execute(
            """SELECT a.performer_id,a.alias
                 FROM performer_catalog_aliases a
                 JOIN performer_catalog p ON p.id=a.performer_id
                WHERE p.active=TRUE AND p.source='stashdb'
                  AND a.origin='stashdb' AND a.approved=TRUE
                ORDER BY a.performer_id,length(a.alias) DESC,a.alias"""
        ).fetchall()
        conn.rollback()
    finally:
        conn.close()

    alias_map: dict[str, list[str]] = {}
    for performer_id, alias in aliases:
        alias_map.setdefault(str(performer_id), []).append(str(alias))
    records = []
    for performer_id, source_id, name in people:
        pid = str(performer_id)
        records.append({
            "id": pid,
            "source": "stashdb",
            "source_id": str(source_id),
            "name": str(name),
            "aliases": list(dict.fromkeys(alias_map.get(pid, []))),
        })
    return {"source": "stashdb", "performers": records}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=os.getenv("DATABASE_URL") or None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = export_catalog(args.db_url)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
    except FileExistsError:
        print("Output already exists; choose a new path.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Could not export external catalogue: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps({"source": result["source"], "performers": len(result["performers"]),
                      "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
