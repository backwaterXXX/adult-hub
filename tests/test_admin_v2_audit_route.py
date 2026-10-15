"""Database-free projection checks for paginated consistency-audit reads."""

import sys
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from fastapi.responses import JSONResponse
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resolver"))

import admin_v2_audits


def request(path="/admin/api/v2/audits", method="GET", body=None):
    raw = path.encode()
    payload = json.dumps(body if body is not None else {}).encode() if method == "POST" else b""
    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}
    headers = [(b"content-length", str(len(payload)).encode())] if method == "POST" else []
    return Request({
        "type": "http", "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": raw, "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
    }, receive)


class InlineAsyncio:
    @staticmethod
    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)


class Cursor:
    def __init__(self, rows=(), row=None):
        self.rows = list(rows)
        self.row = row

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, result):
        self.result = result

    def execute(self, sql, params=()):
        if "SELECT j.id,j.state,j.created_at,j.updated_at,j.result" in sql:
            return Cursor(rows=[(
                "audit-fixture", "done", datetime(2026, 10, 10, tzinfo=timezone.utc),
                datetime(2026, 10, 10, tzinfo=timezone.utc), self.result, "consistency_audit", 3,
            )])
        if "SELECT count(*) FROM video_duplicate_jobs WHERE action IN" in sql:
            return Cursor(row=(1,))
        if "SELECT id,state,result,action FROM video_duplicate_jobs" in sql:
            return Cursor(row=("audit-fixture", "done", self.result, "consistency_audit"))
        if "SELECT id,video_id,kind,status,evidence,details,checked_at" in sql:
            return Cursor(rows=[])
        if "SELECT count(*) FROM admin_audit_items" in sql:
            return Cursor(row=(3,))
        raise AssertionError("Unexpected audit query")

    def close(self):
        pass


class TestAuditRouteCatalogCoverage(unittest.IsolatedAsyncioTestCase):
    def make_routes(self, denied=None, wake=None):
        admin = SimpleNamespace(
            _admin_blocked=lambda _request: False,
            _write_denied=lambda _request: denied,
            _wake_worker=wake or (lambda _worker: None),
            JSONResponse=JSONResponse,
            asyncio=InlineAsyncio,
            HUB_DB_URL="postgresql://fixture.invalid/catalog",
        )
        return {route.path: route.endpoint for route in admin_v2_audits.build_router(admin).routes}

    async def test_history_and_detail_expose_checked_of_total_and_partial_inventory(self):
        result = {
            "checked": 2, "total": 4,
            "audit_mode": "file_consistency",
            "progress": {"stage": "auditing_library", "completed": 3, "total": 6},
            "library_inventory_status": "partial", "library_inventory_entries": 50,
            "jellyfin_import_state": "unknown",
        }
        connection = FakeConnection(result)
        routes = self.make_routes()
        with patch("admin_v2_audits.db.connect", return_value=connection):
            history = await routes["/api/v2/audits"](request(), offset=0, limit=20)
            detail = await routes["/api/v2/audits/{job_id}"](
                request("/admin/api/v2/audits/audit-fixture"), "audit-fixture", offset=0, limit=50,
            )

        item = history["items"][0]
        self.assertEqual((item["checked"], item["catalog_total"]), (2, 4))
        self.assertEqual(item["mode"], "file_consistency")
        self.assertEqual((item["progress_completed"], item["progress_total"]), (3, 6))
        self.assertEqual(item["library_inventory_status"], "partial")
        self.assertEqual((detail["audit"]["checked"], detail["audit"]["catalog_total"]), (2, 4))
        self.assertEqual(detail["audit"]["mode"], "file_consistency")
        self.assertEqual((detail["audit"]["progress_completed"], detail["audit"]["progress_total"]), (3, 6))
        self.assertEqual(detail["audit"]["library_inventory_status"], "partial")
        self.assertEqual(detail["total"], 3)

    async def test_full_mode_atomically_queues_distinct_jobs_and_returns_both_ids(self):
        routes = self.make_routes()
        with patch("common.job_queue.enqueue_jobs_batch", return_value=["metadata-job", "file-job"]) as enqueue:
            response = await routes["/api/v2/audits/execute"](
                request("/admin/api/v2/audits/execute", "POST", {"mode": "full"})
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(json.loads(response.body), {
            "ok": True, "job_id": "file-job", "job_ids": ["metadata-job", "file-job"],
            "mode": "full", "state": "queued",
        })
        enqueue.assert_called_once_with(
            "postgresql://fixture.invalid/catalog",
            [("metadata_quality_audit", {"audit_batch_id": ANY}),
             ("consistency_audit", {"mode": "file_consistency", "audit_batch_id": ANY})],
        )
        first_batch = enqueue.call_args.args[1][0][1]["audit_batch_id"]
        second_batch = enqueue.call_args.args[1][1][1]["audit_batch_id"]
        self.assertRegex(first_batch, r"^[0-9a-f]{32}$")
        self.assertEqual(first_batch, second_batch)

    async def test_individual_modes_are_validated_and_keep_write_permission(self):
        routes = self.make_routes()
        with patch("common.job_queue.enqueue_job", return_value="queued-job") as enqueue:
            metadata = await routes["/api/v2/audits/execute"](
                request("/admin/api/v2/audits/execute", "POST", {"mode": "metadata_quality"})
            )
            files = await routes["/api/v2/audits/execute"](
                request("/admin/api/v2/audits/execute", "POST", {"mode": "file_consistency"})
            )
            invalid = await routes["/api/v2/audits/execute"](
                request("/admin/api/v2/audits/execute", "POST", {"mode": "full", "force": True})
            )
        self.assertEqual(metadata.status_code, 202)
        self.assertEqual(files.status_code, 202)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(enqueue.call_args_list[0].args[1:], ("metadata_quality_audit", {}))
        self.assertEqual(enqueue.call_args_list[1].args[1:], ("consistency_audit", {"mode": "file_consistency"}))

        denied = self.make_routes(denied=JSONResponse({"error": "read only"}, status_code=403))
        with patch("common.job_queue.enqueue_job") as enqueue_denied:
            response = await denied["/api/v2/audits/execute"](
                request("/admin/api/v2/audits/execute", "POST", {"mode": "metadata_quality"})
            )
        self.assertEqual(response.status_code, 403)
        enqueue_denied.assert_not_called()

    async def test_invalid_negative_coverage_values_are_not_exposed(self):
        connection = FakeConnection({"checked": -1, "total": -1, "library_inventory_status": "complete"})
        routes = self.make_routes()
        with patch("admin_v2_audits.db.connect", return_value=connection):
            history = await routes["/api/v2/audits"](request(), offset=0, limit=20)
            detail = await routes["/api/v2/audits/{job_id}"](
                request("/admin/api/v2/audits/audit-fixture"), "audit-fixture", offset=0, limit=50,
            )
        self.assertIsNone(history["items"][0]["checked"])
        self.assertIsNone(history["items"][0]["catalog_total"])
        self.assertIsNone(detail["audit"]["checked"])
        self.assertIsNone(detail["audit"]["catalog_total"])


if __name__ == "__main__":
    unittest.main()
