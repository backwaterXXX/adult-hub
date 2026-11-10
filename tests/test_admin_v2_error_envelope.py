"""Database-free checks for the Admin v2 request-validation contract."""

import asyncio
import unittest

import httpx

import resolver.app as resolver_app


class TestAdminV2ErrorEnvelope(unittest.TestCase):
    def request(self, path, *, raise_app_exceptions=True):
        async def send():
            transport = httpx.ASGITransport(
                app=resolver_app.app, raise_app_exceptions=raise_app_exceptions
            )
            async with httpx.AsyncClient(
                transport=transport, base_url="http://localhost"
            ) as client:
                return await client.get(path)

        return asyncio.run(send())

    def test_framework_validation_error_uses_v2_envelope(self):
        response = self.request("/admin/api/v2/jobs?limit=0")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["ok"], False)
        error = response.json()["error"]
        self.assertEqual(error["code"], "invalid_request")
        self.assertEqual(error["message"], "Invalid request parameters.")
        self.assertIs(error["retryable"], False)
        self.assertEqual(set(error), {"code", "message", "retryable", "correlation_id"})
        self.assertEqual(response.headers["X-Correlation-ID"], error["correlation_id"])

    def test_framework_validation_error_keeps_legacy_v1_shape(self):
        response = self.request("/admin/play/test/stream?mode=invalid")

        self.assertEqual(response.status_code, 422)
        self.assertIn("detail", response.json())
        self.assertNotIn("error", response.json())

    def test_uncaught_v2_exception_is_redacted_and_legacy_500_stays_plain(self):
        from resolver.admin_v2_errors import V2ErrorRoute

        async def unexpected_failure():
            raise RuntimeError("private path /srv/media and token=secret")

        original_route_class = resolver_app.app.router.route_class
        resolver_app.app.router.route_class = V2ErrorRoute
        try:
            resolver_app.app.add_api_route(
                "/admin/api/v2/test/unexpected-failure", unexpected_failure,
                methods=["GET"], include_in_schema=False,
            )
        finally:
            resolver_app.app.router.route_class = original_route_class
        resolver_app.app.add_api_route(
            "/admin/api/test/unexpected-failure", unexpected_failure,
            methods=["GET"], include_in_schema=False,
        )
        routes = [route for route in resolver_app.app.router.routes
                  if getattr(route, "path", "").endswith("/test/unexpected-failure")]
        try:
            v2 = self.request(
                "/admin/api/v2/test/unexpected-failure", raise_app_exceptions=False
            )
            v1 = self.request(
                "/admin/api/test/unexpected-failure", raise_app_exceptions=False
            )
        finally:
            for route in routes:
                resolver_app.app.router.routes.remove(route)

        self.assertEqual(v2.status_code, 500)
        self.assertEqual(v2.json()["error"]["code"], "request_failed")
        self.assertEqual(v2.json()["error"]["message"], "The request could not be completed.")
        self.assertIs(v2.json()["error"]["retryable"], True)
        self.assertEqual(v2.headers["X-Correlation-ID"], v2.json()["error"]["correlation_id"])
        self.assertNotIn("private path", v2.text)
        self.assertNotIn("secret", v2.text)
        self.assertEqual(v1.status_code, 500)
        self.assertEqual(v1.text, "Internal Server Error")


if __name__ == "__main__":
    unittest.main()
