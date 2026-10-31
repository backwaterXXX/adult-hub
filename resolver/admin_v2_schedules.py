"""Authorized API for durable maintenance schedules."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Request, Query
from admin_v2_errors import V2ErrorRoute
from common import admin_schedules
from common import schedule_stages
from common import system_schedules


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    def title_cleanup_status():
        path = Path(os.getenv(
            "TELEGRAM_TITLE_CLEANUP_STATUS_PATH",
            "/data/cache/telegram-title-cleanup.json",
        ))
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    @router.get("/api/v2/schedules")
    async def list_schedules(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            items = await admin.asyncio.to_thread(admin_schedules.list_schedules, admin.HUB_DB_URL)
            configured = await admin.asyncio.to_thread(system_schedules.list_system_schedules, admin.HUB_DB_URL)
            system = []
            library_sync_running = False
            for item in configured:
                status = "unknown"
                details = "Independent system task."
                task_description = None
                problem = bool(item["needs_conversion"])
                if item["id"] == "library-sync":
                    try:
                        from admin_v2_status import _sanitized_sync_status, _sync_stale_after_seconds
                        raw_status = await admin.asyncio.to_thread(admin._sync_status)
                        safe = _sanitized_sync_status(
                            raw_status, await admin.asyncio.to_thread(
                                _sync_stale_after_seconds, admin.CONFIG_PATH, admin.HUB_DB_URL
                            )
                        )
                        library_sync_running = bool(safe.get("running"))
                        status = "running" if safe.get("running") else safe.get("freshness", "unknown")
                        details = safe.get("status_text", "Sync status unavailable")
                        problem = problem or safe.get("last_success") is False or safe.get("freshness") == "stale"
                    except Exception:
                        details = "Sync status unavailable."
                elif item["id"] == "artwork-generation":
                    task_description = (
                        "Queues artwork generation for up to 100 recent items per run when artwork is "
                        "missing, black or matches a known studio logo. Each job rechecks before generation."
                    )
                    status = item.get("last_result") or "unknown"
                    details = f"Last result: {item['last_result']}." if item.get("last_result") else ""
                    if item.get("last_result"):
                        problem = problem or item["last_result"] == "failed"
                elif item.get("last_result"):
                    status = item["last_result"]
                    details = f"Last result: {item['last_result']}"
                    problem = problem or item["last_result"] == "failed"
                if item["needs_conversion"]:
                    details = "Legacy interval cannot be represented exactly; enter a cron expression to convert it."
                system.append({**item, "group": "system", "source": "System worker",
                               "task_description": task_description,
                               "details": details, "status": status, "problem": problem})
            stages = await admin.asyncio.to_thread(schedule_stages.list_stage_runs, admin.HUB_DB_URL)
            cleanup = await admin.asyncio.to_thread(title_cleanup_status)
            cleanup_status = str(cleanup.get("status") or "unknown")
            cleanup_result = str(cleanup.get("last_result") or "")
            cleanup_orphaned_run = cleanup_status == "running" and not library_sync_running
            if cleanup_orphaned_run:
                cleanup_status = "failed"
                cleanup_result = "failed"
            cleanup_display_status = (
                "running" if cleanup_status == "running" else
                "failed" if cleanup_status == "failed" else
                "done" if cleanup_status == "complete" else
                "pending" if cleanup_status == "pending" else "unknown"
            )
            cleanup_progress = []
            if cleanup.get("total") is not None:
                cleanup_progress.append(
                    f"{int(cleanup.get('processed') or 0)}/{int(cleanup.get('total') or 0)} scanned; "
                    f"{int(cleanup.get('titles_changed') or 0)} titles cleaned; "
                    f"{int(cleanup.get('tags_added') or 0)} tags added; "
                    f"{int(cleanup.get('categories_added') or 0)} categories added"
                )
            cleanup_description = "Up to 500 stored Telegram videos per library sync."
            if cleanup.get("nfos_missing"):
                cleanup_progress.append(f"{int(cleanup['nfos_missing'])} NFO files were missing")
            if cleanup.get("nfos_failed"):
                cleanup_progress.append(f"{int(cleanup['nfos_failed'])} NFO files could not be updated")
            if cleanup_status == "pending":
                cleanup_progress.append("It resumes with the next library sync")
            cleanup_progress_details = ". ".join(cleanup_progress)
            if cleanup_progress_details:
                cleanup_progress_details += "."
            cleanup_error = str(cleanup.get("last_error") or "")[:120]
            if cleanup_orphaned_run and not cleanup_error:
                cleanup_error = "Sync stopped before the batch checkpoint."
            for stage in stages:
                parent = stage["schedule_id"]
                row = {
                    **stage,
                    "group": "dependent",
                    "parent_id": parent,
                    "source": "System database backup" if parent == "database-backup" else "Library sync",
                    "status": stage["last_result"] or "unknown",
                    "problem": stage["last_result"] == "failed",
                }
                if stage["id"] == "telegram-title-cleanup":
                    row["task_description"] = cleanup_description
                    row["details"] = cleanup_description
                    row["progress_details"] = cleanup_progress_details
                    row["progress"] = {
                        "processed": cleanup.get("processed", 0),
                        "total": cleanup.get("total"),
                        "titles_changed": cleanup.get("titles_changed", 0),
                        "tags_added": cleanup.get("tags_added", 0),
                        "categories_added": cleanup.get("categories_added", 0),
                    }
                    # Installations upgraded before the first recorded pass still have
                    # the batch file but no stage row, so the file fills in for it.
                    if stage["last_result"] is None:
                        row["status"] = cleanup_display_status
                        row["last_result"] = cleanup_result or None
                        row["last_run_at"] = cleanup.get("last_run_at")
                    row["error"] = stage["last_error"] or cleanup_error
                    row["problem"] = bool(
                        row["problem"] or cleanup_status == "failed"
                        or int(cleanup.get("nfos_failed") or 0) > 0
                    )
                system.append(row)
            return {"ok": True, "items": items, "system_items": system}
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Schedule list is unavailable."}, status_code=503)

    @router.get("/api/v2/schedules/cron-preview")
    async def preview_cron(request: Request, expression: str = Query(..., max_length=100)):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            def calculate():
                instant = admin_schedules._utc()
                result = []
                for _ in range(3):
                    instant = admin_schedules.next_cron(expression, after=instant)
                    result.append(instant.isoformat())
                return result
            return {"ok": True, "timezone": admin_schedules.LOCAL_ZONE.key,
                    "next_runs": await admin.asyncio.to_thread(calculate)}
        except ValueError as exc:
            return admin.JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.post("/api/v2/schedules")
    async def create_schedule(request: Request):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {"name", "action", "schedule_type", "cron_expression", "interval_minutes", "daily_at", "timezone", "video_ids"}:
                raise ValueError("Invalid schedule fields.")
            if body.get("timezone", admin_schedules.LOCAL_ZONE.key) != admin_schedules.LOCAL_ZONE.key:
                raise ValueError(f"Schedules use {admin_schedules.LOCAL_ZONE.key}.")
            if body.get("action") != "availability_check" and "video_ids" in body:
                raise ValueError("Only availability schedules accept video IDs.")
            payload = {"video_ids": body.get("video_ids")} if body.get("action") == "availability_check" else {}
            create_fields = {
                "name": body.get("name"), "action": body.get("action"),
                "schedule_type": body.get("schedule_type"),
                "interval_minutes": body.get("interval_minutes"),
                "daily_at": body.get("daily_at"), "payload": payload,
            }
            if "cron_expression" in body:
                create_fields["cron_expression"] = body["cron_expression"]
            result = await admin.asyncio.to_thread(
                admin_schedules.create_schedule, admin.HUB_DB_URL,
                **create_fields,
            )
            return admin.JSONResponse({"ok": True, **result}, status_code=201)
        except ValueError as exc:
            return admin.JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not create schedule."}, status_code=503)

    @router.post("/api/v2/schedules/{schedule_id}/enabled")
    async def set_schedule_enabled(request: Request, schedule_id: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"enabled", "revision"}:
                raise ValueError("Provide enabled and revision.")
            result = await admin.asyncio.to_thread(
                admin_schedules.set_enabled, admin.HUB_DB_URL, schedule_id,
                enabled=body["enabled"], revision=body["revision"],
            )
            if result["outcome"] == "not_found":
                return admin.JSONResponse({"ok": False, "error": "Schedule not found."}, status_code=404)
            if result["outcome"] == "conflict":
                return admin.JSONResponse({"ok": False, "error": "Schedule changed. Refresh and try again.", **result}, status_code=409)
            return {"ok": True, **result}
        except ValueError as exc:
            return admin.JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not update schedule."}, status_code=503)

    @router.post("/api/v2/schedules/{schedule_id}/cron")
    async def set_schedule_cron(request: Request, schedule_id: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"cron_expression", "revision"}:
                raise ValueError("Provide cron_expression and revision.")
            result = await admin.asyncio.to_thread(
                admin_schedules.set_schedule_cron, admin.HUB_DB_URL, schedule_id,
                expression=body['cron_expression'], revision=body['revision'],
            )
            if result['outcome'] == 'not_found':
                return admin.JSONResponse({"ok": False, "error": "Schedule not found."}, status_code=404)
            if result['outcome'] == 'conflict':
                return admin.JSONResponse({"ok": False, "error": "Schedule changed. Refresh and try again.", **result}, status_code=409)
            return {"ok": True, **result}
        except ValueError as exc:
            return admin.JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not update cron schedule."}, status_code=503)

    @router.post("/api/v2/schedules/system/{schedule_id}")
    async def update_system_schedule(request: Request, schedule_id: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"cron_expression", "enabled", "revision"}:
                raise ValueError("Provide cron_expression, enabled and revision.")
            result = await admin.asyncio.to_thread(
                system_schedules.set_system_schedule, admin.HUB_DB_URL, schedule_id,
                expression=body["cron_expression"], enabled=body["enabled"], revision=body["revision"],
            )
            if result["outcome"] == "not_found":
                return admin.JSONResponse({"ok": False, "error": "System schedule not found."}, status_code=404)
            if result["outcome"] == "conflict":
                return admin.JSONResponse({"ok": False, "error": "Schedule changed. Refresh and try again.", **result}, status_code=409)
            return {"ok": True, **result}
        except ValueError as exc:
            return admin.JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not update system schedule."}, status_code=503)

    async def queue_run(request: Request, schedule_id: str, *, system: bool):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            run = system_schedules.request_system_run if system else admin_schedules.run_schedule_now
            result = await admin.asyncio.to_thread(run, admin.HUB_DB_URL, schedule_id)
            if result["outcome"] == "not_found":
                return admin.JSONResponse({"ok": False, "error": "Schedule not found."}, status_code=404)
            if result["outcome"] == "conflict":
                return admin.JSONResponse({"ok": False, "error": "A run is already queued or processing."}, status_code=409)
            if not system:
                admin._wake_worker("jobs")
            return admin.JSONResponse({"ok": True, **result}, status_code=202)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not run schedule."}, status_code=503)

    @router.post("/api/v2/schedules/{schedule_id}/run")
    async def run_admin_schedule(request: Request, schedule_id: str):
        return await queue_run(request, schedule_id, system=False)

    @router.post("/api/v2/schedules/system/{schedule_id}/run")
    async def run_system_schedule(request: Request, schedule_id: str):
        return await queue_run(request, schedule_id, system=True)

    @router.post("/api/v2/schedules/system/{schedule_id}/stages/{stage_id}/enabled")
    async def set_stage_enabled(request: Request, schedule_id: str, stage_id: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"enabled", "revision"}:
                raise ValueError("Provide enabled and revision.")
            result = await admin.asyncio.to_thread(
                schedule_stages.set_stage_enabled, admin.HUB_DB_URL, schedule_id, stage_id,
                enabled=body["enabled"], revision=body["revision"],
            )
            if result["outcome"] == "not_found":
                return admin.JSONResponse({"ok": False, "error": "Stage not found."}, status_code=404)
            if result["outcome"] == "conflict":
                return admin.JSONResponse({"ok": False, "error": "Schedule changed. Refresh and try again.",
                                           **result}, status_code=409)
            return {"ok": True, **result}
        except ValueError as exc:
            return admin.JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not update stage."}, status_code=503)

    @router.post("/api/v2/schedules/system/{schedule_id}/stages/{stage_id}/run")
    async def run_stage(request: Request, schedule_id: str, stage_id: str):
        denied = admin._write_denied(request)
        if denied is not None:
            return denied
        try:
            result = await admin.asyncio.to_thread(
                schedule_stages.request_stage_run, admin.HUB_DB_URL, schedule_id, stage_id
            )
        except ValueError as exc:
            return admin.JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        except Exception:
            return admin.JSONResponse({"ok": False, "error": "Could not run stage."}, status_code=503)
        if result["outcome"] == "not_found":
            return admin.JSONResponse({"ok": False, "error": "Stage not found."}, status_code=404)
        if result["outcome"] == "disabled":
            return admin.JSONResponse({"ok": False, "error": "Stage is disabled."}, status_code=409)
        if result["outcome"] == "conflict":
            return admin.JSONResponse({"ok": False, "error": "A run is already queued or processing."},
                                       status_code=409)
        admin._wake_worker("jobs")
        return admin.JSONResponse({"ok": True, **result}, status_code=202)

    return router
