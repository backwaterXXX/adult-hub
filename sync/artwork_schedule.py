"""Select and queue a bounded daily batch of items needing generated artwork."""

from __future__ import annotations

import stat
from pathlib import Path

from common.image_utils import compute_dhash, is_black_image, is_bumper_hash

MAX_ARTWORK_GENERATION_ITEMS = 100
ARTWORK_GENERATION_OFFSET_SECONDS = 60


def artwork_needs_generation(path: Path, *, bumper_hashes=None) -> bool:
    """Return whether artwork is absent, black, or matches a known studio bumper."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return False
    if info.st_size <= 0 or is_black_image(path):
        return True

    if bumper_hashes is None:
        try:
            from .thumbnails import known_bumper_hashes
        except ImportError:
            from thumbnails import known_bumper_hashes
        bumper_hashes = known_bumper_hashes()
    if not bumper_hashes:
        return False
    try:
        return is_bumper_hash(compute_dhash(path), bumper_hashes, max_distance=4)
    except Exception:
        return False


def build_artwork_generation_jobs(conn, media_dir: Path, *, limit: int = MAX_ARTWORK_GENERATION_ITEMS,
                                  bumper_hashes=None) -> list[tuple[str, dict]]:
    """Build at most 100 jobs, prioritizing recently seen videos."""
    if type(limit) is not int or limit < 1:
        raise ValueError("Artwork generation batch size must be a positive integer.")
    limit = min(limit, MAX_ARTWORK_GENERATION_ITEMS)
    media_dir = Path(media_dir)
    if bumper_hashes is None:
        try:
            from .thumbnails import known_bumper_hashes
        except ImportError:
            from thumbnails import known_bumper_hashes
        bumper_hashes = known_bumper_hashes()

    rows = conn.execute(
        "SELECT v.id,v.duration,sf.file_path FROM videos v "
        "JOIN synced_files sf ON sf.video_id=v.id "
        "ORDER BY v.first_seen DESC NULLS LAST,v.last_seen DESC NULLS LAST,sf.file_path"
    ).fetchall()
    try:
        from .media_files import resolve_media_path
    except ImportError:
        from media_files import resolve_media_path

    jobs: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for video_id, _duration, stored_path in rows:
        video_id = str(video_id or "")
        if not video_id or video_id in seen or not isinstance(stored_path, str) or not stored_path:
            continue
        seen.add(video_id)
        try:
            stream_path = resolve_media_path(stored_path, media_dir)
        except (OSError, ValueError, TypeError):
            continue
        if stream_path.is_symlink() or not stream_path.is_file():
            continue
        artwork_path = stream_path.with_suffix(".jpg")
        if not artwork_needs_generation(artwork_path, bumper_hashes=bumper_hashes):
            continue
        jobs.append(("artwork_generate", {
            "video_id": video_id,
            "offset_seconds": ARTWORK_GENERATION_OFFSET_SECONDS,
            "only_if_artwork_invalid": True,
            "file_path": stored_path,
        }))
        if len(jobs) >= limit:
            break
    return jobs


def queue_artwork_generation_batch(db_url: str | None, media_dir: Path, *, limit: int = MAX_ARTWORK_GENERATION_ITEMS) -> dict:
    """Enqueue the next bounded batch of artwork generation jobs."""
    from common import db
    from common.job_queue import enqueue_jobs_batch

    conn = db.connect(db_url)
    try:
        jobs = build_artwork_generation_jobs(conn, media_dir, limit=limit)
    finally:
        conn.close()
    job_ids = enqueue_jobs_batch(db_url, jobs)
    return {"candidates": len(jobs), "job_ids": job_ids}
