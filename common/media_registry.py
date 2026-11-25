"""Media sidecars that are registered outside ``synced_files``.

The library tree holds two kinds of files: streams tracked in ``synced_files``
and performer artwork tracked in ``performer_photos.thumb``. Anything that reads
the media directory has to consult both registries, or every performer portrait
looks like an orphan.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from common import db


def performer_artwork_paths(conn) -> set[str]:
    """Return media-relative performer artwork paths recorded in ``performer_photos``.

    Paths are normalized to POSIX relative form, the same shape the library walk
    produces. Absolute paths and parent traversal are ignored so a malformed row
    can never authorize a file outside the media root.
    """
    try:
        if "performer_photos" not in db.table_names(conn):
            return set()
        rows = conn.execute(
            "SELECT DISTINCT thumb FROM performer_photos WHERE thumb IS NOT NULL"
        ).fetchall()
    except Exception:
        return set()
    paths: set[str] = set()
    for row in rows:
        raw = row[0] if row else None
        if not isinstance(raw, str):
            continue
        relative = raw.strip().replace("\\", "/")
        if not relative or relative.startswith("/"):
            continue
        parts = PurePosixPath(relative).parts
        if not parts or ".." in parts:
            continue
        paths.add(PurePosixPath(*parts).as_posix())
    return paths
