"""Library consistency audit and repair job handlers."""
from __future__ import annotations

import json
import os
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from common import db
from psycopg.types.json import Jsonb

from sync import duplicate_jobs as _core


def _parse_stored_overrides(overrides) -> dict:
    if isinstance(overrides, str):
        try:
            overrides = json.loads(overrides)
        except (TypeError, ValueError):
            overrides = {}
    return overrides if isinstance(overrides, dict) else {}


def _missing_metadata_fields(effective: dict, source: dict) -> list:
    missing_fields = []
    if not str(effective.get("title") or "").strip():
        missing_fields.append("title")
    if not effective.get("performers"):
        missing_fields.append("performers")
    if not str(effective.get("studio") or "").strip():
        missing_fields.append("studio")
    if not str(effective.get("plot") or "").strip():
        missing_fields.append("plot")
    if not str(source.get("thumbnail") or "").strip():
        missing_fields.append("thumbnail")
    return missing_fields


def _record_missing_metadata(conn, job_id: str, video_id, missing_fields: list) -> None:
    with conn:
        conn.execute(
            "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details,checked_at) "
            "VALUES (%s,%s,'missing_metadata','open',%s,%s,to_timestamp(%s))",
            (job_id, video_id, "Effective catalog metadata is missing one or more fields.",
             Jsonb({"fields": missing_fields}), int(time.time())),
        )


def _metadata_quality_audit(conn, job_id: str, payload: dict | None = None) -> dict:
    """Audit effective catalog metadata without walking files or contacting Jellyfin."""
    from common.video_metadata import resolve_metadata

    with conn:
        conn.execute("DELETE FROM admin_audit_items WHERE job_id=%s", (job_id,))
    columns = db.table_columns(conn, "videos")
    rows = conn.execute(
        "SELECT v.*,o.fields FROM videos v "
        "LEFT JOIN video_metadata_overrides o ON o.video_id=v.id ORDER BY v.id"
    ).fetchall()
    total = len(rows)
    findings = 0
    for index, row in enumerate(rows, start=1):
        _core._record_job_progress(conn, job_id, "auditing_metadata", completed=index - 1, total=total)
        source = dict(zip(columns, row[:len(columns)]))
        overrides = _parse_stored_overrides(row[len(columns)])
        effective = resolve_metadata(source, overrides)
        missing_fields = _missing_metadata_fields(effective, source)
        if missing_fields:
            _record_missing_metadata(conn, job_id, source["id"], missing_fields)
            findings += 1
        _core._record_job_progress(conn, job_id, "auditing_metadata", completed=index, total=total)

    # A complete catalog pass resolves prior missing-field findings that were not reproduced.
    with conn:
        conn.execute(
            "UPDATE admin_audit_items old SET status='resolved' WHERE old.job_id<>%s "
            "AND old.status='open' AND old.kind='missing_metadata' AND NOT EXISTS ("
            "SELECT 1 FROM admin_audit_items current WHERE current.job_id=%s "
            "AND current.video_id=old.video_id AND current.kind='missing_metadata' AND current.status='open')",
            (job_id, job_id),
        )
    payload = payload if isinstance(payload, dict) else {}
    result = {"local_state": "metadata_quality_audit_completed", "audit_mode": "metadata_quality",
              "checked": total, "total": total, "findings": findings}
    if isinstance(payload.get("audit_batch_id"), str):
        result["audit_batch_id"] = payload["audit_batch_id"]
    return result


class _AuditContext:
    """Inputs and running tallies shared by the helpers of one consistency audit pass."""

    def __init__(self, conn, job_id: str, mode: str, media_dir: Path, services, video_columns, resolve_metadata):
        self.conn = conn
        self.job_id = job_id
        self.mode = mode
        self.media_dir = media_dir
        self.services = services
        self.video_columns = video_columns
        self.resolve_metadata = resolve_metadata
        self.checked = 0
        self.findings = 0
        self.missing_metadata_seen: set[str] = set()


def _index_registered_rows(rows, media_dir: Path, media_root: Path, services) -> tuple:
    registered_streams = set()
    registered_by_name = {}
    registered_names_by_video = {}
    for row in rows:
        stored_path, video_id = row[0], row[1]
        name = Path(str(stored_path)).name
        registered_by_name.setdefault(name, []).append(video_id)
        if video_id is not None:
            registered_names_by_video.setdefault(str(video_id), []).append(name)
        try:
            stream_path = services.resolve_media_path(stored_path, media_dir)
            registered_streams.add(stream_path.relative_to(media_root).as_posix())
        except (ValueError, OSError):
            pass
    return registered_streams, registered_by_name, registered_names_by_video


def _nfo_mismatch_result(ctx: _AuditContext, nfo, source: dict, stored_overrides: dict, video_id):
    """Compare the stored NFO with a freshly rendered one; return (kind, evidence, details)."""
    effective = ctx.resolve_metadata(source, stored_overrides)
    nfo_video = {**source, **effective,
                 "dateadded": ctx.services.format_dateadded(source.get("first_seen"))}
    categories = {str(category[0]) for category in ctx.conn.execute(
        "SELECT category FROM video_categories WHERE video_id=%s ORDER BY category",
        (video_id,),
    ).fetchall() if category[0]}
    tags = set(effective.get("tags") or [])
    expected_nfo = _expected_nfo_root(ctx.services, nfo_video, categories, tags)
    mismatched_fields = _core._audit_nfo_metadata_differences(nfo, expected_nfo)
    if mismatched_fields:
        details = _core._audit_nfo_metadata_diff_details(nfo, expected_nfo)
        evidence = ("NFO metadata differs from current effective catalog metadata "
                    f"(fields: {', '.join(mismatched_fields)}).")
        return "nfo_metadata_mismatch", evidence, details
    return None, "", {}


def _expected_nfo_root(services, nfo_video: dict, categories: set, tags: set):
    fd, expected_path = tempfile.mkstemp(prefix="adult-hub-audit-", suffix=".nfo")
    os.close(fd)
    try:
        expected_path = Path(expected_path)
        services.create_nfo_file(nfo_video, categories, tags, expected_path)
        expected_nfo = ET.parse(expected_path).getroot()
    finally:
        try:
            expected_path.unlink()
        except OSError:
            pass
    return expected_nfo


def _classify_nfo(ctx: _AuditContext, stream_path: Path, source: dict, stored_overrides: dict, video_id):
    nfo_path = stream_path.with_suffix(".nfo")
    try:
        nfo = ET.parse(nfo_path).getroot()
    except (ET.ParseError, OSError):
        return "invalid_nfo", "The NFO sidecar is not readable XML.", {}
    return _nfo_mismatch_result(ctx, nfo, source, stored_overrides, video_id)


def _classify_stream(ctx: _AuditContext, stream_path: Path, source: dict, stored_overrides: dict, video_id):
    kind, evidence, details = None, "", {}
    if stream_path.is_symlink() or not stream_path.resolve().is_relative_to(ctx.media_dir.resolve()):
        kind, evidence = "unknown_mapping", "The registered file path could not be safely mapped inside the library."
    elif not stream_path.is_file():
        kind, evidence = "missing_stream", "The registered stream file is missing."
    elif not stream_path.with_suffix(".nfo").is_file():
        kind, evidence = "missing_nfo", "The stream file exists, but its NFO sidecar is missing."
    else:
        return _classify_nfo(ctx, stream_path, source, stored_overrides, video_id)
    return kind, evidence, details


def _record_missing_artwork_if_needed(ctx: _AuditContext, video_id, thumbnail_url, stream_path: Path) -> None:
    artwork_path = stream_path.with_suffix(".jpg")
    if (thumbnail_url and stream_path.is_file() and not stream_path.is_symlink()
            and stream_path.resolve().is_relative_to(ctx.media_dir.resolve())
            and not stream_path.is_symlink()
            and (artwork_path.is_symlink() or not artwork_path.is_file())):
        with ctx.conn:
            ctx.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details,checked_at) "
                "VALUES (%s,%s,'missing_artwork','open',%s,%s,to_timestamp(%s))",
                (ctx.job_id, video_id, "The catalog has a thumbnail source, but its local artwork file is missing.",
                 Jsonb({"fields": ["thumbnail"]}), int(time.time())),
            )
        ctx.findings += 1


def _audit_video_file(ctx: _AuditContext, stored_path, video_id, source: dict, overrides, thumbnail_url) -> tuple:
    kind, evidence, details = None, "", {}
    stored_overrides = _parse_stored_overrides(overrides)
    effective_metadata = ctx.resolve_metadata(source, stored_overrides)
    missing_fields = _missing_metadata_fields(effective_metadata, source) if ctx.mode == "full" else []
    if missing_fields and str(video_id) not in ctx.missing_metadata_seen:
        _record_missing_metadata(ctx.conn, ctx.job_id, video_id, missing_fields)
        ctx.findings += 1
        ctx.missing_metadata_seen.add(str(video_id))
    try:
        stream_path = ctx.services.resolve_media_path(stored_path, ctx.media_dir)
        kind, evidence, details = _classify_stream(ctx, stream_path, source, stored_overrides, video_id)
        _record_missing_artwork_if_needed(ctx, video_id, thumbnail_url, stream_path)
    except ValueError:
        kind, evidence = "unknown_mapping", "The registered file path could not be safely mapped inside the library."
    except OSError:
        kind, evidence = "missing_stream", "The registered stream file is not available at its expected safe library location."
    return kind, evidence, details


def _audit_registered_row(ctx: _AuditContext, index: int, row, total: int) -> None:
    stored_path, video_id = row[0], row[1]
    column_count = len(ctx.video_columns)
    source = dict(zip(ctx.video_columns, row[2:2 + column_count]))
    has_video = bool(source.get("id"))
    overrides = row[2 + column_count]
    thumbnail_url = source.get("thumbnail")
    _core._record_job_progress(ctx.conn, ctx.job_id, "auditing_library", completed=index - 1, total=total)
    kind, evidence, details = None, "", {}
    if not has_video:
        kind, evidence = "catalog_reference_without_video", "A registered library file refers to a missing catalog item."
    else:
        kind, evidence, details = _audit_video_file(ctx, stored_path, video_id, source, overrides, thumbnail_url)
    ctx.checked += 1
    if kind:
        with ctx.conn:
            ctx.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details,checked_at) "
                "VALUES (%s,%s,%s,'open',%s,%s,to_timestamp(%s))",
                (ctx.job_id, video_id, kind, evidence, Jsonb(details), int(time.time())),
            )
        ctx.findings += 1
    _core._record_job_progress(ctx.conn, ctx.job_id, "auditing_library", completed=index, total=total)


def _store_orphan_findings(ctx: _AuditContext, orphan_families) -> None:
    for family in orphan_families:
        with ctx.conn:
            ctx.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details,checked_at) "
                "VALUES (%s,NULL,'orphan_file','open',%s,%s,to_timestamp(%s))",
                (ctx.job_id, "Unregistered STRM, NFO, or artwork files share this basename family.",
                 Jsonb(family), int(time.time())),
            )
        ctx.findings += 1


def _read_jellyfin_import_findings(ctx: _AuditContext) -> tuple:
    services = ctx.services
    try:
        _core._record_job_progress(ctx.conn, ctx.job_id, "checking_jellyfin_import_state", completed=0)
        audit = services.audit_jellyfin_import_state(
            ctx.media_dir,
            lambda completed, total: _core._record_job_progress(
                ctx.conn, ctx.job_id, "checking_jellyfin_import_state", completed=completed, total=total
            ),
        )
        audit_findings = audit.get("findings", []) if isinstance(audit, dict) else None
        if (not isinstance(audit, dict) or audit.get("status") not in ("checked", "unknown")
                or not isinstance(audit_findings, list)):
            audit = {"status": "unknown", "findings": []}
        jellyfin_state = audit["status"]
        if jellyfin_state == "checked":
            safe_findings = [(filename, kind) for filename, kind in audit_findings
                             if isinstance(filename, str) and kind in
                             ("nfo_import_state_missing", "nfo_import_state_mismatch")]
        else:
            safe_findings = []
    except Exception as exc:
        # Optional remote state checks must not turn a safe local audit into
        # a failed job or expose exception text in the durable report.
        services.logger.warning("Consistency audit could not compare Jellyfin import state (%s)",
                                type(exc).__name__)
        return "unknown", []
    return jellyfin_state, safe_findings


def _store_jellyfin_import_findings(ctx: _AuditContext, safe_findings: list, registered_by_name: dict,
                                    jellyfin_state: str) -> str:
    for filename, kind in safe_findings:
        matches = registered_by_name.get(filename, [])
        # Never attach Jellyfin evidence to an ambiguous local mapping.
        if len(matches) != 1:
            jellyfin_state = "partial"
            continue
        with ctx.conn:
            ctx.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,checked_at) "
                "VALUES (%s,%s,%s,'open',%s,to_timestamp(%s))",
                (ctx.job_id, matches[0], kind,
                 "The NFO checksum does not match the recorded Jellyfin import state."
                 if kind == "nfo_import_state_mismatch"
                 else "The NFO has no recorded successful Jellyfin import checksum.", int(time.time())),
            )
        ctx.findings += 1
    return jellyfin_state


def _resolve_verified_import_findings(ctx: _AuditContext, safe_findings: list, registered_by_name: dict,
                                      registered_names_by_video: dict) -> None:
    # A later complete import-state check closes older findings only
    # for videos whose every registered basename maps unambiguously.
    reported_video_ids = {str(registered_by_name[filename][0]) for filename, _kind in safe_findings
                          if len(registered_by_name.get(filename, [])) == 1}
    verified_video_ids = {
        video_id for video_id, names in registered_names_by_video.items()
        if names and all(len(registered_by_name.get(name, [])) == 1 for name in names)
    }
    resolved_video_ids = sorted(verified_video_ids - reported_video_ids)
    if resolved_video_ids:
        with ctx.conn:
            ctx.conn.execute(
                "UPDATE admin_audit_items SET status='resolved' WHERE job_id<>%s "
                "AND video_id=ANY(%s) AND kind IN ('nfo_import_state_missing','nfo_import_state_mismatch') "
                "AND status='open'",
                (ctx.job_id, resolved_video_ids),
            )


def _audit_jellyfin_imports(ctx: _AuditContext, registered_by_name: dict, registered_names_by_video: dict) -> str:
    jellyfin_state = "unknown"
    if ctx.services.audit_jellyfin_import_state is not None:
        jellyfin_state, safe_findings = _read_jellyfin_import_findings(ctx)
        jellyfin_state = _store_jellyfin_import_findings(ctx, safe_findings, registered_by_name, jellyfin_state)
        if jellyfin_state in {"checked", "partial"}:
            _resolve_verified_import_findings(ctx, safe_findings, registered_by_name, registered_names_by_video)
    return jellyfin_state


def _resolve_stale_quality_findings(ctx: _AuditContext) -> None:
    # A complete pass resolves old catalog-quality findings that were not
    # reproduced. Jellyfin import state is independent and may be unknown.
    with ctx.conn:
        ctx.conn.execute(
            "UPDATE admin_audit_items old SET status='resolved' WHERE old.job_id<>%s "
            "AND old.status='open' AND old.video_id IS NOT NULL "
            "AND old.kind=ANY(%s) AND NOT EXISTS ("
            "SELECT 1 FROM admin_audit_items current WHERE current.job_id=%s "
            "AND current.video_id=old.video_id AND current.kind=old.kind AND current.status='open')",
            (ctx.job_id, ["missing_nfo", "invalid_nfo", "nfo_metadata_mismatch", "missing_artwork"]
             + (["missing_metadata"] if ctx.mode == "full" else []), ctx.job_id),
        )


def _consistency_audit(conn, job_id: str, media_dir: Path, services, payload: dict | None = None) -> dict:
    """Read-only audit of registered STRM/NFO files; never repairs or deletes."""
    payload = payload if isinstance(payload, dict) else {}
    mode = payload.get("mode", "full")
    if mode not in {"full", "file_consistency"}:
        raise ValueError("Unsupported consistency audit mode.")
    # A restarted worker reuses the same job ID. Drop its partial findings so
    # the durable report represents one complete pass, not duplicate chunks.
    with conn:
        conn.execute("DELETE FROM admin_audit_items WHERE job_id=%s", (job_id,))
    from common.video_metadata import resolve_metadata
    video_columns = db.table_columns(conn, "videos")
    rows = conn.execute(
        "SELECT sf.file_path,sf.video_id,v.*,o.fields FROM synced_files sf "
        "LEFT JOIN videos v ON v.id=sf.video_id "
        "LEFT JOIN video_metadata_overrides o ON o.video_id=v.id ORDER BY sf.file_path"
    ).fetchall()
    media_root = media_dir.resolve()
    registered_streams, registered_by_name, registered_names_by_video = _index_registered_rows(
        rows, media_dir, media_root, services)
    ctx = _AuditContext(conn, job_id, mode, media_dir, services, video_columns, resolve_metadata)
    for index, row in enumerate(rows, start=1):
        _audit_registered_row(ctx, index, row, len(rows))
    # Inspect only the configured library, with a hard entry/depth limit and no
    # symlink traversal. Store aggregate evidence so paths and titles stay private.
    orphan_families, inventory_entries, inventory_complete = _core._audit_library_inventory(media_dir, registered_streams)
    _store_orphan_findings(ctx, orphan_families)
    if not inventory_complete:
        services.logger.warning("Consistency audit inventory is partial (entry/depth limit or filesystem access)")
    jellyfin_state = _audit_jellyfin_imports(ctx, registered_by_name, registered_names_by_video)
    if inventory_complete:
        _resolve_stale_quality_findings(ctx)
    result = {"local_state": "audit_completed", "checked": ctx.checked, "total": len(rows),
              "audit_mode": mode,
              "findings": ctx.findings, "jellyfin_import_state": jellyfin_state,
              "library_inventory_status": "complete" if inventory_complete else "partial",
              "library_inventory_entries": inventory_entries}
    if isinstance(payload.get("audit_batch_id"), str):
        result["audit_batch_id"] = payload["audit_batch_id"]
    return result

def _audit_repair_nfo(conn, job_id: str, payload: dict, media_dir: Path, services) -> dict:
    """Compatibility facade for explicit local NFO and artwork repairs."""
    from sync.duplicate_job_audit_repairs import _audit_repair_nfo as implementation
    return implementation(conn, job_id, payload, media_dir, services)


def _audit_import_refresh(conn, job_id: str, payload: dict, services) -> dict:
    """Compatibility facade for Jellyfin audit import refresh requests."""
    from sync.duplicate_job_audit_repairs import _audit_import_refresh as implementation
    return implementation(conn, job_id, payload, services)


def _orphan_file_operation(conn, job_id: str, payload: dict, media_dir: Path, quarantine_root: Path,
                           services, moved: list[tuple[Path, Path]]) -> dict:
    """Compatibility facade for :mod:`duplicate_job_orphan_files`."""
    from sync.duplicate_job_orphan_files import _orphan_file_operation as implementation
    return implementation(conn, job_id, payload, media_dir, quarantine_root, services, moved)

