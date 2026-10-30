"""Authorized API for durable maintenance schedules."""

from __future__ import annotations

from fastapi import APIRouter, Request, Query
from admin_v2_errors import V2ErrorRoute
from common import admin_schedules
from common import system_schedules


def build_router(admin):
    router = APIRouter(route_class=V2ErrorRoute)

    @router.get("/api/v2/schedules")
    async def list_schedules(request: Request):
        if admin._admin_blocked(request):
            return admin.JSONResponse({"ok": False, "error": "Admin is only available locally."}, status_code=403)
        try:
            items = await admin.asyncio.to_thread(admin_schedules.list_schedules, admin.HUB_DB_URL)
            from common.config_loader import load_config
            try:
                config = await admin.asyncio.to_thread(load_config, str(admin.CONFIG_PATH))
                sync_daily_at, sync_interval_minutes = config.sync.daily_at, config.sync.interval_minutes
            except Exception:
                sync_daily_at, sync_interval_minutes = None, 60
            import os
            backup_daily_at = os.getenv("BACKUP_DAILY_AT", "04:30")
            await admin.asyncio.to_thread(
                system_schedules.ensure_system_schedules, admin.HUB_DB_URL,
                sync_daily_at=sync_daily_at, sync_interval_minutes=sync_interval_minutes,
                backup_daily_at=backup_daily_at,
            )
            configured = await admin.asyncio.to_thread(system_schedules.list_system_schedules, admin.HUB_DB_URL)
            system = []
            for item in configured:
                status = "unknown"
                details = "Independent system task."
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
                        status = "running" if safe.get("running") else safe.get("freshness", "unknown")
                        details = safe.get("status_text", "Sync status unavailable")
                        problem = problem or safe.get("last_success") is False or safe.get("freshness") == "stale"
                    except Exception:
                        details = "Sync status unavailable."
                elif item.get("last_result"):
                    status = item["last_result"]
                    details = f"Last result: {item['last_result']}"
                    problem = problem or item["last_result"] == "failed"
                if item["needs_conversion"]:
                    details = "Legacy interval cannot be represented exactly; enter a cron expression to convert it."
                system.append({**item, "group": "system", "source": "System worker",
                               "details": details, "status": status, "problem": problem})
            system.extend([
                {"name": "Backup history cleanup", "group": "dependent", "parent_id": "database-backup", "source": "System database backup", "details": "Runs after a successful system backup; removes history older than one calendar month.", "status": "inherited"},
                {"name": "StashDB performer catalog", "group": "dependent", "parent_id": "library-sync", "source": "Library sync", "details": "At sync, at most once per 24 hours; requires configured credentials.", "status": "unknown"},
                {"name": "Configured source scans", "group": "dependent", "parent_id": "library-sync", "source": "Library sync", "details": "Scans enabled sources; Telegram availability checks run after successful channel scans.", "status": "unknown"},
                {"name": "Telegram availability reconciliation", "group": "dependent", "parent_id": "library-sync", "source": "Library sync", "details": "Runs for eligible enabled channels after a successful channel scan.", "status": "unknown"},
                {"name": "Scene identification", "group": "dependent", "parent_id": "library-sync", "source": "Library sync", "details": "Backfills scene fingerprints when the optional scraper is installed and work is available.", "status": "unknown"},
                {"name": "Performer and scene metadata backfill", "group": "dependent", "parent_id": "library-sync", "source": "Library sync", "details": "Title-based performer discovery and PBC, TPDB and StashDB enrichment; subject to configuration and available work.", "status": "unknown"},
                {"name": "Library maintenance", "group": "dependent", "parent_id": "library-sync", "source": "Library sync", "details": "Retention, orphan cleanup and stream maintenance depend on source success and safety guards.", "status": "unknown"},
                {"name": "Jellyfin reconciliation and refresh", "group": "dependent", "parent_id": "library-sync", "source": "Library sync", "details": "Subject to configuration and available work.", "status": "unknown"},
            ])
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

    return router
