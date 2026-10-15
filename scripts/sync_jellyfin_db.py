#!/usr/bin/env python3
"""Retired direct-SQL Jellyfin maintenance command.

Jellyfin's private SQLite database is not an integration API. Use the regular
sync service, which updates display titles through Jellyfin's HTTP API.
Use Jellyfin's NFO importer for sidecar metadata and the regular sync service
for API-backed display-title reconciliation.
"""

if __name__ == "__main__":
    raise SystemExit(
        "This direct SQLite writer has been retired. Run the regular sync service "
        "to reconcile Jellyfin metadata through its API."
    )
