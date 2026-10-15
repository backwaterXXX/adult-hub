"""Routing and filtering tests for automatic Admin v2 activity capture."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resolver"))
sys.path.insert(0, str(ROOT))

import admin_v2_errors


class TestAdminActivityCapture(unittest.TestCase):
    def _request(self, method="POST", path="/admin/api/v2/audits/execute", route="/api/v2/audits/execute"):
        return SimpleNamespace(
            method=method,
            url=SimpleNamespace(path=path),
            scope={"route": SimpleNamespace(path=route)},
        )

    def _response(self, status=202, correlation_id="b" * 32):
        return SimpleNamespace(status_code=status, headers={"X-Correlation-ID": correlation_id})

    def test_restores_admin_prefix_to_local_included_route_template(self):
        async def inline_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        with patch.object(admin_v2_errors, "_ACTIVITY_DATABASE", lambda: "test-db"), \
             patch("common.admin_action_events.record_event") as record, \
             patch.object(admin_v2_errors.asyncio, "to_thread", inline_to_thread):
            asyncio.run(admin_v2_errors._record_mutation(self._request(), self._response()))
        record.assert_called_once_with(
            "test-db", method="POST", route="/admin/api/v2/audits/execute",
            status_code=202, correlation_id="b" * 32,
        )

    def test_does_not_record_gets_failed_requests_or_non_v2_routes(self):
        with patch.object(admin_v2_errors, "_ACTIVITY_DATABASE", lambda: "test-db"), \
             patch("common.admin_action_events.record_event") as record:
            asyncio.run(admin_v2_errors._record_mutation(self._request("GET"), self._response()))
            asyncio.run(admin_v2_errors._record_mutation(self._request(), self._response(403)))
            asyncio.run(admin_v2_errors._record_mutation(
                self._request(path="/admin/api/jobs/retry", route="/admin/api/jobs/{job_id}/retry"),
                self._response(),
            ))
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
