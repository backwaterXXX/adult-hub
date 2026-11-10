"""Database-free contracts for persisted V2 operation routes."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse
from starlette.requests import Request


import resolver.admin_v2_operations as admin_v2_operations


def request(path, payload=None, *, method="POST", raw=None):
    body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else b"")

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    headers = []
    if body:
        headers.extend([
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ])
    return Request({
        "type": "http", "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
    }, receive=receive)


def make_admin(denied=None):
    class InlineAsyncio:
        @staticmethod
        async def to_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

    wakes = []
    admin = SimpleNamespace(
        _write_denied=lambda _request: denied,
        _admin_blocked=lambda _request: False,
        JSONResponse=JSONResponse,
        asyncio=InlineAsyncio,
        HUB_DB_URL="postgresql://fixture.invalid/catalog",
        CONFIG_PATH="/fixture/config.yaml",
        _wake_worker=lambda worker: wakes.append(worker),
    )
    admin.wakes = wakes
    return admin


class TestAdminV2Operations(unittest.IsolatedAsyncioTestCase):
    async def test_hide_preview_requires_write_access_and_delegates_explicit_scope(self):
        denied = JSONResponse({"error": "Read only"}, status_code=403)
        blocked = make_admin(denied)
        endpoint = {route.path: route.endpoint for route in admin_v2_operations.build_router(blocked).routes}[
            "/api/v2/operations/preview"]
        with patch("common.admin_operations.preview_hide") as preview_hide:
            response = await endpoint(request("/admin/api/v2/operations/preview", {
                "type": "hide", "keeper_id": "keeper", "loser_ids": ["loser"],
            }))
        self.assertEqual(response.status_code, 403)
        preview_hide.assert_not_called()

        admin = make_admin()
        endpoint = {route.path: route.endpoint for route in admin_v2_operations.build_router(admin).routes}[
            "/api/v2/operations/preview"]
        result = {
            "outcome": "previewed", "plan_id": "plan-1", "operation_type": "hide",
            "keeper_id": "keeper", "items": [{"video_id": "loser", "file_count": 2}],
            "reversible": True,
        }
        with patch("common.admin_operations.preview_hide", return_value=result) as preview_hide:
            response = await endpoint(request("/admin/api/v2/operations/preview", {
                "type": "hide", "keeper_id": "keeper", "loser_ids": ["loser"],
            }))
        self.assertEqual(response["plan_id"], "plan-1")
        preview_hide.assert_called_once_with(admin.HUB_DB_URL, "keeper", ["loser"], admin.CONFIG_PATH)

    async def test_preview_rejects_extra_scope_fields_and_maps_conflict(self):
        admin = make_admin()
        endpoint = {route.path: route.endpoint for route in admin_v2_operations.build_router(admin).routes}[
            "/api/v2/operations/preview"]
        with patch("common.admin_operations.preview_hide") as preview_hide:
            invalid = await endpoint(request("/admin/api/v2/operations/preview", {
                "type": "hide", "keeper_id": "keeper", "loser_ids": ["loser"], "path": "/tmp/x",
            }))
        self.assertEqual(invalid.status_code, 400)
        preview_hide.assert_not_called()

        with patch("common.admin_operations.preview_hide", return_value={
            "outcome": "conflict", "message": "Selected data changed.",
        }):
            conflict = await endpoint(request("/admin/api/v2/operations/preview", {
                "type": "hide", "keeper_id": "keeper", "loser_ids": ["loser"],
            }))
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.body, b'{"error":"Selected data changed."}')

    async def test_metadata_batch_preview_requires_write_access_and_delegates_explicit_selection(self):
        payload = {"type": "metadata_update_batch", "video_ids": ["video-a", "video-b"],
                   "changes": {"studio": "Studio"}, "reset_fields": ["plot"]}
        denied = JSONResponse({"error": "Read only"}, status_code=403)
        blocked_endpoint = {route.path: route.endpoint for route in admin_v2_operations.build_router(
            make_admin(denied)).routes}["/api/v2/operations/preview"]
        with patch("common.admin_operations.preview_metadata_batch_plan") as preview_batch:
            response = await blocked_endpoint(request("/admin/api/v2/operations/preview", payload))
        self.assertEqual(response.status_code, 403)
        preview_batch.assert_not_called()

        admin = make_admin()
        endpoint = {route.path: route.endpoint for route in admin_v2_operations.build_router(admin).routes}[
            "/api/v2/operations/preview"]
        expected = {"outcome": "previewed", "plan_id": "batch-plan", "operation_type": "metadata_update_batch",
                    "items": [{"video_id": "video-a", "fields": {"studio": {"before": "A", "after": "Studio"}}}],
                    "reversible": True}
        with patch("common.admin_operations.preview_metadata_batch_plan", return_value=expected) as preview_batch:
            response = await endpoint(request("/admin/api/v2/operations/preview", payload))
        self.assertEqual(response["plan_id"], "batch-plan")
        preview_batch.assert_called_once_with(admin.HUB_DB_URL, payload["video_ids"], payload["changes"], payload["reset_fields"])

        large_payload = {**payload, "changes": {"plot": "💙" * 5_000}}
        self.assertGreater(len(json.dumps(large_payload).encode()), admin_v2_operations.MAX_OPERATION_REQUEST_BYTES)
        with patch("common.admin_operations.preview_metadata_batch_plan", return_value=expected) as preview_batch:
            large_response = await endpoint(request("/admin/api/v2/operations/preview", large_payload))
        self.assertEqual(large_response["plan_id"], "batch-plan",
                         "bounded metadata batch text may exceed the smaller generic operation limit")
        preview_batch.assert_called_once_with(admin.HUB_DB_URL, large_payload["video_ids"], large_payload["changes"], large_payload["reset_fields"])

    async def test_metadata_batch_preview_rejects_extra_scope_before_service_call(self):
        endpoint = {route.path: route.endpoint for route in admin_v2_operations.build_router(make_admin()).routes}[
            "/api/v2/operations/preview"]
        with patch("common.admin_operations.preview_metadata_batch_plan") as preview_batch:
            response = await endpoint(request("/admin/api/v2/operations/preview", {
                "type": "metadata_update_batch", "video_ids": ["video-a"],
                "changes": {"title": "Edited"}, "reset_fields": [], "path": "/data/video.nfo",
            }))
        self.assertEqual(response.status_code, 400)
        preview_batch.assert_not_called()

    async def test_execute_queues_and_wakes_only_for_accepted_plan(self):
        admin = make_admin()
        endpoint = {route.path: route.endpoint for route in admin_v2_operations.build_router(admin).routes}[
            "/api/v2/operations/execute"]
        accepted = {"outcome": "queued", "plan_id": "plan-1", "jobs": [{"job_id": "job-1"}],
                    "reused": False}
        with patch("common.admin_operations.execute_operation", return_value=accepted) as execute:
            response = await endpoint(request("/admin/api/v2/operations/execute", {"plan_id": "plan-1"}))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(admin.wakes, ["jobs"])
        execute.assert_called_once_with(admin.HUB_DB_URL, "plan-1", admin.CONFIG_PATH)

        admin.wakes.clear()
        with patch("common.admin_operations.execute_operation", return_value={
            "outcome": "stale", "message": "The catalog changed.", "stale_ids": ["video-1"],
        }):
            stale = await endpoint(request("/admin/api/v2/operations/execute", {"plan_id": "plan-1"}))
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(admin.wakes, [])

    async def test_operation_body_is_bounded_before_any_service_call(self):
        admin = make_admin()
        routes = {route.path: route.endpoint for route in admin_v2_operations.build_router(admin).routes}
        with patch("common.admin_operations.preview_hide") as preview_hide:
            oversized = await routes["/api/v2/operations/preview"](
                request("/admin/api/v2/operations/preview", raw=b" " * (16 * 1024 + 1)))
        self.assertEqual(oversized.status_code, 413)
        preview_hide.assert_not_called()

        far_oversized = await {route.path: route.endpoint for route in admin_v2_operations.build_router(admin).routes}[
            "/api/v2/operations/preview"](
                request("/admin/api/v2/operations/preview", raw=b" " * (admin_v2_operations.MAX_METADATA_BATCH_REQUEST_BYTES + 1)))
        self.assertEqual(far_oversized.status_code, 413)


if __name__ == "__main__":
    unittest.main()
