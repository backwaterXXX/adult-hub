"""Explicit local repairs and Jellyfin import refresh handlers."""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

if __package__:
    from . import duplicate_jobs as _core
else:
    import duplicate_jobs as _core


@dataclass(frozen=True)
class _RepairTarget:
    """One selected catalog item with the inputs its repair needs."""
    item: dict
    nfo_video: dict
    categories: set
    tags: set
    streams: list
    can_restore_stream: bool
    repair_nfo: bool
    repair_artwork: bool
    thumbnail_url: str


def _load_audit_repair_plan(conn, payload: dict):
    """Validate the queued plan and return ``(audit_id, finding_ids, revisions)``."""
    plan_id = payload.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id or len(plan_id) > 100:
        raise ValueError("Invalid audit repair plan.")
    plan = conn.execute(
        "SELECT operation_type,scope,revisions FROM admin_operation_plans WHERE plan_id=%s AND state='queued'",
        (plan_id,),
    ).fetchone()
    if not plan:
        raise ValueError("Audit repair plan is unavailable.")
    operation_type, raw_scope, raw_revisions = plan
    scope = raw_scope if isinstance(raw_scope, dict) else json.loads(raw_scope or "{}")
    revisions = raw_revisions if isinstance(raw_revisions, dict) else json.loads(raw_revisions or "{}")
    if operation_type != "audit_repair_nfo" or not isinstance(scope, dict):
        raise ValueError("Invalid audit repair plan.")
    audit_id, finding_ids = scope.get("audit_id"), scope.get("finding_ids")
    if (not isinstance(audit_id, str) or not isinstance(finding_ids, list) or not finding_ids
            or len(finding_ids) > 25 or any(type(value) is not int for value in finding_ids)):
        raise ValueError("Invalid audit repair selection.")
    return audit_id, finding_ids, revisions


def _repair_flags(findings: list) -> tuple[bool, bool, bool]:
    """Return ``(can_restore_stream, repair_nfo, repair_artwork)`` for one item's findings."""
    can_restore_stream = any(finding["kind"] == "missing_stream" for finding in findings)
    repair_nfo = any(finding["kind"] in {"missing_nfo", "invalid_nfo", "nfo_metadata_mismatch"}
                     for finding in findings)
    repair_artwork = any(finding["kind"] == "missing_artwork" for finding in findings)
    return can_restore_stream, repair_nfo, repair_artwork


def _stream_entries(conn, video_id: str, media_dir: Path, media_root: Path, services,
                    can_restore_stream: bool, repair_artwork: bool) -> list:
    """Validate every registered ``.strm`` file of the item and return its repair paths."""
    file_rows = conn.execute(
        "SELECT file_path FROM synced_files WHERE video_id=%s ORDER BY file_path", (video_id,)
    ).fetchall()
    streams = []
    for file_row in file_rows:
        stored_path = str(file_row[0])
        if not stored_path.lower().endswith(".strm"):
            continue
        stream = services.resolve_media_path(stored_path, media_dir)
        if stream.is_symlink() or not stream.resolve().is_relative_to(media_root):
            raise ValueError("A registered stream path is unavailable or unsafe.")
        exists = stream.exists()
        if exists and not stream.is_file():
            raise ValueError("A registered stream path is unavailable or unsafe.")
        if not exists and not can_restore_stream:
            raise ValueError("A registered stream is missing without a selected stream repair finding.")
        nfo_path = stream.with_suffix(".nfo")
        if nfo_path.is_symlink() or not nfo_path.resolve().is_relative_to(media_root):
            raise ValueError("An NFO path is unavailable or unsafe.")
        artwork_path = stream.with_suffix(".jpg")
        if artwork_path.is_symlink() or not artwork_path.resolve().is_relative_to(media_root):
            raise ValueError("An artwork path is unavailable or unsafe.")
        if repair_artwork and artwork_path.exists() and not artwork_path.is_file():
            raise ValueError("An artwork path is unavailable or unsafe.")
        streams.append((stream, nfo_path, exists, artwork_path))
    return streams


def _collect_target(conn, item: dict, media_dir: Path, media_root: Path, services,
                    load_metadata_state) -> _RepairTarget:
    video_id = item["video_id"]
    state = load_metadata_state(conn, video_id)
    if state is None:
        raise ValueError("A selected catalog item is no longer available.")
    can_restore_stream, repair_nfo, repair_artwork = _repair_flags(item["findings"])
    streams = _stream_entries(conn, video_id, media_dir, media_root, services,
                              can_restore_stream, repair_artwork)
    if not streams:
        raise ValueError("No registered stream files are available for a selected item.")
    categories = {str(row[0]) for row in conn.execute(
        "SELECT category FROM video_categories WHERE video_id=%s ORDER BY category", (video_id,)
    ).fetchall() if row[0]}
    tags = set(state["metadata"].get("tags") or [])
    nfo_video = {**state["source"], **state["metadata"],
                 "dateadded": services.format_dateadded(state["source"].get("first_seen"))}
    thumbnail_url = str(state["source"].get("thumbnail") or "").strip()
    if repair_artwork and (not thumbnail_url or services.thumbnail_urls is None
                           or services.download_image_if_missing is None):
        raise ValueError("The selected artwork cannot be safely restored from its current catalog source.")
    return _RepairTarget(item=item, nfo_video=nfo_video, categories=categories, tags=tags,
                         streams=streams, can_restore_stream=can_restore_stream,
                         repair_nfo=repair_nfo, repair_artwork=repair_artwork,
                         thumbnail_url=thumbnail_url)


def _restore_streams(target: _RepairTarget, services) -> int:
    restored = 0
    for stream_path, _nfo_path, _existed, _artwork_path in target.streams:
        if target.can_restore_stream and services.create_stream_file(stream_path, target.item["video_id"]):
            restored += 1
    return restored


def _write_nfos(target: _RepairTarget, services) -> int:
    written = 0
    for _stream_path, nfo_path, _existed, _artwork_path in target.streams:
        services.create_nfo_file(target.nfo_video, target.categories, target.tags, nfo_path)
        root = ET.parse(nfo_path).getroot()
        if (root.tag != "movie" or (root.findtext("originaltitle", "") or "")
                != str(target.nfo_video.get("title") or "")):
            raise RuntimeError("Generated NFO validation failed.")
        written += 1
    return written


def _restore_artwork(target: _RepairTarget, services) -> int:
    _public_url, download_url = services.thumbnail_urls(target.thumbnail_url)
    parsed = urlparse(str(download_url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("The selected artwork source is unavailable or unsafe.")
    written = 0
    for _stream_path, _nfo_path, _existed, artwork_path in target.streams:
        if not services.download_image_if_missing(download_url, artwork_path):
            raise RuntimeError("Artwork could not be restored without replacing an existing file.")
        written += 1
    return written


def _audit_repair_nfo(conn, job_id: str, payload: dict, media_dir: Path, services) -> dict:
    """Repair explicit, revision-checked local stream/NFO findings."""
    from common.admin_operations import _audit_repair_state
    from common.video_metadata import load_metadata_state
    audit_id, finding_ids, revisions = _load_audit_repair_plan(conn, payload)
    current = _audit_repair_state(conn, audit_id, finding_ids)
    if current is None or current["revisions"] != revisions:
        raise ValueError("Audit findings or library metadata changed after the repair preview.")

    media_root = media_dir.resolve()
    targets = [_collect_target(conn, item, media_dir, media_root, services, load_metadata_state)
               for item in current["items"]]

    restored_streams = 0
    repaired_nfo_files = 0
    has_stream_repair = any(any(finding["kind"] in {"missing_stream", "missing_artwork"}
                                for finding in target.item["findings"])
                            for target in targets)
    repaired_artwork_files = 0
    for index, target in enumerate(targets, start=1):
        latest = _audit_repair_state(conn, audit_id, finding_ids)
        if latest is None or latest["revisions"] != revisions:
            raise ValueError("Audit findings or library metadata changed before NFO repair.")
        _core._record_job_progress(conn, job_id, "repairing_nfo", completed=index - 1, total=len(targets))
        restored_streams += _restore_streams(target, services)
        if target.repair_nfo:
            repaired_nfo_files += _write_nfos(target, services)
        if target.repair_artwork:
            repaired_artwork_files += _restore_artwork(target, services)
        _core._record_job_progress(conn, job_id, "repairing_nfo", completed=index, total=len(targets))
    local_state = "files_repaired" if has_stream_repair else "nfo_repaired"
    _core._store_job_result_field(conn, job_id, "local_state", local_state)
    refreshed = _core._refresh_jellyfin_with_progress(conn, job_id, local_state, services)
    if not refreshed:
        _core._store_job_result_field(conn, job_id, "jellyfin_refresh", "failed")
        raise RuntimeError("Local files were repaired, but Jellyfin refresh failed. Retry this task to resume the refresh.")
    with conn:
        conn.execute("UPDATE admin_audit_items SET status='resolved' WHERE job_id=%s AND id=ANY(%s) AND status='open'",
                     (audit_id, finding_ids))
    return {"audit_id": audit_id, "repaired_items": len(targets), "restored_streams": restored_streams,
            "repaired_nfo_files": repaired_nfo_files, "repaired_artwork_files": repaired_artwork_files,
            "local_state": local_state,
            "jellyfin_refresh": "updated"}

def _audit_import_refresh(conn, job_id: str, payload: dict, services) -> dict:
    """Request Jellyfin refresh for selected import findings without claiming confirmation."""
    from common.admin_operations import _audit_import_refresh_state
    plan_id = payload.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id or len(plan_id) > 100:
        raise ValueError("Invalid audit import refresh plan.")
    plan = conn.execute(
        "SELECT operation_type,scope,revisions FROM admin_operation_plans WHERE plan_id=%s AND state='queued'",
        (plan_id,),
    ).fetchone()
    if not plan:
        raise ValueError("Audit import refresh plan is unavailable.")
    operation_type, raw_scope, raw_revisions = plan
    scope = raw_scope if isinstance(raw_scope, dict) else json.loads(raw_scope or "{}")
    revisions = raw_revisions if isinstance(raw_revisions, dict) else json.loads(raw_revisions or "{}")
    if operation_type != "audit_import_refresh" or not isinstance(scope, dict):
        raise ValueError("Invalid audit import refresh plan.")
    audit_id, finding_ids = scope.get("audit_id"), scope.get("finding_ids")
    if (not isinstance(audit_id, str) or not isinstance(finding_ids, list) or not finding_ids
            or len(finding_ids) > 25 or any(type(value) is not int for value in finding_ids)):
        raise ValueError("Invalid audit import refresh selection.")
    current = _audit_import_refresh_state(conn, audit_id, finding_ids)
    if current is None or current["revisions"] != revisions:
        raise ValueError("Audit findings or library metadata changed after the refresh preview.")
    _core._record_job_progress(conn, job_id, "refreshing_jellyfin", completed=0, total=len(current["items"]))
    if not services.trigger_jellyfin_refresh():
        _core._store_job_result_field(conn, job_id, "jellyfin_import", "not_confirmed")
        raise RuntimeError("Jellyfin did not accept the refresh request. Retry this task later.")
    _core._store_job_result_field(conn, job_id, "jellyfin_import", "not_confirmed")
    return {"audit_id": audit_id, "refresh_requested": True, "jellyfin_import": "not_confirmed",
            "message": "Refresh request accepted; run a new audit to verify the import."}
