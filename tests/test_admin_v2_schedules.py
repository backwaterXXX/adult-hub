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


def make_admin(denied=None):
    return SimpleNamespace(
        _admin_blocked=lambda _request: False,
        _write_denied=lambda _request: denied,
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
             patch("common.system_schedules.ensure_system_schedules") as seed:
            result = await endpoint(request("/admin/api/v2/schedules", method="GET"))
        self.assertTrue(result["ok"])
        seed.assert_not_called()

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
                 patch("common.system_schedules.list_system_schedules", return_value=[]):
                result = await endpoint(request("/admin/api/v2/schedules", method="GET"))
        cleanup = next(item for item in result["system_items"]
                       if item.get("id") == "telegram-title-cleanup")
        self.assertEqual(cleanup["parent_id"], "library-sync")
        self.assertEqual(cleanup["status"], "done")
        self.assertEqual(cleanup["progress"], {
            "processed": 900, "total": 900, "titles_changed": 140,
            "tags_added": 50, "categories_added": 35,
        })

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
