#!/usr/bin/env python3
"""Refresh the local StashDB performer catalogue (requires STASHDB_API_KEY)."""

import os


from common import db
from sync.performer_catalog_sync import sync_catalog


def main() -> int:
    conn = db.connect((os.getenv("DATABASE_URL") or "").strip() or None)
    try:
        people, aliases = sync_catalog(conn)
        print(f"Imported {people} performers and {aliases} names/aliases")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
