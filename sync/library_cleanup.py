"""Guarded cleanup operations for the media catalog."""

from common import db
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, List, Optional, Set


def _media_root_is_safe_for_cleanup(media_dir: Path, conn: db.Connection,
                                    logger: Any) -> bool:
    """Fail closed when the media volume is missing or unexpectedly empty."""
    if not media_dir.exists() or not media_dir.is_dir():
        logger.warning("Skipping library cleanup: media directory is unavailable: %s", media_dir)
        return False
    try:
        has_catalog_files = conn.execute(
            "SELECT 1 FROM synced_files LIMIT 1"
        ).fetchone() is not None
        has_media_entries = next(media_dir.iterdir(), None) is not None
    except Exception as exc:
        logger.warning("Skipping library cleanup: cannot validate media directory %s: %s",
                       media_dir, exc)
        return False
    if has_catalog_files and not has_media_entries:
        logger.warning(
            "Skipping library cleanup: media directory is empty but the catalog has indexed files: %s",
            media_dir,
        )
        return False
    return True


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
    for file_path_str, video_id in cur.fetchall():
        try:
            actual_path = services.resolve_media_path(file_path_str, media_dir)
        except ValueError as exc:
            services.logger.warning("Skipping unsafe media record: %s", exc)
            continue
        if not actual_path.exists() and services.is_cleanup_allowed(
                video_id, providers_succeeded=providers_succeeded):
            missing.append((file_path_str, video_id))

    if missing:
        services.logger.info(
            f"Removing {len(missing)} missing file records from database..."
        )
        with conn:
            for file_path_str, video_id in missing:
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
        WHERE v.last_seen < %s AND NOT EXISTS (
            SELECT 1 FROM telegram_unavailable_quarantine q
            WHERE q.video_id=v.id AND q.status='quarantined'
        )""", (cutoff,))
    candidates = cur.fetchall()
    expired = []
    for vid_id, provider, title in candidates:
        if services.is_cleanup_allowed(vid_id, provider, providers_succeeded):
            expired.append((vid_id, title))

    if not expired:
        services.logger.info(
            f"Retention check: all active videos seen within {retention_days} days."
        )
        return

    services.logger.info(
        f"Retention policy: purging {len(expired)} videos older than {retention_days} days..."
    )
    # Per-item transactions: a DB error must not roll back deletions that
    # already committed, leaving catalog rows pointing at deleted files.
    for vid_id, title in expired:
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
                        pass
            with conn:
                conn.execute("DELETE FROM video_categories WHERE video_id = %s", (video_id,))
                conn.execute("DELETE FROM synced_files WHERE video_id = %s", (video_id,))
                conn.execute("DELETE FROM videos WHERE id = %s", (video_id,))
            purged += 1
        except Exception as exc:
            services.logger.warning(f"Purge failed for {video_id}: {exc}")
    services.logger.info(f"Purged {purged} videos ({reason})")
    return purged
