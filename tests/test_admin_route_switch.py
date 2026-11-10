"""Only Admin v2 remains mounted as an administrator UI/API."""

import asyncio
import unittest
from unittest.mock import patch

import httpx


import resolver.admin as admin_module
import resolver.app as resolver_app


class AdminRouteTests(unittest.TestCase):
    def request(self, path, method="GET"):
        async def send():
            transport = httpx.ASGITransport(app=resolver_app.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost",
                                         follow_redirects=False) as client:
                return await client.request(method, path)
        return asyncio.run(send())

    def test_root_and_direct_admin_route_serve_v2(self):
        with patch.object(admin_module, "ADMIN_PUBLIC", False), \
             patch.object(admin_module, "ENABLE_ADMIN", True):
            root = self.request("/admin/")
            direct = self.request("/admin/v2/")

        for response in (root, direct):
            self.assertEqual(response.status_code, 200)
            self.assertIn('id="main-content"', response.text)
            self.assertIn("/admin/assets-v2/admin-v2.css", response.text)
            self.assertNotIn("/admin/assets/admin.css", response.text)

    def test_classic_url_is_removed(self):
        response = self.request("/admin/classic/")
        self.assertEqual(response.status_code, 404)

    def test_retired_classic_url_stays_absent_when_admin_is_disabled(self):
        with patch.object(admin_module, "ADMIN_PUBLIC", False), \
             patch.object(admin_module, "ENABLE_ADMIN", False):
            responses = [self.request(path) for path in ("/admin/", "/admin/v2/")]

        self.assertEqual([response.status_code for response in responses], [403, 403])
        self.assertEqual(self.request("/admin/classic/").status_code, 404)

    def test_legacy_admin_api_routes_are_removed(self):
        with patch.object(admin_module, "ADMIN_PUBLIC", False), \
             patch.object(admin_module, "ENABLE_ADMIN", True):
            paths = ("/admin/api/status", "/admin/api/sources", "/admin/api/jobs",
                     "/admin/api/performers/catalog", "/admin/api/library/unavailable",
                     "/admin/api/playback-diagnostics/status")
            responses = [self.request(path) for path in paths]
            mutation = self.request("/admin/api/cache/clear", method="POST")
        self.assertEqual([response.status_code for response in responses], [404] * len(paths))
        self.assertEqual(mutation.status_code, 404)
        self.assertIsNone(responses[0].headers.get("cache-control"))

    def test_classic_assets_are_no_longer_mounted(self):
        response = self.request("/admin/assets/admin.css")
        self.assertEqual(response.status_code, 404)

    def test_admin_v2_assets_are_never_cached(self):
        response = self.request("/admin/assets-v2/quality.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertIn("Metadata suggestions", response.text)
        self.assertIn("quality/proposals", response.text)

    def test_admin_v2_api_responses_are_never_cached(self):
        with patch.object(admin_module, "ADMIN_PUBLIC", False), \
             patch.object(admin_module, "ENABLE_ADMIN", True):
            success = self.request("/admin/api/v2/capabilities")
            missing = self.request("/admin/api/v2/no-such-route")

        self.assertEqual(success.status_code, 200)
        self.assertEqual(missing.status_code, 404)
        for response in (success, missing):
            self.assertEqual(response.headers.get("cache-control"), "no-store")
