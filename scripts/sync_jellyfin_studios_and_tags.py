#!/usr/bin/env python3
"""Retired direct-SQL Jellyfin maintenance command.

Jellyfin's private SQLite database is not an integration API. Studio, tag,
and tagline metadata is read from the library's NFO files by Jellyfin; trigger
a library refresh through Jellyfin when those sidecars have changed.
"""

if __name__ == "__main__":
    raise SystemExit(
        "This direct SQLite writer has been retired. Use Jellyfin's library refresh "
        "API after updating NFO metadata."
    )
