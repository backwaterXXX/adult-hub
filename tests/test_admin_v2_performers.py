"""Focused, database-free tests for the Admin v2 fuzzy performer scan."""

import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse
from starlette.requests import Request


import resolver.admin_v2_performers as admin_v2_performers


def make_request(method="GET", path="/admin/api/v2/performers/fuzzy", body=None):
    raw_body = (body if isinstance(body, bytes) else json.dumps(body).encode()) if body is not None else b""
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(raw_body)).encode())] if body is not None else []

    async def receive():
        return {"type": "http.request", "body": raw_body, "more_body": False}

    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": headers, "client": ("127.0.0.1", 1), "server": ("test", 80),
        "root_path": "",
    }, receive=receive)


def make_admin(*, blocked=False):
    class InlineAsyncio:
        @staticmethod
        async def to_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

    return SimpleNamespace(
        _admin_blocked=lambda _request: blocked,
        JSONResponse=JSONResponse,
        asyncio=InlineAsyncio,
        HUB_DB_URL="postgresql://test.invalid/catalog",
        _write_denied=lambda _request: None,
        _wake_worker=lambda *_args, **_kwargs: None,
    )


class TestAdminV2FuzzyPerformers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.endpoint = admin_v2_performers.build_router(make_admin()).routes[0].endpoint

    async def test_threshold_bounds_are_inclusive_and_outside_values_are_rejected(self):
        expected = {"ok": True, "groups": []}
        with patch("common.fuzzy_performers.find_fuzzy_performer_candidates", return_value=expected):
            for threshold in (0.80, 0.99):
                with self.subTest(threshold=threshold):
                    self.assertEqual(await self.endpoint(make_request(), threshold), expected)

        for threshold in (0.7999, 0.9901, float("nan"), float("inf")):
            with self.subTest(threshold=threshold):
                response = await self.endpoint(make_request(), threshold)
                self.assertEqual(response.status_code, 400)
                self.assertIn("0.80 and 0.99", response.body.decode())

    async def test_admin_read_block_returns_forbidden_without_running_scan(self):
        admin = make_admin(blocked=True)
        endpoint = admin_v2_performers.build_router(admin).routes[0].endpoint
        with patch("common.fuzzy_performers.find_fuzzy_performer_candidates") as scan:
            response = await endpoint(make_request(), 0.88)
        self.assertEqual(response.status_code, 403)
        scan.assert_not_called()

    async def test_forwards_database_url_and_requested_threshold_through_thread(self):
        calls = []
        result = {"ok": True, "stats": {"total_groups": 2}, "groups": []}

        def fake_scan(*_args, **_kwargs):
            return result

        class RecordingAsyncio:
            @staticmethod
            async def to_thread(function, *args, **kwargs):
                calls.append((function, args, kwargs))
                return result

        admin = make_admin()
        admin.asyncio = RecordingAsyncio
        endpoint = admin_v2_performers.build_router(admin).routes[0].endpoint
        with patch("common.fuzzy_performers.find_fuzzy_performer_candidates", fake_scan):
            response = await endpoint(make_request(), 0.91)

        self.assertEqual(response, result)
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0][0], fake_scan)
        self.assertEqual(calls[0][1], (admin.HUB_DB_URL,))
        self.assertEqual(calls[0][2], {"similarity_threshold": 0.91})

    async def test_backend_exception_is_sanitized(self):
        with patch(
            "common.fuzzy_performers.find_fuzzy_performer_candidates",
            side_effect=RuntimeError("private database URL and secret token"),
        ):
            response = await self.endpoint(make_request(), 0.88)

        self.assertEqual(response.status_code, 503)
        body = response.body.decode()
        self.assertIn("Could not scan performer name variants.", body)
        self.assertNotIn("private database URL", body)
        self.assertNotIn("secret token", body)

    async def test_v2_proposal_and_catalog_reads_use_shared_service_without_v1_handlers(self):
        # This stub intentionally has no admin_performer_* handler attributes:
        # the V2 router must not depend on the legacy route module.
        routes = {route.path: route.endpoint for route in admin_v2_performers.build_router(make_admin()).routes}
        proposals = {"items": [{"id": 3}], "total": 401, "limit": 250, "offset": 25}
        catalog = {"items": [{"id": "p1", "name": "Performer"}]}
        with patch("common.performer_proposals.list_pending_proposals", return_value=proposals) as list_fn, \
             patch("common.performer_proposals.search_catalog", return_value=catalog) as search_fn:
            proposal_result = await routes["/api/v2/performers/proposals"](
                make_request(path="/admin/api/v2/performers/proposals"), 250, 25)
            catalog_result = await routes["/api/v2/performers/catalog"](
                make_request(path="/admin/api/v2/performers/catalog"), "performer", 25)
        self.assertEqual(proposal_result, proposals)
        self.assertEqual(catalog_result, catalog)
        list_fn.assert_called_once_with("postgresql://test.invalid/catalog", 250, 25)
        search_fn.assert_called_once_with("postgresql://test.invalid/catalog", "performer", 25)

    async def test_v2_proposal_review_authorizes_before_id_and_uses_shared_service(self):
        admin = make_admin()
        routes = {route.path: route.endpoint for route in admin_v2_performers.build_router(admin).routes}
        result = {"ok": True, "status": "approved", "refresh_pending": True}
        with patch("common.performer_proposals.review_proposal", return_value=result) as review_fn:
            response = await routes["/api/v2/performers/proposals/{proposal_id}"](
                make_request("POST", "/admin/api/v2/performers/proposals/42", {"action": "approve", "performer_id": "p1"}),
                "42",
            )
        self.assertEqual(response, result)
        self.assertEqual(review_fn.call_args.args[:3], (
            "postgresql://test.invalid/catalog", 42, {"action": "approve", "performer_id": "p1"},
        ))

        calls = []
        admin._write_denied = lambda _request: calls.append("auth") or JSONResponse({"error": "Denied"}, status_code=403)
        routes = {route.path: route.endpoint for route in admin_v2_performers.build_router(admin).routes}
        denied = await routes["/api/v2/performers/proposals/{proposal_id}"](
            make_request("POST", "/admin/api/v2/performers/proposals/not-an-id", {"action": "reject"}),
            "not-an-id",
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(calls, ["auth"])

    async def test_v2_proposal_review_bounds_body_and_rejects_extra_fields(self):
        routes = {route.path: route.endpoint for route in admin_v2_performers.build_router(make_admin()).routes}
        endpoint = routes["/api/v2/performers/proposals/{proposal_id}"]
        with patch("common.performer_proposals.review_proposal") as review:
            large = await endpoint(
                make_request("POST", "/admin/api/v2/performers/proposals/7", b" " * 8193), "7")
            extra = await endpoint(
                make_request("POST", "/admin/api/v2/performers/proposals/7",
                             {"action": "reject", "unexpected": "value"}), "7")
        self.assertEqual(large.status_code, 413)
        self.assertEqual(extra.status_code, 400)
        review.assert_not_called()


if __name__ == "__main__":
    unittest.main()
