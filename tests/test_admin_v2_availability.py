"""Database-free allowlist tests for playback availability history."""

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse
from starlette.requests import Request


import resolver.admin_v2_availability as admin_v2_availability


def request(path, *, query=b""):
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": query, "headers": [],
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
    })


class InlineAsyncio:
    @staticmethod
    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)


class TestAdminV2Availability(unittest.IsolatedAsyncioTestCase):
    async def test_history_projects_known_statuses_and_never_returns_stored_evidence(self):
        when = datetime(2026, 10, 7, tzinfo=timezone.utc)

        class Connection:
            closed = False

            def execute(self, query, _params):
                self.query = query
                return self

            def fetchall(self):
                return [
                    ("available", when, "https://private.invalid/?token=secret"),
                    ("forged", when, "traceback and private identifier"),
                ]

            def close(self):
                self.closed = True

        connection = Connection()
        admin = SimpleNamespace(
            _admin_blocked=lambda _request: False,
            JSONResponse=JSONResponse,
            HUB_DB_URL="postgresql://fixture.invalid/catalog",
        )
        endpoint = next(route.endpoint for route in admin_v2_availability.build_router(admin).routes
                        if route.path == "/api/v2/availability/history")
        with patch("resolver.admin_v2_availability.db.connect", return_value=connection):
            result = await endpoint(request("/admin/api/v2/availability/history",
                                             query=b"video_id=fixture&limit=5"), "fixture", 5)

        self.assertEqual(result["items"], [
            {"status": "available", "checked_at": when.isoformat(),
             "evidence": "A media byte range was served by the resolver playback chain."},
            {"status": "unknown", "checked_at": when.isoformat(),
             "evidence": "The playback probe was inconclusive; no availability change was made."},
        ])
        self.assertNotIn("private.invalid", str(result))
        self.assertNotIn("traceback", str(result))
        self.assertTrue(connection.closed)

    def test_preview_maps_unknown_saved_check_status_to_safe_unknown(self):
        when = datetime(2026, 10, 7, tzinfo=timezone.utc)

        class Result:
            def __init__(self, rows):
                self.rows = rows

            def fetchall(self):
                return self.rows

        class Connection:
            closed = False

            def execute(self, query, _params):
                self.query = query
                return Result([("video-1", "Fixture", 1, False, "forged", when)])

            def close(self):
                self.closed = True

        connection = Connection()
        with patch("resolver.admin_v2_availability.db.connect", return_value=connection):
            preview = admin_v2_availability._preview("postgresql://fixture.invalid/catalog", ["video-1"])
        self.assertEqual(preview["items"][0]["last_check"], "unknown")
        self.assertEqual(preview["items"][0]["current"], "available")
        self.assertNotIn("private.invalid", str(preview))
        self.assertTrue(connection.closed)
        self.assertIn("LEFT JOIN LATERAL", connection.query)


if __name__ == "__main__":
    unittest.main()
