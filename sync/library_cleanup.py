"""Guarded cleanup operations for the media catalog."""

from common import db
import time as _time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set
import logging

logger = logging.getLogger(__name__)


# Destructive upkeep removes catalog rows for material that is already gone, so
# a replaced or half-mounted media volume is indistinguishable from a healthy
# source that simply lost most of its files. A pass may therefore delete at most
# CLEANUP_LOSS_FRACTION_LIMIT of a source's entries, but never fewer than
# CLEANUP_LOSS_MIN_ENTRIES: small sources legitimately churn away completely,
# while a real library losing a quarter of its registrations is an environment
# fault that has to stop and wait for the operator.
CLEANUP_LOSS_FRACTION_LIMIT = 0.25
CLEANUP_LOSS_MIN_ENTRIES = 10


def _media_root_is_safe_for_cleanup(media_dir: Path, conn: db.Connection,
                                    logger: Any) -> bool:
    """Fail closed when the media volume is missing or unexpectedly empty."""
    if not media_dir.exists() or not media_dir.is_dir():
        logger.warning("Skipping library cleanup: media directory is unavailable: %s", media_dir)
        return False
    try:
        registered_files = conn.execute(
            "SELECT count(*) FROM synced_files"
        ).fetchone()[0]
        # A single unrelated file (a lock, a leftover note) must not be enough to
        # convince the guard that this directory really is the indexed library.
        has_stream_files = any(entry.name.endswith(".strm") for entry in media_dir.iterdir())
    except Exception as exc:
        logger.warning("Skipping library cleanup: cannot validate media directory %s: %s",
                       media_dir, exc)
        return False
    if registered_files and not has_stream_files:
        logger.warning(
            "Skipping library cleanup: %s holds no stream files while the catalog "
            "registers %s of them: the volume is missing or was replaced",
            media_dir, registered_files,
        )
        return False
    return True


def _loss_limit(population: int) -> int:
    return max(CLEANUP_LOSS_MIN_ENTRIES, int(population * CLEANUP_LOSS_FRACTION_LIMIT))


def _sources_blocked_by_scale(planned: Dict[str, int], population: Dict[str, int],
                             what: str, logger: Any) -> Set[str]:
    """Return sources whose deletion plan is too large to be trusted."""
    blocked = set()
    for source, count in sorted(planned.items()):
        total = population.get(source, 0)
        limit = _loss_limit(total)
        if count > limit:
            logger.warning(
                "Skipping %s for source '%s': the pass would remove %s of %s "
                "catalogued entries, above the guard limit of %s. Treating that as "
                "a missing or replaced media volume, or a source that stopped "
                "listing its items. Entries and files are kept, so stale rows "
                "survive until the source or the volume is repaired.",
                what, source, count, total, limit,
            )
            blocked.add(source)
    return blocked


@dataclass(frozen=True)
class LibraryCleanupServices:
    """Dependencies supplied dynamically by the ``sync.py`` facade."""

    is_cleanup_allowed: Callable
    video_is_disabled: Callable
    resolve_media_path: Callable
    logger: Any
    time: Any = _time


def cleanup_orphaned_records(media_dir: Path, conn: db.Connection,
                             providers_succeeded: Optional[Set[str]] = None, *,
                             services: LibraryCleanupServices) -> None:
    """Remove DB entries whose stream file is absent, if their source succeeded."""
    if not _media_root_is_safe_for_cleanup(media_dir, conn, services.logger):
        return
    cur = conn.execute("""SELECT sf.file_path, sf.video_id FROM synced_files sf
        WHERE NOT EXISTS (
            SELECT 1 FROM telegram_unavailable_quarantine q
            WHERE q.video_id=sf.video_id AND q.status='quarantined'
        )""")
    missing = []
    entries_by_source: Dict[str, int] = {}
    for file_path_str, video_id in cur.fetchall():
        source = video_id.split(":", 1)[0]
        entries_by_source[source] = entries_by_source.get(source, 0) + 1
        try:
            actual_path = services.resolve_media_path(file_path_str, media_dir)
        except ValueError as exc:
            services.logger.warning("Skipping unsafe media record: %s", exc)
            continue
        if not actual_path.exists() and services.is_cleanup_allowed(
                video_id, providers_succeeded=providers_succeeded):
            missing.append((file_path_str, video_id, source))

    if not missing:
        return

    planned = dict(Counter(source for _, _, source in missing))
    blocked = _sources_blocked_by_scale(planned, entries_by_source,
                                        "orphaned-entry cleanup", services.logger)
    survivors = [item for item in missing if item[2] not in blocked]
    if not survivors:
        return

    services.logger.info(
        f"Removing {len(survivors)} missing file records from database..."
    )
    with conn:
        for file_path_str, video_id, _source in survivors:
            conn.execute("DELETE FROM synced_files WHERE file_path = %s", (file_path_str,))
            count = conn.execute(
                "SELECT count(*) FROM synced_files WHERE video_id = %s", (video_id,)
            ).fetchone()[0]
            if count == 0:
                conn.execute("DELETE FROM videos WHERE id = %s", (video_id,))


def cleanup_retention(conn: db.Connection, media_dir: Path, retention_days: int,
                      providers_succeeded: Optional[Set[str]] = None, *,
                      services: LibraryCleanupServices) -> None:
    """Purge expired catalog rows/files for sources confirmed healthy this run."""
    if retention_days <= 0:
        return
    if not _media_root_is_safe_for_cleanup(media_dir, conn, services.logger):
        return
    now = int(services.time.time())
    cutoff = now - (retention_days * 86400)

    cur = conn.execute("""SELECT v.id, v.provider, v.title FROM videos v
        WHERE v.provider IS DISTINCT FROM 'telegram' AND v.last_seen < %s AND NOT EXISTS (
            SELECT 1 FROM telegram_unavailable_quarantine q
            WHERE q.video_id=v.id AND q.status='quarantined'
        )""", (cutoff,))
    candidates = cur.fetchall()
    expired = []
    for vid_id, provider, title in candidates:
        if services.is_cleanup_allowed(vid_id, provider, providers_succeeded):
            expired.append((vid_id, title, provider))
    return _purge_expired_videos(conn, media_dir, expired, retention_days, services)


def _purge_expired_videos(conn: db.Connection, media_dir: Path, expired: list, retention_days: int,
                          services: LibraryCleanupServices) -> None:
    """Apply the scale guard, then delete files and catalog rows for each expired video."""
    if not expired:
        services.logger.info("Retention check: no eligible non-Telegram videos expired within %s days.", retention_days)
        return

    population = {row[0]: row[1] for row in conn.execute(
        "SELECT provider, count(*) FROM videos GROUP BY provider").fetchall()}
    blocked = _sources_blocked_by_scale(dict(Counter(p for _, _, p in expired)),
                                        population, "retention", services.logger)
    expired = [item for item in expired if item[2] not in blocked]
    if not expired:
        return

    services.logger.info(
        f"Retention policy: purging {len(expired)} videos older than {retention_days} days..."
    )
    # Per-item transactions: a DB error must not roll back deletions that
    # already committed, leaving catalog rows pointing at deleted files.
    for vid_id, title, _provider in expired:
        _delete_retention_files(conn, media_dir, vid_id, services)
        try:
            with conn:
                conn.execute("DELETE FROM video_categories WHERE video_id = %s", (vid_id,))
                conn.execute("DELETE FROM synced_files WHERE video_id = %s", (vid_id,))
                conn.execute("DELETE FROM videos WHERE id = %s", (vid_id,))
        except Exception as exc:
            services.logger.warning(
                f"Retention: keeping catalog rows for {vid_id} after a DB error: {exc}"
            )
            continue
        services.logger.info(f"Retention: purged video {vid_id} ('{title}')")


def _delete_retention_files(conn: db.Connection, media_dir: Path, vid_id, services) -> None:
    f_cur = conn.execute(
        "SELECT file_path FROM synced_files WHERE video_id = %s", (vid_id,)
    )
    for (file_path,) in f_cur.fetchall():
        try:
            path = services.resolve_media_path(file_path, media_dir)
        except ValueError as exc:
            services.logger.warning("Skipping unsafe media record: %s", exc)
            continue
        for ext in (".strm", ".nfo", ".jpg", ".thumb_fail"):
            file_to_delete = path.with_suffix(ext)
            if file_to_delete.exists():
                try:
                    file_to_delete.unlink()
                    services.logger.info(f"Retention: deleted {file_to_delete.name}")
                except Exception as exc:
                    services.logger.warning(
                        f"Retention: error deleting {file_to_delete}: {exc}"
                    )


def purge_videos(conn: db.Connection, media_dir: Path, comp_ids: List[str], reason: str,
                 providers_succeeded: Optional[Set[str]] = None, force: bool = False, *,
                 services: LibraryCleanupServices) -> int:
    """Delete selected videos from disk and DB, respecting source guards."""
    if not comp_ids:
        return 0
    purged = 0
    # Per-item transactions so one failed video cannot roll back the DB rows of
    # videos whose files were already deleted earlier in this loop.
    for video_id in comp_ids:
        if services.video_is_disabled(video_id):
            continue
        if not force and not services.is_cleanup_allowed(
                video_id, providers_succeeded=providers_succeeded):
            continue
        try:
            rows = conn.execute(
                "SELECT file_path FROM synced_files WHERE video_id = %s", (video_id,)
            ).fetchall()
            for (file_path,) in rows:
                try:
                    path = services.resolve_media_path(file_path, media_dir)
                except ValueError:
                    continue
                for ext in (".strm", ".nfo", ".jpg", ".thumb_fail"):
                    try:
                        path.with_suffix(ext).unlink(missing_ok=True)
                    except Exception:
                        logger.debug("Ignored exception in purge_videos", exc_info=True)
            with conn:
                conn.execute("DELETE FROM video_categories WHERE video_id = %s", (video_id,))
                conn.execute("DELETE FROM synced_files WHERE video_id = %s", (video_id,))
                conn.execute("DELETE FROM videos WHERE id = %s", (video_id,))
            purged += 1
        except Exception as exc:
            services.logger.warning(f"Purge failed for {video_id}: {exc}")
    services.logger.info(f"Purged {purged} videos ({reason})")
    return purged
