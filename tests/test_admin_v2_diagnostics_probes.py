"""Safe, bounded service-probe diagnostics for Admin V2."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.responses import JSONResponse
from starlette.requests import Request


import resolver.admin_v2_diagnostics as admin_v2_diagnostics
from common import db as db_module


def request(path="/admin/api/v2/diagnostics/probes"):
    raw = path.encode()
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": raw, "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
        "app": SimpleNamespace(state=SimpleNamespace()),
    })


def make_admin(*, blocked=False):
    return SimpleNamespace(
        _admin_blocked=lambda _request: blocked,
        JSONResponse=JSONResponse,
        HUB_DB_URL="postgresql://example.invalid/admin",
    )


class TestAdminV2DiagnosticsProbes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        admin_v2_diagnostics._PROBE_CACHE.update(checked_monotonic=0.0, result=None)

    async def test_http_probe_is_fixed_bounded_and_never_reads_or_returns_body(self):
        entered = False

        class Response:
            status_code = 204

            async def aread(self):
                raise AssertionError("Probe response body must not be read")

        class Stream:
            async def __aenter__(self):
                nonlocal entered
                entered = True
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, method, url):
                self.call = (method, url)
                return Stream()

        client = Client(timeout=2.0, follow_redirects=False, trust_env=False)
        with patch("httpx.AsyncClient", return_value=client):
            result = await admin_v2_diagnostics._http_probe(
                "http://127.0.0.1:8090/admin/health/live",
                service="resolver", label="Resolver",
            )

        self.assertTrue(entered)
        self.assertEqual(client.call, ("GET", "http://127.0.0.1:8090/admin/health/live"))
        self.assertEqual(client.kwargs, {"timeout": 2.0, "follow_redirects": False, "trust_env": False})
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["http_status"], 204)
        self.assertNotIn("url", result)
        self.assertNotIn("body", result)

    async def test_jellyfin_probe_uses_only_configured_public_endpoint_and_rejects_credentials(self):
        class Response:
            status_code = 200

        class Stream:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def stream(self, method, url):
                observed.append((method, url))
                return Stream()

        observed = []
        with patch.dict("os.environ", {"JELLYFIN_URL": "https://jellyfin.example/base"}), \
                patch("httpx.AsyncClient", Client):
            valid = await admin_v2_diagnostics._configured_jellyfin_probe()
        self.assertEqual(valid["status"], "healthy")
        self.assertEqual(observed, [("GET", "https://jellyfin.example/base/System/Info/Public")])

        for value in (
            "", "https://user:secret@jellyfin.example", "https://jellyfin.example/?token=secret",
            "file:///etc/passwd", "not a url",
        ):
            with self.subTest(value=value), \
                    patch.dict("os.environ", {"JELLYFIN_URL": value}), \
                    patch("httpx.AsyncClient", side_effect=AssertionError("invalid target must not be probed")):
                result = await admin_v2_diagnostics._configured_jellyfin_probe()
                self.assertIn(result["status"], {"not_configured", "unknown"})
        self.assertEqual(len(observed), 1)

    async def test_database_probe_has_connection_and_statement_timeouts_and_sanitizes_errors(self):
        async def run_immediately(func, *args, **kwargs):
            return func(*args, **kwargs)

        conn = Mock()
        conn.execute.side_effect = [Mock(), SimpleNamespace(fetchone=lambda: (1,))]
        conn.close = Mock()
        connect = Mock(return_value=conn)
        with patch.object(db_module, "connect", connect), \
                patch.object(admin_v2_diagnostics.asyncio, "to_thread", run_immediately):
            result = await admin_v2_diagnostics._database_probe(make_admin())
        self.assertEqual(result["status"], "healthy")
        connect.assert_called_once_with("postgresql://example.invalid/admin", connect_timeout=2)
        self.assertEqual(conn.execute.call_args_list[0].args[0], "SET statement_timeout = '1500ms'")
        self.assertEqual(conn.execute.call_args_list[1].args[0], "SELECT 1")
        conn.close.assert_called_once_with()

        connect.side_effect = RuntimeError("postgresql://user:secret@db/private")
        with patch.object(db_module, "connect", connect), \
                patch.object(admin_v2_diagnostics.asyncio, "to_thread", run_immediately):
            failed = await admin_v2_diagnostics._database_probe(make_admin())
        self.assertEqual(failed["status"], "unavailable")
        self.assertNotIn("secret", str(failed))

    async def test_endpoint_is_local_only_caches_probe_burst_and_returns_fixed_shape(self):
        router = admin_v2_diagnostics.build_router(make_admin())
        endpoint = next(route.endpoint for route in router.routes
                        if route.path == "/api/v2/diagnostics/probes")
        expected = {"services": [{"service": "postgresql", "label": "PostgreSQL", "status": "healthy"}]}
        run = AsyncMock(return_value=expected)
        with patch.object(admin_v2_diagnostics, "_run_service_probes", run):
            first = await endpoint(request())
            second = await endpoint(request())

        self.assertEqual(first["ok"], True)
        self.assertEqual(first["services"], expected["services"])
        self.assertEqual(first["cache_age_seconds"], 0)
        self.assertEqual(second["services"], expected["services"])
        self.assertGreaterEqual(second["cache_age_seconds"], 0)
        self.assertEqual(run.await_count, 1)

        blocked = await next(route.endpoint for route in admin_v2_diagnostics.build_router(
            make_admin(blocked=True)).routes if route.path == "/api/v2/diagnostics/probes")(request())
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(run.await_count, 1)

    async def test_failed_probe_batch_is_cached_instead_of_retrying_on_every_request(self):
        endpoint = next(route.endpoint for route in admin_v2_diagnostics.build_router(
            make_admin()).routes if route.path == "/api/v2/diagnostics/probes")
        run = AsyncMock(side_effect=RuntimeError("private probe failure"))
        with patch.object(admin_v2_diagnostics, "_run_service_probes", run):
            first = await endpoint(request())
            second = await endpoint(request())

        self.assertEqual(run.await_count, 1)
        self.assertEqual(first["services"], second["services"])
        self.assertTrue(all(item["status"] == "unknown" for item in first["services"]))
        self.assertNotIn("private", str(first))


if __name__ == "__main__":
    unittest.main()
