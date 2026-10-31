"""Database-free contracts for schedule API validation and write policy."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resolver"))

import admin_v2_schedules
from common import schedule_stages


def request(path, payload=None, *, method="POST"):
    body = json.dumps(payload).encode() if payload is not None else b""

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    headers = []
    if body:
        headers.extend([(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())])
    return Request({
        "type": "http", "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
    }, receive=receive)


class InlineAsyncio:
    @staticmethod
    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)


def stage_rows(*, title_result=None):
    """Stand in for the recorded stage rows, which need a database connection."""
    rows = []
    for stage in schedule_stages.STAGES:
        rows.append({
            "id": stage.id, "schedule_id": stage.schedule_id, "name": stage.name,
            "details": stage.details, "run_action": stage.run_action,
            "run_unavailable_reason": stage.run_unavailable_reason,
            "enabled": True, "revision": 1,
            "last_result": title_result if stage.id == "telegram-title-cleanup" else None,
            "last_error": None, "last_run_at": None, "last_finished_at": None,
        })
    return rows


def make_admin(denied=None):
    return SimpleNamespace(
        _admin_blocked=lambda _request: False,
        _write_denied=lambda _request: denied,
        _wake_worker=lambda _kind: None,
        JSONResponse=JSONResponse,
        asyncio=InlineAsyncio,
        HUB_DB_URL="postgresql://fixture.invalid/catalog",
    )


class TestAdminV2Schedules(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_list_does_not_seed_or_write(self):
        admin = make_admin()
        endpoint = next(route.endpoint for route in admin_v2_schedules.build_router(admin).routes
                        if route.path == "/api/v2/schedules" and "GET" in route.methods)
        with patch("common.admin_schedules.list_schedules", return_value=[]), \
             patch("common.system_schedules.list_system_schedules", return_value=[]), \
             patch("common.schedule_stages.list_stage_runs", return_value=stage_rows()), \
             patch("common.system_schedules.ensure_system_schedules") as seed:
            result = await endpoint(request("/admin/api/v2/schedules", method="GET"))
        self.assertTrue(result["ok"])
        seed.assert_not_called()
        self.assertEqual({item["id"] for item in result["system_items"] if item["group"] == "dependent"},
                         {stage.id for stage in schedule_stages.STAGES})

    async def test_title_cleanup_is_listed_as_library_sync_child_with_progress(self):
        admin = make_admin()
        endpoint = next(route.endpoint for route in admin_v2_schedules.build_router(admin).routes
                        if route.path == "/api/v2/schedules" and "GET" in route.methods)
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "telegram-title-cleanup.json"
            status_path.write_text(json.dumps({
                "status": "complete", "last_result": "done", "last_run_at": "2026-10-31T08:00:00+00:00",
                "processed": 900, "total": 900, "titles_changed": 140,
                "tags_added": 50, "categories_added": 35,
            }), encoding="utf-8")
            with patch.dict(os.environ, {"TELEGRAM_TITLE_CLEANUP_STATUS_PATH": str(status_path)}), \
                 patch("common.admin_schedules.list_schedules", return_value=[]), \
                 patch("common.system_schedules.list_system_schedules", return_value=[]), \
                 patch("common.schedule_stages.list_stage_runs", return_value=stage_rows()):
                result = await endpoint(request("/admin/api/v2/schedules", method="GET"))
        cleanup = next(item for item in result["system_items"]
                       if item.get("id") == "telegram-title-cleanup")
        self.assertEqual(cleanup["parent_id"], "library-sync")
        self.assertEqual(cleanup["status"], "done")
        self.assertEqual(cleanup["progress"], {
            "processed": 900, "total": 900, "titles_changed": 140,
            "tags_added": 50, "categories_added": 35,
        })

    async def test_recorded_stage_state_wins_over_the_cleanup_file(self):
        admin = make_admin()
        endpoint = next(route.endpoint for route in admin_v2_schedules.build_router(admin).routes
                        if route.path == "/api/v2/schedules" and "GET" in route.methods)
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "telegram-title-cleanup.json"
            status_path.write_text(json.dumps({"status": "complete", "last_result": "done"}), encoding="utf-8")
            with patch.dict(os.environ, {"TELEGRAM_TITLE_CLEANUP_STATUS_PATH": str(status_path)}), \
                 patch("common.admin_schedules.list_schedules", return_value=[]), \
                 patch("common.system_schedules.list_system_schedules", return_value=[]), \
                 patch("common.schedule_stages.list_stage_runs",
                       return_value=stage_rows(title_result="failed")):
                result = await endpoint(request("/admin/api/v2/schedules", method="GET"))
        cleanup = next(item for item in result["system_items"]
                       if item.get("id") == "telegram-title-cleanup")
        self.assertEqual(cleanup["status"], "failed")
        self.assertTrue(cleanup["problem"])

    async def test_stage_toggle_reports_conflict_and_missing_stage(self):
        admin = make_admin()
        endpoint = next(route.endpoint for route in admin_v2_schedules.build_router(admin).routes
                        if route.path == "/api/v2/schedules/system/{schedule_id}/stages/{stage_id}/enabled")
        with patch("common.schedule_stages.set_stage_enabled",
                   return_value={"outcome": "conflict", "revision": 3}) as update:
            conflict = await endpoint(request("/admin/api/v2/schedules/system/library-sync/stages/x/enabled",
                                              {"enabled": False, "revision": 2}), "library-sync", "x")
        self.assertEqual(conflict.status_code, 409)
        update.assert_called_once_with(admin.HUB_DB_URL, "library-sync", "x",
                                       enabled=False, revision=2)
        with patch("common.schedule_stages.set_stage_enabled", return_value={"outcome": "not_found"}):
            missing = await endpoint(request("/admin/api/v2/schedules/system/library-sync/stages/x/enabled",
                                             {"enabled": False, "revision": 2}), "library-sync", "x")
        self.assertEqual(missing.status_code, 404)
        invalid = await endpoint(request("/admin/api/v2/schedules/system/library-sync/stages/x/enabled",
                                         {"enabled": False}), "library-sync", "x")
        self.assertEqual(invalid.status_code, 400)

    async def test_stage_run_queues_the_job_and_wakes_the_worker(self):
        admin = make_admin()
        endpoint = next(route.endpoint for route in admin_v2_schedules.build_router(admin).routes
                        if route.path == "/api/v2/schedules/system/{schedule_id}/stages/{stage_id}/run")
        with patch("common.schedule_stages.request_stage_run",
                   return_value={"outcome": "queued", "action": "scene_backfill", "job_id": "job-1"}) as run, \
             patch.object(admin, "_wake_worker") as wake:
            accepted = await endpoint(request("/admin/api/v2/schedules/system/library-sync/stages/scene-identification/run",
                                              {}), "library-sync", "scene-identification")
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(json.loads(accepted.body), {"ok": True, "outcome": "queued",
                                                     "action": "scene_backfill", "job_id": "job-1"})
        run.assert_called_once_with(admin.HUB_DB_URL, "library-sync", "scene-identification")
        wake.assert_called_once_with("jobs")
        for outcome, code in (("not_found", 404), ("disabled", 409), ("conflict", 409)):
            with patch("common.schedule_stages.request_stage_run", return_value={"outcome": outcome}), \
                 patch.object(admin, "_wake_worker") as wake:
                response = await endpoint(request("/admin/api/v2/schedules/system/library-sync/stages/x/run",
                                                  {}), "library-sync", "x")
            self.assertEqual(response.status_code, code)
            wake.assert_not_called()
        with patch("common.schedule_stages.request_stage_run",
                   side_effect=ValueError("This stage has no independent trigger.")):
            untriggerable = await endpoint(request("/admin/api/v2/schedules/system/library-sync/stages/source-scans/run",
                                                   {}), "library-sync", "source-scans")
        self.assertEqual(untriggerable.status_code, 400)
        self.assertNotIn("Traceback", untriggerable.body.decode())

    async def test_stage_run_is_refused_before_the_body_is_parsed(self):
        admin = make_admin(JSONResponse({"error": "Read only"}, status_code=403))
        routes = {route.path: route.endpoint
                  for route in admin_v2_schedules.build_router(admin).routes}
        with patch("common.schedule_stages.request_stage_run") as run, \
             patch("common.schedule_stages.set_stage_enabled") as toggle:
            denied_run = await routes["/api/v2/schedules/system/{schedule_id}/stages/{stage_id}/run"](
                request("/admin/api/v2/schedules/system/library-sync/stages/x/run", method="POST"),
                "library-sync", "x")
            denied_toggle = await routes["/api/v2/schedules/system/{schedule_id}/stages/{stage_id}/enabled"](
                request("/admin/api/v2/schedules/system/library-sync/stages/x/enabled", method="POST"),
                "library-sync", "x")
        self.assertEqual(denied_run.status_code, 403)
        self.assertEqual(denied_toggle.status_code, 403)
        run.assert_not_called()
        toggle.assert_not_called()

    async def test_create_schedule_rejects_irrelevant_ids_and_non_warsaw_timezone(self):
        admin = make_admin()
        endpoint = next(route.endpoint for route in admin_v2_schedules.build_router(admin).routes
                        if route.path == "/api/v2/schedules" and "POST" in route.methods)
        base = {"name": "Nightly backup", "action": "backup_create",
                "schedule_type": "daily", "daily_at": "03:00", "timezone": "Europe/Warsaw"}
        with patch("common.admin_schedules.create_schedule", return_value={"id": "schedule-1"}) as create:
            irrelevant = await endpoint(request("/admin/api/v2/schedules", {**base, "video_ids": ["v1"]}))
            wrong_zone = await endpoint(request("/admin/api/v2/schedules", {**base, "timezone": "UTC"}))
            accepted = await endpoint(request("/admin/api/v2/schedules", base))
        self.assertEqual(irrelevant.status_code, 400)
        self.assertEqual(wrong_zone.status_code, 400)
        self.assertEqual(accepted.status_code, 201)
        create.assert_called_once_with(
            admin.HUB_DB_URL, name="Nightly backup", action="backup_create", schedule_type="daily",
            interval_minutes=None, daily_at="03:00", payload={},
        )

    async def test_write_denial_happens_before_schedule_body_is_parsed(self):
        denied = JSONResponse({"error": "Read only"}, status_code=403)
        admin = make_admin(denied)
        endpoint = next(route.endpoint for route in admin_v2_schedules.build_router(admin).routes
                        if route.path == "/api/v2/schedules" and "POST" in route.methods)
        with patch("common.admin_schedules.create_schedule") as create:
            response = await endpoint(request("/admin/api/v2/schedules", method="POST"))
        self.assertEqual(response.status_code, 403)
        create.assert_not_called()

    async def test_enabled_revision_conflict_is_returned_without_claiming_success(self):
        admin = make_admin()
        endpoint = next(route.endpoint for route in admin_v2_schedules.build_router(admin).routes
                        if route.path == "/api/v2/schedules/{schedule_id}/enabled")
        with patch("common.admin_schedules.set_enabled", return_value={"outcome": "conflict", "revision": 3}) as update:
            response = await endpoint(request("/admin/api/v2/schedules/id/enabled", {
                "enabled": False, "revision": 2,
            }), "id")
        self.assertEqual(response.status_code, 409)
        update.assert_called_once_with(admin.HUB_DB_URL, "id", enabled=False, revision=2)


if __name__ == "__main__":
    unittest.main()
