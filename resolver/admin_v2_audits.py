"""Read-only consistency audit execution and paginated findings."""

from __future__ import annotations

import json
import uuid
from pathlib import PurePosixPath
from datetime import datetime, timezone
from fastapi import APIRouter, Query, Request
from admin_v2_errors import V2ErrorRoute
from common import db


def _result(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _safe_import_state(result):
    value = result.get("jellyfin_import_state") if isinstance(result, dict) else None
    return value if isinstance(value, str) and value in {"checked", "partial", "unknown"} else None


def _safe_batch_id(value):
    return value if isinstance(value, str) and len(value) == 32 and all(
        char in "0123456789abcdef" for char in value
    ) else None


def _safe_finding_details(kind, value):
    if kind != "orphan_file":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict) or not isinstance(value.get("files"), list):
        return None
    files = []
    total_count = len(value["files"])
    for item in value["files"][:3]:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return None
        path = PurePosixPath(item["path"])
        suffix = str(item.get("suffix") or "").lower()
        if (path.is_absolute() or ".." in path.parts or "\\" in item["path"]
                or suffix not in {".strm", ".nfo", ".jpg"} or path.suffix.lower() != suffix
                or type(item.get("size")) is not int or item["size"] < 0):
            return None
        files.append({"path": item["path"], "suffix": suffix, "size": item["size"]})
    return {"files": files, "file_count": total_count,
            "total_bytes": value.get("total_bytes") if type(value.get("total_bytes")) is int and value["total_bytes"] >= 0
            else sum(file["size"] for file in files)}


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.post("/api/v2/audits/execute")
    async def execute(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        from admin_v2_operations import _read_operation_body
        body, error_status = await _read_operation_body(request)
        if error_status:
            message = "Request is too large." if error_status == 413 else "Choose a supported audit mode."
            return admin.JSONResponse({"error": message}, status_code=error_status)
        if set(body) - {"mode"}:
            return admin.JSONResponse({"error": "Choose a supported audit mode."}, status_code=400)
        mode = body.get("mode", "full")
        if mode not in {"metadata_quality", "file_consistency", "full"}:
            return admin.JSONResponse({"error": "Choose a supported audit mode."}, status_code=400)
        try:
            from common.job_queue import enqueue_job, enqueue_jobs_batch
            if mode == "full":
                batch_id = uuid.uuid4().hex
                job_ids = await admin.asyncio.to_thread(
                    enqueue_jobs_batch, admin.HUB_DB_URL,
                    [("metadata_quality_audit", {"audit_batch_id": batch_id}),
                     ("consistency_audit", {"mode": "file_consistency", "audit_batch_id": batch_id})],
                )
                # Keep the historical scalar response focused on the consistency report.
                job_id = job_ids[1]
            elif mode == "metadata_quality":
                job_id = await admin.asyncio.to_thread(
                    enqueue_job, admin.HUB_DB_URL, "metadata_quality_audit", {}
                )
                job_ids = [job_id]
            else:
                job_id = await admin.asyncio.to_thread(
                    enqueue_job, admin.HUB_DB_URL, "consistency_audit", {"mode": "file_consistency"}
                )
                job_ids = [job_id]
            admin._wake_worker("jobs")
            return admin.JSONResponse({"ok": True, "job_id": job_id, "job_ids": job_ids,
                                       "mode": mode, "state": "queued"}, status_code=202)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not queue the requested audit."}, status_code=503)

    @router.post("/api/v2/audits/{job_id}/repairs/preview")
    async def preview_nfo_repair(request: Request, job_id: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        from admin_v2_operations import _read_operation_body
        body, error_status = await _read_operation_body(request)
        if error_status:
            message = "Request is too large." if error_status == 413 else "Specify selected finding IDs."
            return admin.JSONResponse({"error": message}, status_code=error_status)
        if set(body) != {"finding_ids"}:
            return admin.JSONResponse({"error": "Specify selected finding IDs."}, status_code=400)
        try:
            from common.admin_operations import preview_audit_repair
            result = await admin.asyncio.to_thread(
                preview_audit_repair, admin.HUB_DB_URL, job_id, body.get("finding_ids")
            )
        except Exception:
            return admin.JSONResponse({"error": "Could not create the local file repair preview."}, status_code=503)
        if result["outcome"] == "invalid":
            return admin.JSONResponse({"error": result["message"]}, status_code=400)
        if result["outcome"] == "conflict":
            return admin.JSONResponse({"error": result["message"]}, status_code=409)
        return {"ok": True, **{key: value for key, value in result.items() if key != "outcome"}}

    @router.post("/api/v2/audits/{job_id}/reimports/preview")
    async def preview_import_refresh(request: Request, job_id: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        from admin_v2_operations import _read_operation_body
        body, error_status = await _read_operation_body(request)
        if error_status:
            message = "Request is too large." if error_status == 413 else "Specify selected finding IDs."
            return admin.JSONResponse({"error": message}, status_code=error_status)
        if set(body) != {"finding_ids"}:
            return admin.JSONResponse({"error": "Specify selected finding IDs."}, status_code=400)
        try:
            from common.admin_operations import preview_audit_import_refresh
            result = await admin.asyncio.to_thread(
                preview_audit_import_refresh, admin.HUB_DB_URL, job_id, body.get("finding_ids")
            )
        except Exception:
            return admin.JSONResponse({"error": "Could not create the Jellyfin refresh preview."}, status_code=503)
        if result["outcome"] == "invalid":
            return admin.JSONResponse({"error": result["message"]}, status_code=400)
        if result["outcome"] == "conflict":
            return admin.JSONResponse({"error": result["message"]}, status_code=409)
        return {"ok": True, **{key: value for key, value in result.items() if key != "outcome"}}

    @router.get("/api/v2/audits")
    async def list_audits(request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        conn = None
        try:
            conn = db.connect(admin.HUB_DB_URL)
            rows = conn.execute(
                "SELECT j.id,j.state,j.created_at,j.updated_at,j.result,j.action,"
                "(SELECT count(*) FROM admin_audit_items i WHERE i.job_id=j.id) "
                "FROM video_duplicate_jobs j WHERE j.action IN ('consistency_audit','metadata_quality_audit') "
                "ORDER BY j.created_at DESC,j.id DESC LIMIT %s OFFSET %s", (limit, offset)
            ).fetchall()
            total = conn.execute(
                "SELECT count(*) FROM video_duplicate_jobs WHERE action IN ('consistency_audit','metadata_quality_audit')"
            ).fetchone()[0]
            items = []
            for row in rows:
                result = _result(row[4])
                progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}
                import_state = _safe_import_state(result)
                progress_completed = progress.get("completed") if type(progress.get("completed")) is int and progress["completed"] >= 0 else None
                progress_total = progress.get("total") if type(progress.get("total")) is int and progress["total"] >= 0 else None
                scope = ("metadata_quality" if row[5] == "metadata_quality_audit" else result.get("audit_mode")
                         if result.get("audit_mode") in {"metadata_quality", "full", "file_consistency"}
                         else "combined")
                batch_id = _safe_batch_id(result.get("audit_batch_id"))
                mode = "full" if batch_id or scope == "combined" else scope
                items.append({"id": row[0], "state": row[1], "mode": mode, "scope": scope,
                              "audit_batch_id": batch_id,
                              "created_at": _iso(row[2]),
                              "updated_at": _iso(row[3]), "finding_count": int(row[6]),
                              "checked": result.get("checked") if type(result.get("checked")) is int and result["checked"] >= 0 else None,
                              "catalog_total": result.get("total") if type(result.get("total")) is int and result["total"] >= 0 else None,
                              "progress_completed": progress_completed, "progress_total": progress_total,
                              "jellyfin_import_state": import_state,
                              "library_inventory_status": result.get("library_inventory_status") if result.get("library_inventory_status") in {"complete", "partial"} else None,
                              "library_inventory_entries": result.get("library_inventory_entries") if isinstance(result.get("library_inventory_entries"), int) and result.get("library_inventory_entries") >= 0 else None,
                              "stage": progress.get("stage") if progress.get("stage") in {"queued", "starting", "auditing_library", "auditing_metadata", "checking_jellyfin_import_state", "completed", "failed"} else row[1]})
            return {"ok": True, "items": items, "total": int(total), "offset": offset, "limit": limit}
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Consistency audit history is unavailable."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()

    @router.get("/api/v2/audits/{job_id}")
    async def audit_detail(request: Request, job_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        conn = None
        try:
            conn = db.connect(admin.HUB_DB_URL)
            job = conn.execute(
                "SELECT id,state,result,action FROM video_duplicate_jobs WHERE id=%s "
                "AND action IN ('consistency_audit','metadata_quality_audit')", (job_id,)
            ).fetchone()
            if job is None:
                return admin.JSONResponse({"ok": False, "error": "Audit report was not found."}, status_code=404)
            result = _result(job[2])
            progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}
            scope = "metadata_quality" if job[3] == "metadata_quality_audit" else (
                result.get("audit_mode") if result.get("audit_mode") in {"full", "file_consistency"}
                else "combined")
            batch_id = _safe_batch_id(result.get("audit_batch_id"))
            mode = "full" if batch_id or scope == "combined" else scope
            rows = conn.execute(
                "SELECT id,video_id,kind,status,evidence,details,checked_at FROM admin_audit_items "
                "WHERE job_id=%s ORDER BY id LIMIT %s OFFSET %s", (job_id, limit, offset)
            ).fetchall()
            total = conn.execute("SELECT count(*) FROM admin_audit_items WHERE job_id=%s", (job_id,)).fetchone()[0]
            import_state = _safe_import_state(result)
            return {"ok": True, "audit": {"id": job[0], "state": job[1], "mode": mode, "scope": scope,
                    "audit_batch_id": batch_id,
                    "checked": result.get("checked") if type(result.get("checked")) is int and result["checked"] >= 0 else None,
                    "catalog_total": result.get("total") if type(result.get("total")) is int and result["total"] >= 0 else None,
                    "progress_completed": progress.get("completed") if type(progress.get("completed")) is int and progress["completed"] >= 0 else None,
                    "progress_total": progress.get("total") if type(progress.get("total")) is int and progress["total"] >= 0 else None,
                    "jellyfin_import_state": import_state,
                    "library_inventory_status": result.get("library_inventory_status") if result.get("library_inventory_status") in {"complete", "partial"} else None,
                    "library_inventory_entries": result.get("library_inventory_entries") if isinstance(result.get("library_inventory_entries"), int) and result.get("library_inventory_entries") >= 0 else None,
                    "findings": int(total)}, "items": [{"id": int(row[0]), "video_id": row[1], "kind": row[2],
                    "status": row[3], "evidence": row[4], "details": _safe_finding_details(row[2], row[5]),
                    "checked_at": _iso(row[6])}
                    for row in rows], "total": int(total), "offset": offset, "limit": limit}
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Consistency audit report is unavailable."}, status_code=503)
        finally:
            if conn is not None:
                conn.close()

    return router
