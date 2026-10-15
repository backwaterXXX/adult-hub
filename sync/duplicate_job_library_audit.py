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

if __package__:
    from . import duplicate_jobs as _core
else:
    import duplicate_jobs as _core


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
        overrides = row[len(columns)]
        if isinstance(overrides, str):
            try:
                overrides = json.loads(overrides)
            except (TypeError, ValueError):
                overrides = {}
        effective = resolve_metadata(source, overrides if isinstance(overrides, dict) else {})
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
        if missing_fields:
            with conn:
                conn.execute(
                    "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details,checked_at) "
                    "VALUES (%s,%s,'missing_metadata','open',%s,%s,to_timestamp(%s))",
                    (job_id, source["id"], "Effective catalog metadata is missing one or more fields.",
                     Jsonb({"fields": missing_fields}), int(time.time())),
                )
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
    checked = findings = 0
    missing_metadata_seen: set[str] = set()
    for index, row in enumerate(rows, start=1):
        stored_path, video_id = row[0], row[1]
        source = dict(zip(video_columns, row[2:2 + len(video_columns)]))
        has_video = bool(source.get("id"))
        overrides = row[2 + len(video_columns)]
        thumbnail_url = source.get("thumbnail")
        _core._record_job_progress(conn, job_id, "auditing_library", completed=index - 1, total=len(rows))
        kind, evidence, details = None, "", {}
        if not has_video:
            kind, evidence = "catalog_reference_without_video", "A registered library file refers to a missing catalog item."
        else:
            from common.video_metadata import resolve_metadata
            if isinstance(overrides, str):
                try:
                    overrides = json.loads(overrides)
                except (TypeError, ValueError):
                    overrides = {}
            stored_overrides = overrides if isinstance(overrides, dict) else {}
            effective_metadata = resolve_metadata(source, stored_overrides)
            missing_fields = []
            if mode == "full":
                if not str(effective_metadata.get("title") or "").strip():
                    missing_fields.append("title")
                if not effective_metadata.get("performers"):
                    missing_fields.append("performers")
                if not str(effective_metadata.get("studio") or "").strip():
                    missing_fields.append("studio")
                if not str(effective_metadata.get("plot") or "").strip():
                    missing_fields.append("plot")
                if not str(source.get("thumbnail") or "").strip():
                    missing_fields.append("thumbnail")
            if missing_fields and str(video_id) not in missing_metadata_seen:
                with conn:
                    conn.execute(
                        "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details,checked_at) "
                        "VALUES (%s,%s,'missing_metadata','open',%s,%s,to_timestamp(%s))",
                        (job_id, video_id, "Effective catalog metadata is missing one or more fields.",
                         Jsonb({"fields": missing_fields}), int(time.time())),
                    )
                findings += 1
                missing_metadata_seen.add(str(video_id))
            try:
                stream_path = services.resolve_media_path(stored_path, media_dir)
                if stream_path.is_symlink() or not stream_path.resolve().is_relative_to(media_dir.resolve()):
                    kind, evidence = "unknown_mapping", "The registered file path could not be safely mapped inside the library."
                elif not stream_path.is_file():
                    kind, evidence = "missing_stream", "The registered stream file is missing."
                elif not stream_path.with_suffix(".nfo").is_file():
                    kind, evidence = "missing_nfo", "The stream file exists, but its NFO sidecar is missing."
                else:
                    nfo_path = stream_path.with_suffix(".nfo")
                    try:
                        nfo = ET.parse(nfo_path).getroot()
                    except (ET.ParseError, OSError):
                        kind, evidence = "invalid_nfo", "The NFO sidecar is not readable XML."
                    else:
                        effective = resolve_metadata(source, stored_overrides)
                        nfo_video = {**source, **effective,
                                     "dateadded": services.format_dateadded(source.get("first_seen"))}
                        categories = {str(category[0]) for category in conn.execute(
                            "SELECT category FROM video_categories WHERE video_id=%s ORDER BY category",
                            (video_id,),
                        ).fetchall() if category[0]}
                        tags = set(effective.get("tags") or [])
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
                        mismatched_fields = _core._audit_nfo_metadata_differences(nfo, expected_nfo)
                        if mismatched_fields:
                            kind = "nfo_metadata_mismatch"
                            details = _core._audit_nfo_metadata_diff_details(nfo, expected_nfo)
                            evidence = ("NFO metadata differs from current effective catalog metadata "
                                        f"(fields: {', '.join(mismatched_fields)}).")
                artwork_path = stream_path.with_suffix(".jpg")
                if (thumbnail_url and stream_path.is_file() and not stream_path.is_symlink()
                        and stream_path.resolve().is_relative_to(media_dir.resolve())
                        and not stream_path.is_symlink()
                        and (artwork_path.is_symlink() or not artwork_path.is_file())):
                    with conn:
                        conn.execute(
                            "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details,checked_at) "
                            "VALUES (%s,%s,'missing_artwork','open',%s,%s,to_timestamp(%s))",
                            (job_id, video_id, "The catalog has a thumbnail source, but its local artwork file is missing.",
                             Jsonb({"fields": ["thumbnail"]}), int(time.time())),
                        )
                    findings += 1
            except ValueError:
                kind, evidence = "unknown_mapping", "The registered file path could not be safely mapped inside the library."
            except OSError:
                kind, evidence = "missing_stream", "The registered stream file is not available at its expected safe library location."
        checked += 1
        if kind:
            with conn:
                conn.execute(
                    "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details,checked_at) "
                    "VALUES (%s,%s,%s,'open',%s,%s,to_timestamp(%s))",
                    (job_id, video_id, kind, evidence, Jsonb(details), int(time.time())),
                )
            findings += 1
        _core._record_job_progress(conn, job_id, "auditing_library", completed=index, total=len(rows))
    # Inspect only the configured library, with a hard entry/depth limit and no
    # symlink traversal. Store aggregate evidence so paths and titles stay private.
    orphan_families, inventory_entries, inventory_complete = _core._audit_library_inventory(media_dir, registered_streams)
    for family in orphan_families:
        with conn:
            conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details,checked_at) "
                "VALUES (%s,NULL,'orphan_file','open',%s,%s,to_timestamp(%s))",
                (job_id, "Unregistered STRM, NFO, or artwork files share this basename family.",
                 Jsonb(family), int(time.time())),
            )
        findings += 1
    if not inventory_complete:
        services.logger.warning("Consistency audit inventory is partial (entry/depth limit or filesystem access)")
    jellyfin_state = "unknown"
    if services.audit_jellyfin_import_state is not None:
        try:
            _core._record_job_progress(conn, job_id, "checking_jellyfin_import_state", completed=0)
            audit = services.audit_jellyfin_import_state(
                media_dir,
                lambda completed, total: _core._record_job_progress(
                    conn, job_id, "checking_jellyfin_import_state", completed=completed, total=total
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
            jellyfin_state = "unknown"
            safe_findings = []
        for filename, kind in safe_findings:
            matches = registered_by_name.get(filename, [])
            # Never attach Jellyfin evidence to an ambiguous local mapping.
            if len(matches) != 1:
                jellyfin_state = "partial"
                continue
            with conn:
                conn.execute(
                    "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,checked_at) "
                    "VALUES (%s,%s,%s,'open',%s,to_timestamp(%s))",
                    (job_id, matches[0], kind,
                     "The NFO checksum does not match the recorded Jellyfin import state."
                     if kind == "nfo_import_state_mismatch"
                     else "The NFO has no recorded successful Jellyfin import checksum.", int(time.time())),
                )
            findings += 1
        if jellyfin_state in {"checked", "partial"}:
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
                with conn:
                    conn.execute(
                        "UPDATE admin_audit_items SET status='resolved' WHERE job_id<>%s "
                        "AND video_id=ANY(%s) AND kind IN ('nfo_import_state_missing','nfo_import_state_mismatch') "
                        "AND status='open'",
                        (job_id, resolved_video_ids),
                    )
    if inventory_complete:
        # A complete pass resolves old catalog-quality findings that were not
        # reproduced. Jellyfin import state is independent and may be unknown.
        with conn:
            conn.execute(
                "UPDATE admin_audit_items old SET status='resolved' WHERE old.job_id<>%s "
                "AND old.status='open' AND old.video_id IS NOT NULL "
                "AND old.kind=ANY(%s) AND NOT EXISTS ("
                "SELECT 1 FROM admin_audit_items current WHERE current.job_id=%s "
                "AND current.video_id=old.video_id AND current.kind=old.kind AND current.status='open')",
                (job_id, ["missing_nfo", "invalid_nfo", "nfo_metadata_mismatch", "missing_artwork"]
                 + (["missing_metadata"] if mode == "full" else []), job_id),
            )
    result = {"local_state": "audit_completed", "checked": checked, "total": len(rows),
              "audit_mode": mode,
              "findings": findings, "jellyfin_import_state": jellyfin_state,
              "library_inventory_status": "complete" if inventory_complete else "partial",
              "library_inventory_entries": inventory_entries}
    if isinstance(payload.get("audit_batch_id"), str):
        result["audit_batch_id"] = payload["audit_batch_id"]
    return result

def _audit_repair_nfo(conn, job_id: str, payload: dict, media_dir: Path, services) -> dict:
    """Compatibility facade for explicit local NFO and artwork repairs."""
    if __package__:
        from .duplicate_job_audit_repairs import _audit_repair_nfo as implementation
    else:
        from duplicate_job_audit_repairs import _audit_repair_nfo as implementation
    return implementation(conn, job_id, payload, media_dir, services)


def _audit_import_refresh(conn, job_id: str, payload: dict, services) -> dict:
    """Compatibility facade for Jellyfin audit import refresh requests."""
    if __package__:
        from .duplicate_job_audit_repairs import _audit_import_refresh as implementation
    else:
        from duplicate_job_audit_repairs import _audit_import_refresh as implementation
    return implementation(conn, job_id, payload, services)


def _orphan_file_operation(conn, job_id: str, payload: dict, media_dir: Path, quarantine_root: Path,
                           services, moved: list[tuple[Path, Path]]) -> dict:
    """Compatibility facade for :mod:`duplicate_job_orphan_files`."""
    if __package__:
        from .duplicate_job_orphan_files import _orphan_file_operation as implementation
    else:
        from duplicate_job_orphan_files import _orphan_file_operation as implementation
    return implementation(conn, job_id, payload, media_dir, quarantine_root, services, moved)
