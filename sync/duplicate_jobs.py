"""Durable duplicate decision job execution."""
import asyncio  # noqa: F401 -- duplicate_job_worker reads this through the compatibility facade
import hashlib  # noqa: F401 -- duplicate_job_metadata reads this through the compatibility facade
import json
import os
import re
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
_LONG_RUNNING_WORKER_ACTIONS = frozenset({"metadata_backfill", "telegram_fingerprint_scan"})

_AUDIT_INVENTORY_MAX_ENTRIES = 100000
_AUDIT_INVENTORY_MAX_DEPTH = 12


def _audit_library_inventory(media_dir: Path, registered_streams: set[str]):
    """Return orphan same-basename file families without following symlinks."""
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
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if depth >= _AUDIT_INVENTORY_MAX_DEPTH:
                                complete = False
                            else:
                                stack.append((Path(entry.path), depth + 1))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        complete = False
                        continue
                    relative = Path(entry.path).relative_to(root).as_posix()
                    suffix = Path(relative).suffix.lower()
                    if suffix not in {".strm", ".nfo", ".jpg"}:
                        continue
                    path = Path(relative)
                    key = (path.parent.as_posix(), path.stem)
                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        complete = False
                        continue
                    families.setdefault(key, []).append({
                        "path": relative, "suffix": suffix, "size": int(stat.st_size),
                        "mtime_ns": int(stat.st_mtime_ns), "dev": int(stat.st_dev),
                        "ino": int(stat.st_ino),
                    })
        except OSError:
            complete = False
    orphans = []
    registered_families = set()
    for registered in registered_streams:
        path = Path(registered)
        if path.suffix.lower() == ".strm":
            registered_families.add((path.parent.as_posix(), path.stem))
    for (parent, stem), files in sorted(families.items()):
        # Match the exact managed stream path; an orphan NFO/JPG is actionable
        # only when no registered STRM exists for its same-basename family.
        if (parent, stem) in registered_families:
            continue
        files.sort(key=lambda item: (item["path"].casefold(), item["path"]))
        orphans.append({"files": files, "total_bytes": sum(item["size"] for item in files)})
    return orphans, scanned, complete


def _audit_nfo_metadata_signature(root):
    """Return catalog-derived fields, ignoring XML formatting and set ordering."""
    simple_fields = ("title", "originaltitle", "studio", "premiered", "year", "plot",
                     "runtime", "genre", "tag")
    unordered_fields = {"studio", "genre", "tag"}
    simple = {}
    for name in simple_fields:
        values = tuple((node.text or "").replace("\r\n", "\n").strip()
                       for node in root.findall(name))
        if name in unordered_fields:
            values = tuple(sorted({value for value in values if value}))
        simple[name] = values
    sets = tuple(sorted({(node.findtext("name", "") or "").strip()
                         for node in root.findall("set")
                         if (node.findtext("name", "") or "").strip()}))
    actors = tuple(((node.findtext("name", "") or "").strip(),
                    (node.findtext("role", "") or "").strip()) for node in root.findall("actor"))
    return {"simple": dict(simple), "collections": sets, "cast": actors}


def _audit_nfo_metadata_differences(actual, expected):
    """Name differing NFO fields without retaining or exposing their values."""
    labels = {
        "title": "title",
        "originaltitle": "original title",
        "studio": "studio",
        "premiered": "premiered date",
        "year": "year",
        "plot": "plot",
        "runtime": "runtime",
        "genre": "genres",
        "tag": "tags",
    }
    actual_signature = _audit_nfo_metadata_signature(actual)
    expected_signature = _audit_nfo_metadata_signature(expected)
    differences = [labels[name] for name in labels
                   if actual_signature["simple"][name] != expected_signature["simple"][name]]
    if actual_signature["collections"] != expected_signature["collections"]:
        differences.append("collections")
    if actual_signature["cast"] != expected_signature["cast"]:
        differences.append("cast")
    return differences


def _audit_nfo_metadata_diff_details(actual, expected):
    """Keep bounded, path-free before/after values for allowlisted NFO fields."""
    labels = {
        "title": "Title", "originaltitle": "Original title", "studio": "Studio",
        "premiered": "Release date", "year": "Year", "plot": "Description",
        "runtime": "Runtime", "genre": "Genres", "tag": "Tags",
    }
    before, after = _audit_nfo_metadata_signature(actual), _audit_nfo_metadata_signature(expected)

    def display(values, other_values):
        value = " · ".join(str(part) for part in values if str(part).strip())
        value = re.sub(r"https?://\S+", "[link]", value, flags=re.IGNORECASE)
        value = "".join(char for char in value if char >= " " or char in "\t\n")
        if len(value) <= 300:
            return value
        other = " · ".join(str(part) for part in other_values if str(part).strip())
        other = re.sub(r"https?://\S+", "[link]", other, flags=re.IGNORECASE)
        other = "".join(char for char in other if char >= " " or char in "\t\n")
        common = 0
        for current_char, expected_char in zip(value, other):
            if current_char != expected_char:
                break
            common += 1
        start = max(0, common - 90)
        end = min(len(value), common + 150)
        return f"[{start} characters before] … {value[start:end]} … [{len(value) - end} characters after]"

    diffs = []
    for field, label in labels.items():
        if before["simple"][field] != after["simple"][field]:
            diffs.append({"field": label, "current": display(before["simple"][field], after["simple"][field]),
                          "expected": display(after["simple"][field], before["simple"][field])})
    if before["collections"] != after["collections"]:
        diffs.append({"field": "Collections", "current": display(before["collections"], after["collections"]),
                      "expected": display(after["collections"], before["collections"])})
    if before["cast"] != after["cast"]:
        current_cast = tuple(" — ".join(pair) for pair in before["cast"])
        expected_cast = tuple(" — ".join(pair) for pair in after["cast"])
        diffs.append({"field": "Cast", "current": display(current_cast, expected_cast),
                      "expected": display(expected_cast, current_cast)})
    return {"diffs": diffs}


def _record_job_progress(conn, job_id: str, stage: str, *, completed=None, total=None,
                         local_state=None, jellyfin_refresh=None) -> None:
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
    if jellyfin_refresh is not None:
        result["jellyfin_refresh"] = jellyfin_refresh
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


def _refresh_jellyfin_with_progress(conn, job_id: str, local_state: str, services) -> bool:
    """Persist local success and the remote request stage before calling Jellyfin."""
    _record_job_progress(
        conn, job_id, "refreshing_jellyfin", local_state=local_state, jellyfin_refresh="pending"
    )
    return bool(services.trigger_jellyfin_refresh())


def _json_list_value(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []
    return []


def _metadata_update(conn, job_id: str, payload: dict, media_dir: Path, services) -> dict:
    if __package__:
        from .duplicate_job_metadata import _metadata_update as implementation
    else:
        from duplicate_job_metadata import _metadata_update as implementation
    return implementation(conn, job_id, payload, media_dir, services)


def _availability_check(conn, job_id: str, payload: dict, services) -> dict:
    if __package__:
        from .duplicate_job_metadata import _availability_check as implementation
    else:
        from duplicate_job_metadata import _availability_check as implementation
    return implementation(conn, job_id, payload, services)


def _metadata_quality_audit(conn, job_id: str, payload: dict | None = None) -> dict:
    try:
        from .duplicate_job_library_audit import _metadata_quality_audit as implementation
    except ImportError:
        from duplicate_job_library_audit import _metadata_quality_audit as implementation
    return implementation(conn, job_id, payload)


def _consistency_audit(conn, job_id: str, media_dir: Path, services, payload: dict | None = None) -> dict:
    try:
        from .duplicate_job_library_audit import _consistency_audit as implementation
    except ImportError:
        from duplicate_job_library_audit import _consistency_audit as implementation
    return implementation(conn, job_id, media_dir, services, payload)



def _audit_repair_nfo(conn, job_id: str, payload: dict, media_dir: Path, services) -> dict:
    try:
        from .duplicate_job_library_audit import _audit_repair_nfo as implementation
    except ImportError:
        from duplicate_job_library_audit import _audit_repair_nfo as implementation
    return implementation(conn, job_id, payload, media_dir, services)



def _audit_import_refresh(conn, job_id: str, payload: dict, services) -> dict:
    try:
        from .duplicate_job_library_audit import _audit_import_refresh as implementation
    except ImportError:
        from duplicate_job_library_audit import _audit_import_refresh as implementation
    return implementation(conn, job_id, payload, services)



def _orphan_file_operation(conn, job_id: str, payload: dict, media_dir: Path, quarantine_root: Path,
                           services, moved: list[tuple[Path, Path]]) -> dict:
    try:
        from .duplicate_job_library_audit import _orphan_file_operation as implementation
    except ImportError:
        from duplicate_job_library_audit import _orphan_file_operation as implementation
    return implementation(conn, job_id, payload, media_dir, quarantine_root, services, moved)



@dataclass(frozen=True)
class DuplicateJobServices:
    jobs_lock_root: Callable
    video_is_disabled: Callable
    write_catalog_backup: Callable
    duplicate_file_matches: Callable
    duplicate_move_file: Callable
    create_nfo_file: Callable
    create_stream_file: Callable
    format_dateadded: Callable
    load_performer_thumbs: Callable
    resolve_media_path: Callable
    trigger_jellyfin_refresh: Callable
    probe_stream: Callable
    logger: object
    probe_stream_detailed: Callable | None = None
    audit_jellyfin_import_state: Callable | None = None
    thumbnail_urls: Callable | None = None
    download_image_if_missing: Callable | None = None
    external_metadata_backfill: Callable | None = None
    telegram_fingerprint_scan: Callable | None = None
    sync_jellyfin_telegram_metadata: Callable | None = None


def _performer_refresh(conn: db.Connection, job_id: str, payload: dict,
                       media_dir: Path, services: DuplicateJobServices) -> dict:
    """Rebuild NFOs after approved performer assignments and request Jellyfin refresh."""
    ids = payload.get("video_ids")
    if not isinstance(ids, list) or not ids or len(ids) > 100:
        raise ValueError("Performer refresh requires 1–100 video IDs.")
    import sync as S
    updated = 0
    for index, video_id in enumerate(dict.fromkeys(str(value) for value in ids)):
        row = conn.execute("""
            SELECT provider,title,slug,duration,thumbnail,performers,categories,tags,studio,first_seen,plot
            FROM videos WHERE id=%s
        """, (video_id,)).fetchone()
        if not row:
            continue
        provider,title,slug,duration,thumbnail,raw_performers,categories,tags,studio,first_seen,plot = row
        performers = _json_list_value(raw_performers)
        genres, tag_set = set(_json_list_value(categories)), set(_json_list_value(tags))
        video = {"id": video_id, "provider": provider,
                 "provider_video_id": video_id.split(":", 1)[-1], "title": title or video_id,
                 "slug": slug or "", "duration": duration or 0, "thumbnail": thumbnail or "",
                 "performers": performers, "performer_thumbs": services.load_performer_thumbs(conn, performers),
                 "studio": studio or "", "plot": plot or "",
                 "dateadded": services.format_dateadded(first_seen or time.time())}
        nfo_path = media_dir / f"{S.stable_media_basename(video_id)}.nfo"
        services.create_nfo_file(video, genres, tag_set, nfo_path, conn=conn)
        updated += 1
        _record_job_progress(conn, job_id, "writing_nfo", completed=index + 1, total=len(ids))
    refreshed = services.trigger_jellyfin_refresh()
    if not refreshed:
        raise RuntimeError("Jellyfin did not accept the library refresh request.")
    return {"videos_refreshed": updated, "jellyfin_refresh": "requested"}


if __package__:
    from .duplicate_job_worker import process_video_duplicate_jobs_locked
else:
    from duplicate_job_worker import process_video_duplicate_jobs_locked  # noqa: F401

