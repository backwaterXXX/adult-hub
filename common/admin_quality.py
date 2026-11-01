"""Read-only projection used by the Admin v2 catalog quality center."""

from __future__ import annotations

import json
import re

from common import db
from common.admin_v2_quality import QUALITY_FIELDS, QUALITY_KINDS

QUALITY_HISTORY_LIMIT = 8


def _object(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _audit_batch_id(value):
    return value if isinstance(value, str) and len(value) == 32 and all(
        char in "0123456789abcdef" for char in value
    ) else None


def _complete_audit(conn):
    rows = conn.execute(
        "SELECT id,state,created_at,result,action,payload FROM video_duplicate_jobs "
        "WHERE action IN ('consistency_audit','metadata_quality_audit') ORDER BY created_at DESC,id DESC"
    ).fetchall()
    parsed = []
    for row in rows:
        result = _object(row[3])
        payload = _object(row[5])
        batch_id = _audit_batch_id(result.get("audit_batch_id")) or _audit_batch_id(payload.get("audit_batch_id"))
        if batch_id:
            result["audit_batch_id"] = batch_id
        else:
            result.pop("audit_batch_id", None)
        if row[4] == "consistency_audit" and "audit_mode" not in result:
            mode = payload.get("mode")
            if mode in {"full", "file_consistency"}:
                result["audit_mode"] = mode
        parsed.append((row, result))
    consistency_by_batch = {
        result.get("audit_batch_id"): (row, result)
        for row, result in parsed
        if row[4] == "consistency_audit" and _audit_batch_id(result.get("audit_batch_id"))
    }
    metadata_by_batch = {
        result.get("audit_batch_id"): row
        for row, result in parsed
        if row[4] == "metadata_quality_audit" and _audit_batch_id(result.get("audit_batch_id"))
    }
    current = rows[0] if rows else None
    if parsed:
        newest_row, newest_result = parsed[0]
        batch_id = newest_result.get("audit_batch_id")
        if _audit_batch_id(batch_id) and batch_id in metadata_by_batch:
            metadata_row = metadata_by_batch[batch_id]
            batch_rows = [row for row, result in parsed if result.get("audit_batch_id") == batch_id]
            states = [str(row[1]) for row in batch_rows]
            if "failed" in states:
                combined_state = "failed"
            elif "processing" in states:
                combined_state = "processing"
            elif "queued" in states:
                combined_state = "queued"
            else:
                combined_state = "done"
            current = (metadata_row[0], combined_state,
                       max((row[2] for row in batch_rows if row[2] is not None), default=metadata_row[2]),
                       _object(metadata_row[3]), metadata_row[4])
    complete = []
    for row, result in parsed:
        if row[1] != "done":
            continue
        mode = result.get("audit_mode")
        checked = result.get("checked")
        total = result.get("total")
        catalog_complete = (type(checked) is int and type(total) is int
                            and checked >= 0 and total >= 0 and checked == total)
        if row[4] == "metadata_quality_audit":
            if not catalog_complete:
                continue
            batch_id = result.get("audit_batch_id")
            if isinstance(batch_id, str):
                sibling = consistency_by_batch.get(batch_id)
                if sibling is None:
                    continue
                file_row, file_result = sibling
                file_complete = (file_row[1] == "done"
                                 and type(file_result.get("checked")) is int
                                 and type(file_result.get("total")) is int
                                 and file_result["checked"] == file_result["total"]
                                 and file_result.get("library_inventory_status") in {"complete", "partial"})
                if not file_complete:
                    continue
                combined = {**file_result, **result, "audit_mode": "full",
                            "file_checked": file_result["checked"],
                            "file_total": file_result["total"],
                            "library_inventory_status": file_result["library_inventory_status"],
                            "library_inventory_entries": file_result.get("library_inventory_entries"),
                            "jellyfin_import_state": file_result.get("jellyfin_import_state"),
                            "audit_batch_id": batch_id}
                complete.append((str(row[0]), row[2], combined,
                                 [str(row[0]), str(file_row[0])]))
            else:
                complete.append((str(row[0]), row[2], {**result, "audit_mode": "metadata_quality"},
                                 [str(row[0])]))
        elif mode == "file_consistency":
            # A file-only scan is not a complete metadata quality snapshot. Paired
            # full audits are combined when visiting their metadata job above.
            continue
        elif (catalog_complete
              and result.get("library_inventory_status") in {"complete", "partial"}):
            # Historical jobs without a mode performed the original combined audit.
            complete.append((str(row[0]), row[2], {**result, "audit_mode": "full"}, [str(row[0])]))
    complete.sort(key=lambda item: (item[1] or 0, item[0]), reverse=True)
    if complete:
        selected_mode = complete[0][2]["audit_mode"]
        complete = [entry for entry in complete if entry[2]["audit_mode"] == selected_mode][:QUALITY_HISTORY_LIMIT]
    return (complete[0] if complete else None), current, complete


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value if isinstance(value, str) else None


def _fingerprint_coverage(conn):
    total = int(conn.execute("SELECT count(*) FROM videos").fetchone()[0])
    if not db.table_exists(conn, "video_fingerprints"):
        return {"available": False, "total_videos": total, "oshash": 0, "phash": 0}
    row = conn.execute(
        "SELECT count(*) FILTER (WHERE EXISTS (SELECT 1 FROM video_fingerprints f "
        "WHERE f.video_id=v.id AND f.algorithm='OSHASH')) AS oshash, "
        "count(*) FILTER (WHERE EXISTS (SELECT 1 FROM video_fingerprints f "
        "WHERE f.video_id=v.id AND f.algorithm='PHASH')) AS phash "
        "FROM videos v"
    ).fetchone()
    return {"available": True, "total_videos": total,
            "oshash": int(row[0] or 0), "phash": int(row[1] or 0)}


def _safe_value(value):
    text = re.sub(r"https?://\S+", "[link]", str(value or ""), flags=re.IGNORECASE)
    text = "".join(char for char in text if char >= " " or char in "\t\n")
    return text[:300]


def read_quality_summary(db_url: str | None) -> dict:
    conn = db.connect(db_url)
    try:
        fingerprint_coverage = _fingerprint_coverage(conn)
        latest, current, complete = _complete_audit(conn)
        if latest is None:
            current_state = str(current[1]) if current else None
            status_reason = None
            if current is None:
                status_reason = "no_audit"
            else:
                current_result = _object(current[3])
                if current_state in {"queued", "processing"}:
                    status_reason = "audit_in_progress"
                elif (current[4] == "consistency_audit"
                        and current_result.get("audit_mode") == "file_consistency"):
                    status_reason = "file_consistency_only"
                elif (current_state == "done"
                        and current_result.get("library_inventory_status") == "complete"
                        and type(current_result.get("checked")) is int
                        and type(current_result.get("total")) is not int):
                    # Older completed audits did not persist the catalog total, so
                    # their coverage cannot be verified safely from today's catalog.
                    status_reason = "legacy_missing_catalog_total"
                else:
                    status_reason = "audit_not_complete"
            return {"available": True, "status": "no_complete_audit", "latest_audit": None,
                    "current_audit_id": str(current[0]) if current else None,
                    "current_audit_state": current_state, "status_reason": status_reason,
                    "counts": {}, "total": 0, "history": [],
                    "fingerprint_coverage": fingerprint_coverage}
        audit_id, created_at, result, audit_ids = latest
        all_history_job_ids = list(dict.fromkeys(
            job_id for audit in complete for job_id in audit[3]
        ))
        counts_by_audit = {
            audit[0]: dict.fromkeys(QUALITY_KINDS, 0)
            for audit in complete
        }
        primary_by_job = {
            job_id: audit[0]
            for audit in complete
            for job_id in audit[3]
        }
        for counted_audit_id, kind, count in conn.execute(
            "SELECT i.job_id,i.kind,count(*) FROM admin_audit_items i "
            "LEFT JOIN admin_quality_acknowledgements a ON i.job_id=ANY(%s) AND a.state='active' "
            "AND a.video_id=i.video_id AND a.kind=i.kind "
            "AND a.finding_fingerprint=md5(i.details::text) "
            "WHERE i.job_id=ANY(%s) AND i.status='open' AND i.kind=ANY(%s) AND a.id IS NULL "
            "GROUP BY i.job_id,i.kind", (audit_ids, all_history_job_ids, list(QUALITY_KINDS))
        ).fetchall():
            primary_id = primary_by_job.get(str(counted_audit_id))
            if primary_id is not None:
                counts_by_audit[primary_id][str(kind)] += int(count)
        missing_fields = dict.fromkeys(QUALITY_FIELDS, 0)
        for field, count in conn.execute(
            "SELECT f.field,count(*) FROM admin_audit_items i "
            "LEFT JOIN admin_quality_acknowledgements a ON i.job_id=ANY(%s) AND a.state='active' AND a.video_id=i.video_id "
            "AND a.kind=i.kind AND a.finding_fingerprint=md5(i.details::text) "
            "CROSS JOIN LATERAL jsonb_array_elements_text(i.details->'fields') AS f(field) "
            "WHERE i.job_id=ANY(%s) AND i.kind='missing_metadata' AND i.status='open' AND a.id IS NULL "
            "GROUP BY f.field", (audit_ids, audit_ids)
        ).fetchall():
            if str(field) in missing_fields:
                missing_fields[str(field)] = int(count)
        history = []
        for history_id, history_date, history_result, history_job_ids in complete:
            history_counts = counts_by_audit[history_id]
            history.append({"id": history_id, "job_ids": history_job_ids,
                            "mode": history_result.get("audit_mode"), "created_at": _iso(history_date),
                            "checked": history_result["checked"], "total": history_result["total"],
                            "library_inventory_status": history_result.get("library_inventory_status"),
                            "library_inventory_entries": (
                                history_result.get("library_inventory_entries")
                                if type(history_result.get("library_inventory_entries")) is int
                                and history_result["library_inventory_entries"] >= 0 else None
                            ),
                            "counts": history_counts, "open_total": sum(history_counts.values())})
        counts = counts_by_audit[audit_id]
        return {"available": True, "status": "ready", "latest_audit": {
            "id": audit_id, "job_ids": audit_ids, "mode": result.get("audit_mode"),
            "created_at": _iso(created_at), "checked": result["checked"],
            "total": result["total"], "library_inventory_status": result.get("library_inventory_status"),
            "library_inventory_entries": (
                result.get("library_inventory_entries")
                if type(result.get("library_inventory_entries")) is int
                and result["library_inventory_entries"] >= 0 else None
            ),
            "jellyfin_import_state": result.get("jellyfin_import_state")
            if result.get("jellyfin_import_state") in {"checked", "partial", "unknown"} else "unknown",
        }, "current_audit_id": str(current[0]) if current else None,
            "current_audit_state": str(current[1]) if current else None,
            "counts": counts, "missing_fields": missing_fields, "total": sum(counts.values()), "history": history,
            "fingerprint_coverage": fingerprint_coverage}
    finally:
        conn.close()
