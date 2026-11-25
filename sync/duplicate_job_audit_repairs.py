"""Explicit local stream and artwork repairs for audit findings."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sync import duplicate_jobs as _core


@dataclass(frozen=True)
class _RepairTarget:
    """One selected catalog item with the inputs its repair needs."""
    item: dict
    streams: list
    can_restore_stream: bool
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
    if not isinstance(operation_type, str) or not operation_type.startswith("audit_repair") \
            or not isinstance(scope, dict):
        raise ValueError("Invalid audit repair plan.")
    audit_id, finding_ids = scope.get("audit_id"), scope.get("finding_ids")
    if (not isinstance(audit_id, str) or not isinstance(finding_ids, list) or not finding_ids
            or len(finding_ids) > 25 or any(type(value) is not int for value in finding_ids)):
        raise ValueError("Invalid audit repair selection.")
    return audit_id, finding_ids, revisions


def _repair_flags(findings: list) -> tuple[bool, bool]:
    """Return ``(can_restore_stream, repair_artwork)`` for one item's findings."""
    can_restore_stream = any(finding["kind"] == "missing_stream" for finding in findings)
    repair_artwork = any(finding["kind"] == "missing_artwork" for finding in findings)
    return can_restore_stream, repair_artwork


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
        artwork_path = stream.with_suffix(".jpg")
        if artwork_path.is_symlink() or not artwork_path.resolve().is_relative_to(media_root):
            raise ValueError("An artwork path is unavailable or unsafe.")
        if repair_artwork and artwork_path.exists() and not artwork_path.is_file():
            raise ValueError("An artwork path is unavailable or unsafe.")
        streams.append((stream, exists, artwork_path))
    return streams


def _collect_target(conn, item: dict, media_dir: Path, media_root: Path, services,
                    load_metadata_state) -> _RepairTarget:
    video_id = item["video_id"]
    state = load_metadata_state(conn, video_id)
    if state is None:
        raise ValueError("A selected catalog item is no longer available.")
    can_restore_stream, repair_artwork = _repair_flags(item["findings"])
    streams = _stream_entries(conn, video_id, media_dir, media_root, services,
                              can_restore_stream, repair_artwork)
    if not streams:
        raise ValueError("No registered stream files are available for a selected item.")
    thumbnail_url = str(state["source"].get("thumbnail") or "").strip()
    if repair_artwork and (not thumbnail_url or services.thumbnail_urls is None
                           or services.download_image_if_missing is None):
        raise ValueError("The selected artwork cannot be safely restored from its current catalog source.")
    return _RepairTarget(item=item, streams=streams, can_restore_stream=can_restore_stream,
                         repair_artwork=repair_artwork, thumbnail_url=thumbnail_url)


def _restore_streams(target: _RepairTarget, services) -> int:
    restored = 0
    for stream_path, _existed, _artwork_path in target.streams:
        if target.can_restore_stream and services.create_stream_file(stream_path, target.item["video_id"]):
            restored += 1
    return restored


def _restore_artwork(target: _RepairTarget, services) -> int:
    _public_url, download_url = services.thumbnail_urls(target.thumbnail_url)
    parsed = urlparse(str(download_url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("The selected artwork source is unavailable or unsafe.")
    written = 0
    for _stream_path, _existed, artwork_path in target.streams:
        if not services.download_image_if_missing(download_url, artwork_path):
            raise RuntimeError("Artwork could not be restored without replacing an existing file.")
        written += 1
    return written


def _audit_repair_artwork(conn, job_id: str, payload: dict, media_dir: Path, services) -> dict:
    """Repair explicit, revision-checked local stream and artwork findings."""
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
    repaired_artwork_files = 0
    for index, target in enumerate(targets, start=1):
        latest = _audit_repair_state(conn, audit_id, finding_ids)
        if latest is None or latest["revisions"] != revisions:
            raise ValueError("Audit findings or library metadata changed before the repair.")
        _core._record_job_progress(conn, job_id, "restoring_files", completed=index - 1, total=len(targets))
        restored_streams += _restore_streams(target, services)
        if target.repair_artwork:
            repaired_artwork_files += _restore_artwork(target, services)
        _core._record_job_progress(conn, job_id, "restoring_files", completed=index, total=len(targets))
    local_state = "files_repaired"
    _core._store_job_result_field(conn, job_id, "local_state", local_state)
    with conn:
        conn.execute("UPDATE admin_audit_items SET status='resolved' WHERE job_id=%s AND id=ANY(%s) AND status='open'",
                     (audit_id, finding_ids))
    return {"audit_id": audit_id, "repaired_items": len(targets), "restored_streams": restored_streams,
            "repaired_artwork_files": repaired_artwork_files, "local_state": local_state}
