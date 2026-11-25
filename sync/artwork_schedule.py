"""Select and queue a bounded daily batch of items needing generated artwork."""

from __future__ import annotations

import stat
from pathlib import Path
import logging

from common.artwork_parking import ARTWORK_FRAME_FAILURE_CODE, ARTWORK_FRAME_FAILURE_LIMIT
from common.image_utils import compute_dhash, is_black_image, is_bumper_hash

logger = logging.getLogger(__name__)


# The schedule wakes every four hours, so this cap sets the real pace of the
# artwork backlog. A measured 200-job run needs about ten minutes of the single
# worker, so 800 still finishes well inside the window while doubling the drain.
MAX_ARTWORK_GENERATION_ITEMS = 800
# Half of every batch is reserved for the oldest gaps. The missing files are a
# legacy cohort (1 456 of 1 472 date from September): a newest-first batch is
# consumed by fresh arrivals and replacements, so those items never got a slot.
ARTWORK_BACKLOG_SHARE_NUMERATOR = 1
ARTWORK_BACKLOG_SHARE_DENOMINATOR = 2
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
        from sync.thumbnails import known_bumper_hashes
        bumper_hashes = known_bumper_hashes()
    if not bumper_hashes:
        return False
    try:
        return is_bumper_hash(compute_dhash(path), bumper_hashes, max_distance=4)
    except Exception:
        logger.debug("Ignored exception in artwork_needs_generation", exc_info=True)
        return False


def _candidate_rows(conn, *, oldest_first: bool) -> list:
    """Return playable, unfiltered candidate rows in the requested age order."""
    order = "v.first_seen ASC NULLS LAST" if oldest_first else "v.first_seen DESC NULLS LAST"
    return conn.execute(
        "SELECT v.id,v.duration,sf.file_path FROM videos v "
        "JOIN synced_files sf ON sf.video_id=v.id "
        f"WHERE COALESCE(v.playable, 1) != 0{_dead_stream_exclusion(conn)}"
        f"{_parked_artwork_exclusion(conn)} "
        f"ORDER BY {order},v.last_seen DESC NULLS LAST,sf.file_path"
    ).fetchall()


def build_artwork_generation_jobs(conn, media_dir: Path, *, limit: int = MAX_ARTWORK_GENERATION_ITEMS,
                                  bumper_hashes=None) -> list[tuple[str, dict]]:
    """Build at most ``limit`` jobs, draining the oldest gaps and covering new items.

    Selection runs in two passes: the first reserves a share of the batch for the
    oldest entries (the legacy backlog), the second fills the rest newest first so
    fresh arrivals still get artwork in the same run.
    """
    if type(limit) is not int or limit < 1:
        raise ValueError("Artwork generation batch size must be a positive integer.")
    limit = min(limit, MAX_ARTWORK_GENERATION_ITEMS)
    media_dir = Path(media_dir)
    if bumper_hashes is None:
        bumper_hashes = _default_bumper_hashes()
    from sync.media_files import resolve_media_path

    backlog_budget = (limit * ARTWORK_BACKLOG_SHARE_NUMERATOR) // ARTWORK_BACKLOG_SHARE_DENOMINATOR
    jobs: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for oldest_first, budget in ((True, backlog_budget), (False, limit)):
        if len(jobs) >= limit:
            break
        target = min(budget, limit)
        for video_id, _duration, stored_path in _candidate_rows(conn, oldest_first=oldest_first):
            if len(jobs) >= target:
                break
            video_id = str(video_id or "")
            if not video_id or video_id in seen or not isinstance(stored_path, str) or not stored_path:
                continue
            seen.add(video_id)
            if not _artwork_generation_needed(stored_path, media_dir, resolve_media_path, bumper_hashes):
                continue
            jobs.append(("artwork_generate", {
                "video_id": video_id,
                "offset_seconds": ARTWORK_GENERATION_OFFSET_SECONDS,
                "only_if_artwork_invalid": True,
                "file_path": stored_path,
            }))
    return jobs


def _dead_stream_exclusion(conn) -> str:
    """Return the dead-stream filter for this schema, or nothing when absent.

    Denylisted corpses never stream again, so generating artwork for them only
    burns Telegram bandwidth and adds failure noise to the job list.
    """
    from common import db

    try:
        tables = db.table_names(conn)
    except Exception:
        return ""
    if "dead_streams" not in tables:
        return ""
    return " AND NOT EXISTS (SELECT 1 FROM dead_streams ds WHERE ds.video_id = v.id)"


def _parked_artwork_exclusion(conn) -> str:
    """Return the filter that skips streams whose frame extraction keeps failing.

    ffmpeg cannot decode those files, so re-queuing them every four hours only
    burns slots of the bounded batch. They stay visible as missing artwork in the
    audit, and the one-month job-history retention retries them eventually; an
    operator can still queue one video by hand.
    """
    from common import db

    try:
        tables = db.table_names(conn)
    except Exception:
        return ""
    if "video_duplicate_jobs" not in tables:
        return ""
    pattern = f'%"{ARTWORK_FRAME_FAILURE_CODE}"%'
    return (
        " AND NOT EXISTS (SELECT 1 FROM ("
        "SELECT j.payload::jsonb ->> 'video_id' AS parked_id "
        "FROM video_duplicate_jobs j "
        "WHERE j.action = 'artwork_generate' AND j.state = 'failed' "
        f"AND j.result LIKE '{pattern}' "
        "GROUP BY 1 HAVING COUNT(*) >= "
        f"{ARTWORK_FRAME_FAILURE_LIMIT}"
        ") parked WHERE parked.parked_id = v.id)"
    )


def _default_bumper_hashes():
    from sync.thumbnails import known_bumper_hashes
    return known_bumper_hashes()


def _artwork_generation_needed(stored_path: str, media_dir: Path, resolve_media_path, bumper_hashes) -> bool:
    """Return True when a stored path is a regular media file that lacks valid artwork."""
    try:
        stream_path = resolve_media_path(stored_path, media_dir)
    except (OSError, ValueError, TypeError):
        return False
    if stream_path.is_symlink() or not stream_path.is_file():
        return False
    return artwork_needs_generation(stream_path.with_suffix(".jpg"), bumper_hashes=bumper_hashes)


def resolve_stale_artwork_findings(conn, media_dir: Path, *, bumper_hashes=None) -> int:
    """Resolve open ``missing_artwork`` findings for items that have artwork now.

    A library audit records what is missing at the moment it runs, so an item that
    receives its artwork afterwards keeps an open finding and the quality centre
    keeps reporting work that is already done. The four-hourly artwork schedule
    re-checks those rows with the same predicate the batch uses, so the reported
    backlog follows the library instead of the audit history.
    """
    from sync.media_files import resolve_media_path

    media_dir = Path(media_dir)
    rows = conn.execute(
        "SELECT DISTINCT a.video_id,sf.file_path FROM admin_audit_items a "
        "JOIN synced_files sf ON sf.video_id=a.video_id "
        "WHERE a.kind='missing_artwork' AND a.status='open'"
    ).fetchall()
    if not rows:
        return 0
    if bumper_hashes is None:
        bumper_hashes = _default_bumper_hashes()

    resolved: list[str] = []
    for video_id, stored_path in rows:
        if not video_id or not isinstance(stored_path, str) or not stored_path:
            continue
        try:
            stream_path = resolve_media_path(stored_path, media_dir)
        except (OSError, ValueError, TypeError):
            continue
        if stream_path.is_symlink() or not stream_path.is_file():
            # A vanished stream belongs to the missing_stream finding; leave it.
            continue
        if artwork_needs_generation(stream_path.with_suffix(".jpg"), bumper_hashes=bumper_hashes):
            continue
        resolved.append(str(video_id))
    if not resolved:
        return 0
    with conn:
        conn.execute(
            "UPDATE admin_audit_items SET status='resolved' "
            "WHERE kind='missing_artwork' AND status='open' AND video_id=ANY(%s)",
            (resolved,),
        )
    return len(resolved)


def queue_artwork_generation_batch(db_url: str | None, media_dir: Path, *, limit: int = MAX_ARTWORK_GENERATION_ITEMS) -> dict:
    """Enqueue the next bounded batch of artwork generation jobs."""
    from common import db
    from common.job_queue import enqueue_jobs_batch

    conn = db.connect(db_url)
    try:
        resolved = resolve_stale_artwork_findings(conn, media_dir)
        jobs = build_artwork_generation_jobs(conn, media_dir, limit=limit)
    finally:
        conn.close()
    job_ids = enqueue_jobs_batch(db_url, jobs)
    return {"candidates": len(jobs), "job_ids": job_ids,
            "artwork_findings_resolved": resolved}
