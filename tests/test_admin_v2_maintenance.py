"""Database/network-free contract tests for Admin v2 maintenance actions."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse
from starlette.requests import Request


import resolver.admin_v2_maintenance as admin_v2_maintenance


class InlineAsyncio:
    @staticmethod
    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)


def make_request(path, *, body=None, cache_clear=None):
    encoded = b"" if body is None else json.dumps(body).encode()
    received = False

    async def receive():
        nonlocal received
        if received:
            return {"type": "http.request", "body": b"", "more_body": False}
        received = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    return Request({
        "type": "http", "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
        "app": SimpleNamespace(state=SimpleNamespace(admin_cache_clear=cache_clear)),
    }, receive=receive)


def response_body(result):
    return json.loads(result.body) if isinstance(result, JSONResponse) else result


def make_admin(*, denied=False):
    wake_calls = []
    admin = SimpleNamespace(
        _write_denied=lambda _request: JSONResponse({"error": "Writes are disabled."}, status_code=403) if denied else None,
        JSONResponse=JSONResponse,
        asyncio=InlineAsyncio,
        HUB_DB_URL="postgresql://test.invalid/admin",
        CONFIG_PATH="/nonexistent/config.yaml",
        _wake_worker=lambda *args, **kwargs: wake_calls.append((args, kwargs)),
    )
    admin.wake_calls = wake_calls
    return admin


class TestAdminV2Maintenance(unittest.IsolatedAsyncioTestCase):
    def routes(self, admin):
        return {route.path: route.endpoint for route in admin_v2_maintenance.build_router(admin).routes}

    async def test_clear_cache_reports_count_unavailable_and_failure_without_details(self):
        admin = make_admin()
        route = self.routes(admin)["/api/v2/maintenance/cache/clear"]
        result = await route(make_request("/admin/api/v2/maintenance/cache/clear", cache_clear=lambda: 3))
        self.assertEqual(result, {"ok": True, "cleared": 3})
        negative = await route(make_request("/admin/api/v2/maintenance/cache/clear", cache_clear=lambda: -2))
        self.assertEqual(negative["cleared"], 0)
        unavailable = await route(make_request("/admin/api/v2/maintenance/cache/clear"))
        self.assertEqual(unavailable.status_code, 503)
        failed = await route(make_request("/admin/api/v2/maintenance/cache/clear",
                                          cache_clear=lambda: (_ for _ in ()).throw(RuntimeError("/private/secret"))))
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("private", str(response_body(failed)))

    async def test_jellyfin_refresh_sends_controlled_request_and_reports_only_acceptance(self):
        admin = make_admin()
        route = self.routes(admin)["/api/v2/maintenance/jellyfin/refresh"]
        calls = []

        class Client:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, url, headers):
                calls.append((url, headers, self.timeout))
                return SimpleNamespace(status_code=204)

        with patch.dict("os.environ", {"JELLYFIN_URL": "http://jellyfin.invalid/", "JELLYFIN_API_KEY": "secret"}), \
             patch.object(admin_v2_maintenance.httpx, "AsyncClient", Client):
            result = await route(make_request("/admin/api/v2/maintenance/jellyfin/refresh"))
        self.assertEqual(result, {"ok": True, "requested": True})
        self.assertEqual(calls[0][0], "http://jellyfin.invalid/Library/Refresh")
        self.assertEqual(calls[0][1]["Authorization"], 'MediaBrowser Token="secret"')
        self.assertEqual(calls[0][2], 10.0)

    async def test_jellyfin_rejects_configuration_and_remote_errors_without_leaking_details(self):
        admin = make_admin()
        route = self.routes(admin)["/api/v2/maintenance/jellyfin/refresh"]
        with patch.dict("os.environ", {"JELLYFIN_URL": "", "JELLYFIN_API_KEY": ""}):
            unavailable = await route(make_request("/admin/api/v2/maintenance/jellyfin/refresh"))
        self.assertEqual(unavailable.status_code, 503)

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, *_args, **_kwargs):
                return SimpleNamespace(status_code=503)

        with patch.dict("os.environ", {"JELLYFIN_URL": "http://jellyfin.invalid", "JELLYFIN_API_KEY": "secret"}), \
             patch.object(admin_v2_maintenance.httpx, "AsyncClient", Client):
            rejected = await route(make_request("/admin/api/v2/maintenance/jellyfin/refresh"))
        self.assertEqual(rejected.status_code, 502)
        self.assertNotIn("secret", str(response_body(rejected)))

    async def test_collections_refresh_enqueues_durable_action_and_wakes_worker(self):
        admin = make_admin()
        route = self.routes(admin)["/api/v2/maintenance/collections/refresh"]
        with patch.dict("os.environ", {"JELLYFIN_URL": "http://jellyfin.invalid", "JELLYFIN_API_KEY": "secret"}), \
             patch("resolver.video_duplicates.enqueue_job", return_value="job-collections") as enqueue:
            result = await route(make_request("/admin/api/v2/maintenance/collections/refresh"))
        self.assertEqual(result.status_code, 202)
        self.assertEqual(response_body(result), {"ok": True, "queued": True,
                                                 "job_id": "job-collections", "state": "queued"})
        enqueue.assert_called_once_with(admin.HUB_DB_URL, "collections_refresh", {})
        self.assertEqual(admin.wake_calls, [(('jobs',), {})])

    async def test_collections_refresh_rejects_missing_jellyfin_configuration_before_enqueue(self):
        admin = make_admin()
        route = self.routes(admin)["/api/v2/maintenance/collections/refresh"]
        with patch.dict("os.environ", {"JELLYFIN_URL": "", "JELLYFIN_API_KEY": ""}), \
             patch("resolver.video_duplicates.enqueue_job") as enqueue:
            result = await route(make_request("/admin/api/v2/maintenance/collections/refresh"))
        self.assertEqual(result.status_code, 503)
        self.assertEqual(response_body(result), {
            "ok": False, "error": "Jellyfin collection refresh is not configured."
        })
        enqueue.assert_not_called()
        self.assertEqual(admin.wake_calls, [])

    async def test_global_sync_queues_scheduled_mode_and_rejects_invalid_mode(self):
        admin = make_admin()
        route = self.routes(admin)["/api/v2/maintenance/sync"]
        with patch("resolver.video_duplicates.enqueue_job", return_value="job-sync") as enqueue:
            result = await route(make_request("/admin/api/v2/maintenance/sync",
                                              body={"source": None, "sync_mode": "scheduled"}))
        self.assertEqual(result.status_code, 202)
        self.assertEqual(response_body(result), {"ok": True, "job_id": "job-sync", "state": "queued",
                                                 "source": None, "sync_mode": "scheduled"})
        enqueue.assert_called_once_with(admin.HUB_DB_URL, "sync", {"source": None, "sync_mode": "scheduled"})
        self.assertEqual(admin.wake_calls, [(('sync',), {"source": None})])
        rejected = await route(make_request("/admin/api/v2/maintenance/sync", body={"sync_mode": "unsafe"}))
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(response_body(rejected)["error"], "Sync mode must be scheduled, new, or backfill.")

    async def test_write_denial_happens_before_cache_jellyfin_or_queue_side_effects(self):
        admin = make_admin(denied=True)
        routes = self.routes(admin)
        cache_calls = []
        cache = await routes["/api/v2/maintenance/cache/clear"](
            make_request("/admin/api/v2/maintenance/cache/clear", cache_clear=lambda: cache_calls.append(1)))
        collections = await routes["/api/v2/maintenance/collections/refresh"](
            make_request("/admin/api/v2/maintenance/collections/refresh"))
        jellyfin = await routes["/api/v2/maintenance/jellyfin/refresh"](
            make_request("/admin/api/v2/maintenance/jellyfin/refresh"))
        sync = await routes["/api/v2/maintenance/sync"](
            make_request("/admin/api/v2/maintenance/sync", body={"sync_mode": "scheduled"}))
        for result in (cache, collections, jellyfin, sync):
            self.assertEqual(result.status_code, 403)
        self.assertEqual(cache_calls, [])
        self.assertEqual(admin.wake_calls, [])


if __name__ == "__main__":
    unittest.main()
