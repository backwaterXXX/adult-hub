"""Durable duplicate decision job execution."""
import asyncio  # noqa: F401 -- duplicate_job_worker reads this through the compatibility facade
import hashlib  # noqa: F401 -- duplicate_job_metadata reads this through the compatibility facade
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from common import db
from common.job_queue import WORKER_ACTIONS  # noqa: F401 -- exposed to duplicate_job_worker
from common.job_stages import WORKING_STAGES
from psycopg.types.json import Jsonb  # noqa: F401 -- job handlers read this through the facade


_JOB_STAGES = WORKING_STAGES
_LONG_RUNNING_WORKER_ACTIONS = frozenset({
    "metadata_backfill", "telegram_fingerprint_scan", "source_remove", "phash_generation",
    "filesize_backfill", "dead_stream_quarantine",
})

def _audit_entry_limit() -> int:
    """Return the inventory walk bound, overridable for unusually large libraries.

    The walk is a runaway guard, not a sampling policy: when it trips, the audit
    reports itself as partial and stale findings are never resolved. It has to
    stay above the live file count (about 110k today).
    """
    raw = os.getenv("AUDIT_INVENTORY_MAX_ENTRIES", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return 250000


_AUDIT_INVENTORY_MAX_ENTRIES = _audit_entry_limit()
_AUDIT_INVENTORY_MAX_DEPTH = 12


def _audit_library_inventory(media_dir: Path, registered_streams: set[str],
                             registered_sidecars: set[str] | None = None):
    """Return orphan same-basename file families without following symlinks.

    ``registered_sidecars`` carries paths registered outside ``synced_files``
    (performer artwork); a family that holds one of them is not an orphan.
    """
    root = Path(media_dir).resolve()
    stack = [(root, 0)]
    families: dict[tuple[str, str], list[dict]] = {}
    scanned = 0
    complete = True
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if scanned >= _AUDIT_INVENTORY_MAX_ENTRIES:
                        complete = False
                        stack.clear()
                        break
                    scanned += 1
                    kind = _entry_kind(entry)
                    if kind == "incomplete":
                        complete = False
                    elif kind == "dir":
                        if depth >= _AUDIT_INVENTORY_MAX_DEPTH:
                            complete = False
                        else:
                            stack.append((Path(entry.path), depth + 1))
                    elif kind == "file" and not _record_family_file(entry, root, families):
                        complete = False
        except OSError:
            complete = False
    return _orphan_families(families, registered_streams, registered_sidecars), scanned, complete


def _entry_kind(entry) -> str:
    """Classify one scandir entry as 'dir', 'file', 'skip', or 'incomplete' (could not be read)."""
    try:
        if entry.is_symlink():
            return "skip"
        if entry.is_dir(follow_symlinks=False):
            return "dir"
        if not entry.is_file(follow_symlinks=False):
            return "skip"
    except OSError:
        return "incomplete"
    return "file"


def _record_family_file(entry, root: Path, families: dict) -> bool:
    """Add a sidecar-capable file to its same-basename family. False if its metadata could not be read."""
    relative = Path(entry.path).relative_to(root).as_posix()
    suffix = Path(relative).suffix.lower()
    if suffix not in {".strm", ".jpg"}:
        return True
    path = Path(relative)
    # Artwork extraction writes dot-prefixed temporaries next to the target; a
    # pass that overlaps a running extraction must not report them as orphans.
    if path.name.startswith("."):
        return True
    key = (path.parent.as_posix(), path.stem)
    try:
        stat = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    families.setdefault(key, []).append({
        "path": relative, "suffix": suffix, "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns), "dev": int(stat.st_dev),
        "ino": int(stat.st_ino),
    })
    return True


def _orphan_families(families: dict, registered_streams: set[str],
                     registered_sidecars: set[str] | None = None) -> list:
    orphans = []
    registered_families = set()
    for registered in registered_streams:
        path = Path(registered)
        if path.suffix.lower() == ".strm":
            registered_families.add((path.parent.as_posix(), path.stem))
    for (parent, stem), files in sorted(families.items()):
        # Match the exact managed stream path; an orphan JPG is actionable
        # only when no registered STRM exists for its same-basename family.
        if (parent, stem) in registered_families:
            continue
        if registered_sidecars and any(item["path"] in registered_sidecars for item in files):
            continue
        files.sort(key=lambda item: (item["path"].casefold(), item["path"]))
        orphans.append({"files": files, "total_bytes": sum(item["size"] for item in files)})
    return orphans


def _record_job_progress(conn, job_id: str, stage: str, *, completed=None, total=None,
                         local_state=None) -> None:
    """Persist a safe checkpoint without overwriting the worker recovery journal."""
    if stage not in _JOB_STAGES:
        raise ValueError("Unsupported job progress stage.")
    now = int(time.time())
    row = conn.execute("SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
    try:
        result = json.loads(row[0]) if row and row[0] else {}
    except (TypeError, ValueError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    progress = {"stage": stage, "updated_at": now}
    if isinstance(completed, int) and completed >= 0:
        progress["completed"] = completed
    if isinstance(total, int) and total >= 0:
        progress["total"] = total
    events = result.get("events") if isinstance(result.get("events"), list) else []
    event = {key: progress[key] for key in ("stage", "updated_at", "completed", "total") if key in progress}
    events.append(event)
    result["events"] = events[-20:]
    result["progress"] = progress
    if local_state is not None:
        result["local_state"] = local_state
    with conn:
        conn.execute(
            "UPDATE video_duplicate_jobs SET result=%s, updated_at=%s WHERE id=%s AND state='processing'",
            (json.dumps(result, ensure_ascii=False), now, job_id),
        )


def _heartbeat_processing_job(db_url, job_id: str, stop_event: threading.Event, logger, interval: float = 10.0) -> None:
    """Refresh last-activity time while a worker is inside a long blocking operation."""
    while not stop_event.wait(interval):
        heartbeat_conn = None
        try:
            heartbeat_conn = db.connect(db_url)
            heartbeat_conn.execute(
                "UPDATE video_duplicate_jobs SET updated_at=%s WHERE id=%s AND state='processing'",
                (int(time.time()), job_id),
            )
            heartbeat_conn.commit()
        except Exception as exc:
            logger.debug("Could not update worker heartbeat (%s)", type(exc).__name__)
            if heartbeat_conn is not None:
                heartbeat_conn.rollback()
        finally:
            if heartbeat_conn is not None:
                heartbeat_conn.close()


def _store_job_result_field(conn, job_id: str, key: str, value) -> None:
    """Update recovery data without discarding progress events or other journal fields."""
    row = conn.execute("SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
    try:
        result = json.loads(row[0]) if row and row[0] else {}
    except (TypeError, ValueError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    result[key] = value
    with conn:
        conn.execute(
            "UPDATE video_duplicate_jobs SET result=%s, updated_at=%s WHERE id=%s AND state='processing'",
            (json.dumps(result, ensure_ascii=False), int(time.time()), job_id),
        )


def _finish_job(conn, job_id: str, result: dict) -> None:
    """Keep the compact activity history when replacing an active recovery journal with its outcome."""
    row = conn.execute("SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
    try:
        previous = json.loads(row[0]) if row and row[0] else {}
    except (TypeError, ValueError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    finished = dict(result)
    events = previous.get("events") if isinstance(previous.get("events"), list) else []
    now = int(time.time())
    events = [event for event in events if isinstance(event, dict)][-19:]
    events.append({"stage": "completed", "updated_at": now})
    finished["events"] = events
    finished["progress"] = {"stage": "completed", "updated_at": now}
    with conn:
        conn.execute(
            "UPDATE video_duplicate_jobs SET state='done', result=%s, updated_at=%s WHERE id=%s AND state='processing'",
            (json.dumps(finished, ensure_ascii=False), now, job_id),
        )


def _metadata_update(conn, job_id: str, payload: dict, media_dir: Path, services) -> dict:
    from sync.duplicate_job_metadata import _metadata_update as implementation
    return implementation(conn, job_id, payload, media_dir, services)


def _availability_check(conn, job_id: str, payload: dict, services) -> dict:
    from sync.duplicate_job_metadata import _availability_check as implementation
    return implementation(conn, job_id, payload, services)


def _metadata_quality_audit(conn, job_id: str, payload: dict | None = None) -> dict:
    from sync.duplicate_job_library_audit import _metadata_quality_audit as implementation
    return implementation(conn, job_id, payload)


def _consistency_audit(conn, job_id: str, media_dir: Path, services, payload: dict | None = None) -> dict:
    from sync.duplicate_job_library_audit import _consistency_audit as implementation
    return implementation(conn, job_id, media_dir, services, payload)



def _audit_repair_artwork(conn, job_id: str, payload: dict, media_dir: Path, services) -> dict:
    from sync.duplicate_job_library_audit import _audit_repair_artwork as implementation
    return implementation(conn, job_id, payload, media_dir, services)



def _orphan_file_operation(conn, job_id: str, payload: dict, media_dir: Path, quarantine_root: Path,
                           services, moved: list[tuple[Path, Path]]) -> dict:
    from sync.duplicate_job_library_audit import _orphan_file_operation as implementation
    return implementation(conn, job_id, payload, media_dir, quarantine_root, services, moved)



@dataclass(frozen=True)
class DuplicateJobServices:
    jobs_lock_root: Callable
    video_is_disabled: Callable
    write_catalog_backup: Callable
    duplicate_file_matches: Callable
    duplicate_move_file: Callable
    create_stream_file: Callable
    resolve_media_path: Callable
    probe_stream: Callable
    logger: object
    probe_stream_detailed: Callable | None = None
    thumbnail_urls: Callable | None = None
    download_image_if_missing: Callable | None = None
    external_metadata_backfill: Callable | None = None
    telegram_fingerprint_scan: Callable | None = None


def __getattr__(name: str):
    # Resolved on first access instead of at import: the worker imports sibling duplicate_job_*
    # modules that import this one, so a module-level import here closes an import cycle.
    if name == "process_video_duplicate_jobs_locked":
        from sync.duplicate_job_worker import process_video_duplicate_jobs_locked
        return process_video_duplicate_jobs_locked
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

