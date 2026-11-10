"""Helpers shared by the one-off performer and NFO cleanup scripts under ``scripts/``.

Script-level settings (``BASE_DIR``, ``JF_PEOPLE_DIR``, the ``sync`` module, the
backup directory) are passed in as arguments. The scripts keep their own names
for these helpers as thin wrappers, so module globals are read at call time.
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Iterator

from common import db
from common.json_helpers import json_set_or_empty


def docker_pg_dump_backup(backup_dir: Path, prefix: str) -> Path:
    """Dump the database with ``docker exec postgres pg_dump`` into a gzip file."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_file = backup_dir / f"{prefix}-{stamp}.sql.gz"
    res = subprocess.run(
        ["docker", "exec", "postgres", "pg_dump", "-U", "adulthub", "adulthub"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"docker pg_dump failed: {res.stderr.decode()[:300]}")
    with gzip.open(backup_file, "wb") as f:
        f.write(res.stdout)
    return backup_file


def create_db_backup(db_url: str | None, backup_dir: Path, prefix: str) -> Path:
    """Back up with ``db.pg_backup``; fall back to the docker pg_dump route on RuntimeError."""
    try:
        return db.pg_backup(db_url, backup_dir, prefix=prefix)
    except RuntimeError:
        return docker_pg_dump_backup(backup_dir, prefix)


def remove_person_dir_via_docker(
    person_dir: Path, error: Exception, *, base_dir: Path, people_dir: Path, report=print
) -> int:
    """Remove a Jellyfin People folder through the jellyfin container. Return 0 or 1 for the count."""
    removed = 0
    try:
        rel = person_dir.relative_to(people_dir)
        res = subprocess.run(
            ["docker", "exec", "jellyfin", "rm", "-rf", f"/config/metadata/People/{rel}"],
            capture_output=True,
        )
        if res.returncode == 0:
            removed += 1
            report(
                f"  Removed Jellyfin People folder (via docker): {person_dir.relative_to(base_dir)}"
            )
            return removed
        report(f"  Error removing {person_dir}: {error}")
        return removed
    except Exception:
        report(f"  Error removing {person_dir}: {error}")
        return removed


def remove_person_dir_counted(
    person_dir: Path, *, base_dir: Path, people_dir: Path, report=print
) -> int:
    """Remove one Jellyfin People folder and return how many removals to count (0 or 1)."""
    removed = 0
    try:
        shutil.rmtree(person_dir)
        removed += 1
        report(f"  Removed Jellyfin People folder: {person_dir.relative_to(base_dir)}")
        return removed
    except Exception as e:
        return removed + remove_person_dir_via_docker(
            person_dir, e, base_dir=base_dir, people_dir=people_dir, report=report
        )


def iter_jellyfin_person_dirs(people_dir: Path) -> Iterator[Path]:
    """Yield each person folder under ``people_dir/<letter>/``. A missing root yields nothing."""
    if not people_dir.exists():
        return
    for letter_dir in people_dir.iterdir():
        if not letter_dir.is_dir():
            continue
        for person_dir in list(letter_dir.iterdir()):
            if person_dir.is_dir():
                yield person_dir


def build_video_nfo(conn, row, new_perfs: list, *, sync_api) -> tuple[dict, set, set]:
    """Build the NFO video dict, genre set and tag set for one ``videos`` row.

    ``row`` follows this column order: id, provider, title, slug, duration,
    thumbnail, performers, categories, tags, studio, first_seen. The performers
    column is ignored because ``new_perfs`` replaces it. Building happens before
    any file write so the caller can keep its own error handling around the write.
    """
    (
        vid_id,
        prov,
        title,
        slug,
        dur,
        thumb,
        _perfs_raw,
        cats_raw,
        tags_raw,
        studio,
        first_seen,
    ) = row
    tag_set = json_set_or_empty(tags_raw)
    genres = json_set_or_empty(cats_raw)

    video_dict = {
        "id": vid_id,
        "provider": prov,
        "provider_video_id": vid_id.split(":", 1)[-1],
        "title": title or vid_id,
        "slug": slug or "",
        "duration": dur or 0,
        "thumbnail": thumb or "",
        "performers": new_perfs,
        "performer_thumbs": sync_api.load_performer_thumbs(conn, new_perfs),
        "studio": studio or "",
        "dateadded": sync_api.format_dateadded(first_seen or time.time()),
    }
    return video_dict, genres, tag_set


def regenerate_telegram_nfo(
    conn, video_id: str, performers: list[str], *, media_dir: Path, sync_api
) -> bool:
    """Rewrite one video's NFO for ``performers``. Return False when the row or NFO file is missing."""
    row = conn.execute(
        """
        SELECT provider,title,slug,duration,thumbnail,categories,tags,studio,first_seen,source_text
        FROM videos WHERE id=%s
    """,
        (video_id,),
    ).fetchone()
    if not row:
        return False
    (
        provider,
        title,
        slug,
        duration,
        thumbnail,
        categories,
        tags,
        studio,
        first_seen,
        source_text,
    ) = row
    categories = categories if isinstance(categories, list) else json.loads(categories or "[]")
    tags = tags if isinstance(tags, list) else json.loads(tags or "[]")
    path = media_dir / f"{sync_api.stable_media_basename(video_id)}.nfo"
    if not path.exists():
        return False
    video = {
        "id": video_id,
        "provider": provider,
        "provider_video_id": video_id.split(":", 1)[-1],
        "title": title or video_id,
        "slug": slug or "",
        "duration": duration or 0,
        "thumbnail": thumbnail or "",
        "performers": performers,
        "plot": source_text or "",
        "performer_thumbs": sync_api.load_performer_thumbs(conn, performers),
        "studio": studio or "",
        "dateadded": sync_api.format_dateadded(first_seen or 0),
    }
    sync_api.create_nfo_file(video, set(categories), set(tags), path, conn=conn)
    return True
