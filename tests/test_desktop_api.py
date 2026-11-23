"""Targeted tests for Desktop-first API integration slice.

Covers:
- Offline unit tests:
  - Honest version & capability discovery for Desktop (/api/v1/version)
  - TV pairing routes strictly excluded from Desktop app (no pair/init, pair/status, approve)
  - Query parameter rejection on auth endpoints
  - Non-dict JSON payload rejection on auth endpoints
  - Pre-auth bounded body reads (rejection of oversized payloads > 32KB)
  - Typed string validation on auth inputs (rejection of non-string types)
  - Malformed revoke handling with uniform error and no-store caching
  - Isolated factory isolation (no implicit fallback to production database)
  - Mandatory Bearer access token authorization on all catalog endpoints
  - Input bounds and NUL-byte rejection on catalog query parameters
  - Protected route validation errors strictly enforce Cache-Control: private, no-store
  - Production Resolver containment smoke (routes disabled by default on resolver.app, no legacy shadow)
  - Explicit opt-in staging mount mechanism
- Live integration tests against disposable PostgreSQL (PgTestCase):
  - Owner password login, response allowlisting (strictly NO is_admin), private caching
  - Single-use refresh token rotation and replay revocation
  - Authenticated catalog browsing with filtering (studio, min_duration, sort) and detail lookups
  - Correct catalog video fixtures with date_released, first_seen timezone, and provider
  - Nonexistent video/channel 404 handling with private, no-store caching
  - Revoked, disabled, and expired token rejection (401 Unauthorized)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from common import db
from common.app_version import __version__ as APP_VERSION
from common.client_auth.service import _safe_hash_token, bootstrap_owner
from resolver.app import app as resolver_production_app
from resolver.desktop_api import (
    create_desktop_integration_app,
    mount_desktop_api,
)
from tests.pg_helper import PgTestCase

desktop_test_app = create_desktop_integration_app()


class TestDesktopApiOffline(unittest.TestCase):
    """Offline unit tests executable in hermetic sandboxes without database access."""

    def test_desktop_version_discovery_advertises_honest_capabilities(self):
        """GET /api/v1/version advertises desktop capabilities and strictly disables TV pairing."""
        client = TestClient(desktop_test_app)
        res = client.get("/api/v1/version")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("cache-control"), "public, max-age=300")

        data = res.json()
        self.assertEqual(data["api_version"], "1.0.0")
        self.assertEqual(data["server_version"], APP_VERSION)
        self.assertEqual(data["status"], "ok")

        caps = data.get("capabilities", {})
        self.assertTrue(caps.get("desktop_api"))
        self.assertFalse(caps.get("tv_pairing"))
        self.assertTrue(caps.get("direct_play"))
        self.assertFalse(caps.get("user_state"))
        self.assertFalse(caps.get("transcoding"))

        endpoints = data.get("endpoints", {})
        self.assertEqual(endpoints.get("auth"), "/api/v1/auth")
        self.assertEqual(endpoints.get("catalog"), "/api/v1/catalog")
        self.assertEqual(endpoints.get("channels"), "/api/v1/catalog/channels")
        self.assertEqual(endpoints.get("playback"), "/api/v1/playback")
        self.assertEqual(endpoints.get("artwork"), "/api/v1/artwork")
        self.assertNotIn("pair", endpoints)

    def test_pairing_routes_strictly_not_mounted_on_desktop_app(self):
        """Desktop app excludes all TV pairing endpoints and Admin V2 approval."""
        client = TestClient(desktop_test_app)
        self.assertEqual(client.post("/api/v1/auth/pair/init").status_code, 404)
        self.assertEqual(client.post("/api/v1/auth/pair/status").status_code, 404)
        self.assertEqual(client.post("/admin/api/v2/pairing/approve").status_code, 404)

    def test_auth_endpoints_reject_query_parameters(self):
        """Authentication endpoints return 400 if secrets or parameters appear in query string."""
        client = TestClient(desktop_test_app)
        res_login = client.post("/api/v1/auth/login?username=admin", json={"username": "admin", "password": "pwd"})
        self.assertEqual(res_login.status_code, 400)
        self.assertEqual(res_login.headers.get("cache-control"), "private, no-store")
        self.assertIn("Query parameters are strictly prohibited", res_login.json()["error"])

        res_refresh = client.post("/api/v1/auth/refresh?token=secret", json={"refresh_token": "ah_ref_test"})
        self.assertEqual(res_refresh.status_code, 400)
        self.assertEqual(res_refresh.headers.get("cache-control"), "private, no-store")
        self.assertIn("Query parameters are strictly prohibited", res_refresh.json()["error"])

        res_revoke = client.post("/api/v1/auth/revoke?token=secret", json={"refresh_token": "ah_ref_test"})
        self.assertEqual(res_revoke.status_code, 400)
        self.assertEqual(res_revoke.headers.get("cache-control"), "private, no-store")
        self.assertIn("Query parameters are strictly prohibited", res_revoke.json()["error"])

    def test_auth_endpoints_reject_non_dict_json_payloads(self):
        """Authentication endpoints return 400 if JSON body is not a dict."""
        client = TestClient(desktop_test_app)
        for bad_payload in (["username", "password"], "string_payload", 12345):
            res_login = client.post("/api/v1/auth/login", json=bad_payload)
            self.assertEqual(res_login.status_code, 400)
            self.assertEqual(res_login.headers.get("cache-control"), "private, no-store")
            self.assertIn("JSON object", res_login.json()["error"])

            res_refresh = client.post("/api/v1/auth/refresh", json=bad_payload)
            self.assertEqual(res_refresh.status_code, 400)
            self.assertEqual(res_refresh.headers.get("cache-control"), "private, no-store")

            res_revoke = client.post("/api/v1/auth/revoke", json=bad_payload)
            self.assertEqual(res_revoke.status_code, 400)
            self.assertEqual(res_revoke.headers.get("cache-control"), "private, no-store")

    def test_pre_auth_bounded_body_rejection(self):
        """Pre-auth endpoints reject payloads exceeding 32 KB and empty bodies with 400 and private no-store."""
        client = TestClient(desktop_test_app)
        oversized_str = "x" * (33 * 1024)
        oversized_payload = {"username": "admin", "password": oversized_str}

        res_login = client.post("/api/v1/auth/login", json=oversized_payload)
        self.assertEqual(res_login.status_code, 400)
        self.assertEqual(res_login.headers.get("cache-control"), "private, no-store")
        self.assertIn("exceeds maximum size", res_login.json()["error"])

        res_refresh = client.post("/api/v1/auth/refresh", json={"refresh_token": oversized_str})
        self.assertEqual(res_refresh.status_code, 400)
        self.assertEqual(res_refresh.headers.get("cache-control"), "private, no-store")
        self.assertIn("exceeds maximum size", res_refresh.json()["error"])

        res_revoke = client.post("/api/v1/auth/revoke", json={"refresh_token": oversized_str})
        self.assertEqual(res_revoke.status_code, 400)
        self.assertEqual(res_revoke.headers.get("cache-control"), "private, no-store")
        self.assertIn("exceeds maximum size", res_revoke.json()["error"])

        # Empty body
        res_empty = client.post("/api/v1/auth/login", content=b"")
        self.assertEqual(res_empty.status_code, 400)
        self.assertEqual(res_empty.headers.get("cache-control"), "private, no-store")

    def test_auth_endpoints_enforce_typed_strings(self):
        """Auth endpoints reject non-string typed inputs for username, password, and refresh_token."""
        client = TestClient(desktop_test_app)

        # Integer username
        res1 = client.post("/api/v1/auth/login", json={"username": 12345, "password": "valid_password"})
        self.assertEqual(res1.status_code, 400)
        self.assertEqual(res1.headers.get("cache-control"), "private, no-store")
        self.assertIn("must be a string", res1.json()["error"])

        # Boolean password
        res2 = client.post("/api/v1/auth/login", json={"username": "valid_user", "password": True})
        self.assertEqual(res2.status_code, 400)
        self.assertEqual(res2.headers.get("cache-control"), "private, no-store")
        self.assertIn("must be a string", res2.json()["error"])

        # Integer refresh_token on refresh
        res3 = client.post("/api/v1/auth/refresh", json={"refresh_token": 9999})
        self.assertEqual(res3.status_code, 400)
        self.assertEqual(res3.headers.get("cache-control"), "private, no-store")
        self.assertIn("must be a string", res3.json()["error"])

        # List refresh_token on revoke
        res4 = client.post("/api/v1/auth/revoke", json={"refresh_token": ["token1", "token2"]})
        self.assertEqual(res4.status_code, 400)
        self.assertEqual(res4.headers.get("cache-control"), "private, no-store")
        self.assertIn("must be a string", res4.json()["error"])

    def test_revoke_failures_and_malformed_payloads(self):
        """Revoke handles missing, empty, or malformed tokens with no-store uniform errors without sensitive leaks."""
        client = TestClient(desktop_test_app)

        # Missing token field
        res1 = client.post("/api/v1/auth/revoke", json={})
        self.assertEqual(res1.status_code, 400)
        self.assertEqual(res1.headers.get("cache-control"), "private, no-store")
        self.assertIn("refresh_token is required", res1.json()["error"])

        # Empty string token
        res2 = client.post("/api/v1/auth/revoke", json={"refresh_token": ""})
        self.assertEqual(res2.status_code, 400)
        self.assertEqual(res2.headers.get("cache-control"), "private, no-store")
        self.assertIn("refresh_token is required", res2.json()["error"])

        # Token with invalid format / NUL bytes
        res3 = client.post("/api/v1/auth/revoke", json={"refresh_token": "ah_ref_\x00bad"})
        self.assertEqual(res3.status_code, 400)
        self.assertEqual(res3.headers.get("cache-control"), "private, no-store")
        self.assertIn("null", res3.json()["error"].lower())

    def test_isolated_factory_prevents_implicit_production_db(self):
        """Isolated app factory without db_url does not implicitly connect to production DB."""
        isolated_app = create_desktop_integration_app()
        client = TestClient(isolated_app)

        # Login attempts DB connection; should fail safely with 500 without reaching production
        res = client.post(
            "/api/v1/auth/login",
            json={"username": "test_user", "password": "test_password"},
        )
        self.assertEqual(res.status_code, 500)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")
        self.assertEqual(res.json(), {"error": "Authentication failed"})

    def test_catalog_endpoints_require_bearer_authorization(self):
        """All catalog endpoints return 401 Unauthorized when Bearer token is missing or malformed."""
        client = TestClient(desktop_test_app)
        endpoints = [
            ("GET", "/api/v1/catalog/videos"),
            ("GET", "/api/v1/catalog/videos/vid-1"),
            ("GET", "/api/v1/catalog/channels"),
            ("GET", "/api/v1/catalog/channels/chan-1"),
        ]
        for method, path in endpoints:
            # Missing header
            res1 = client.request(method, path)
            self.assertEqual(res1.status_code, 401, f"{path} should return 401 on missing auth")
            self.assertEqual(res1.headers.get("cache-control"), "private, no-store")

            # Non-Bearer scheme
            res2 = client.request(method, path, headers={"Authorization": "Basic dXNlcjpwYXNz"})
            self.assertEqual(res2.status_code, 401, f"{path} should return 401 on Basic auth")
            self.assertEqual(res2.headers.get("cache-control"), "private, no-store")

            # Empty Bearer token
            res3 = client.request(method, path, headers={"Authorization": "Bearer "})
            self.assertEqual(res3.status_code, 401, f"{path} should return 401 on empty Bearer")
            self.assertEqual(res3.headers.get("cache-control"), "private, no-store")

    def test_catalog_parameter_bounds_and_nul_validation(self):
        """Catalog query parameters enforce bounds, reject NUL bytes, and return Cache-Control: private, no-store."""
        client = TestClient(desktop_test_app)
        fake_auth = {"Authorization": "Bearer ah_tok_dummy_offline_token"}

        # Negative offset returns 422 with no-store
        res = client.get("/api/v1/catalog/videos?offset=-5", headers=fake_auth)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")

        # Limit 0 returns 422 with no-store
        res = client.get("/api/v1/catalog/videos?limit=0", headers=fake_auth)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")

        # Limit > 100 returns 422 with no-store
        res = client.get("/api/v1/catalog/videos?limit=150", headers=fake_auth)
        self.assertEqual(res.status_code, 422)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")

        # Unauthenticated request with bad parameter returns 401 first
        res_unauth = client.get("/api/v1/catalog/videos?sort=unknown_sort")
        self.assertEqual(res_unauth.status_code, 401)
        self.assertEqual(res_unauth.headers.get("cache-control"), "private, no-store")

        with patch("resolver.desktop_api._db_connection"), patch("resolver.desktop_api._verify_client_session", return_value={"user_id": "usr_1"}):
            # Invalid sort on videos returns 400 with no-store
            res = client.get("/api/v1/catalog/videos?sort=unknown_sort", headers=fake_auth)
            self.assertEqual(res.status_code, 400)
            self.assertEqual(res.headers.get("cache-control"), "private, no-store")
            self.assertIn("Invalid sort parameter", res.json()["error"])

            # Invalid direction on videos returns 400 with no-store
            res = client.get("/api/v1/catalog/videos?direction=invalid_dir", headers=fake_auth)
            self.assertEqual(res.status_code, 400)
            self.assertEqual(res.headers.get("cache-control"), "private, no-store")
            self.assertIn("Invalid direction parameter", res.json()["error"])

            # Invalid sort on channels returns 400 with no-store
            res = client.get("/api/v1/catalog/channels?sort=invalid_channel_sort", headers=fake_auth)
            self.assertEqual(res.status_code, 400)
            self.assertEqual(res.headers.get("cache-control"), "private, no-store")
            self.assertIn("Invalid sort parameter", res.json()["error"])

            # NUL byte in search parameter returns 400 with no-store
            res = client.get("/api/v1/catalog/videos?q=test%00evil", headers=fake_auth)
            self.assertEqual(res.status_code, 400)
            self.assertEqual(res.headers.get("cache-control"), "private, no-store")
            self.assertIn("NUL bytes", res.json()["error"])

            # NUL byte in channel_id parameter returns 400 with no-store
            res = client.get("/api/v1/catalog/channels/chan%00evil", headers=fake_auth)
            self.assertEqual(res.status_code, 400)
            self.assertEqual(res.headers.get("cache-control"), "private, no-store")
            self.assertIn("NUL bytes", res.json()["error"])

    def test_playback_endpoints_require_bearer_authorization(self):
        """All playback endpoints reject requests lacking valid Bearer auth with 401 and private no-store."""
        client = TestClient(desktop_test_app)

        for endpoint, method in (
            ("/api/v1/playback/capabilities", "GET"),
            ("/api/v1/playback/renditions/telegram:vid-1", "GET"),
            ("/api/v1/playback/telegram:vid-1", "GET"),
            ("/api/v1/playback/stream/telegram:vid-1", "GET"),
            ("/api/v1/playback/stream/telegram:vid-1", "HEAD"),
        ):
            # Missing header
            res1 = client.request(method, endpoint)
            self.assertEqual(res1.status_code, 401, f"{method} {endpoint} should return 401 on missing auth")
            self.assertEqual(res1.headers.get("cache-control"), "private, no-store")

            # Non-Bearer auth
            res2 = client.request(method, endpoint, headers={"Authorization": "Basic dXNlcjpwd2Q="})
            self.assertEqual(res2.status_code, 401, f"{method} {endpoint} should return 401 on non-Bearer auth")
            self.assertEqual(res2.headers.get("cache-control"), "private, no-store")

            # Empty token
            res3 = client.request(method, endpoint, headers={"Authorization": "Bearer   "})
            self.assertEqual(res3.status_code, 401, f"{method} {endpoint} should return 401 on empty Bearer token")
            self.assertEqual(res3.headers.get("cache-control"), "private, no-store")

    def test_watch_progress_endpoints_require_bearer_authorization_and_validate_inputs(self):
        """Watch progress endpoints strictly require Bearer authorization and reject malformed inputs."""
        client = TestClient(desktop_test_app)

        for endpoint, method in (
            ("/api/v1/user/watch/telegram:vid-1", "GET"),
            ("/api/v1/user/watch/telegram:vid-1", "PUT"),
            ("/api/v1/user/watch/telegram:vid-1", "DELETE"),
            ("/api/v1/me/progress/telegram:vid-1", "GET"),
            ("/api/v1/me/progress/telegram:vid-1", "PUT"),
            ("/api/v1/me/progress/telegram:vid-1", "DELETE"),
        ):
            # Missing auth header
            res1 = client.request(method, endpoint, json={"position_seconds": 10, "duration_seconds": 100, "is_played": False})
            self.assertEqual(res1.status_code, 401)
            self.assertEqual(res1.headers.get("cache-control"), "private, no-store")

            # Non-Bearer auth
            res2 = client.request(method, endpoint, headers={"Authorization": "Basic bad"}, json={"position_seconds": 10, "duration_seconds": 100, "is_played": False})
            self.assertEqual(res2.status_code, 401)
            self.assertEqual(res2.headers.get("cache-control"), "private, no-store")

        fake_auth = {"Authorization": "Bearer ah_tok_dummy_fake"}

        # Non-dict JSON payload on PUT
        for bad_payload in (["bad"], "string", 1234):
            res_bad = client.put("/api/v1/user/watch/telegram:vid-1", json=bad_payload, headers=fake_auth)
            self.assertEqual(res_bad.status_code, 400)
            self.assertEqual(res_bad.headers.get("cache-control"), "private, no-store")

        # Invalid field types on PUT
        for invalid_body in (
            {"position_seconds": -1, "duration_seconds": 100, "is_played": False},
            {"position_seconds": "not_an_int", "duration_seconds": 100, "is_played": False},
            {"position_seconds": 10, "duration_seconds": -5, "is_played": False},
            {"position_seconds": 10, "duration_seconds": "not_an_int", "is_played": False},
            {"position_seconds": 10, "duration_seconds": 100, "is_played": "not_a_bool"},
            {"position_seconds": 10, "duration_seconds": 100, "is_played": False, "played_percentage": 105.0},
            {"position_seconds": 10, "duration_seconds": 100, "is_played": False, "played_percentage": -1.0},
            {"position_seconds": 10, "duration_seconds": 100, "is_played": False, "last_watched_at": "invalid_date"},
        ):
            res_val = client.put("/api/v1/user/watch/telegram:vid-1", json=invalid_body, headers=fake_auth)
            self.assertEqual(res_val.status_code, 400)
            self.assertEqual(res_val.headers.get("cache-control"), "private, no-store")

        # NUL byte in video_id returns 400
        res_nul = client.get("/api/v1/user/watch/telegram:vid%00bad", headers=fake_auth)
        self.assertEqual(res_nul.status_code, 400)
        self.assertEqual(res_nul.headers.get("cache-control"), "private, no-store")

    def test_favorites_endpoints_require_bearer_authorization_and_validate_inputs(self):
        """Favorites endpoints strictly require Bearer authorization and reject malformed inputs."""
        client = TestClient(desktop_test_app)

        for endpoint, method in (
            ("/api/v1/user/favorites", "GET"),
            ("/api/v1/user/favorites/telegram:vid-1", "GET"),
            ("/api/v1/user/favorites/telegram:vid-1", "PUT"),
            ("/api/v1/user/favorites/telegram:vid-1", "POST"),
            ("/api/v1/user/favorites/telegram:vid-1", "DELETE"),
        ):
            # Missing auth header
            res1 = client.request(method, endpoint)
            self.assertEqual(res1.status_code, 401, f"{method} {endpoint} should return 401 when missing auth")
            self.assertEqual(res1.headers.get("cache-control"), "private, no-store")

            # Non-Bearer auth
            res2 = client.request(method, endpoint, headers={"Authorization": "Basic bad"})
            self.assertEqual(res2.status_code, 401, f"{method} {endpoint} should return 401 for non-Bearer auth")
            self.assertEqual(res2.headers.get("cache-control"), "private, no-store")

            # Empty Bearer token
            res3 = client.request(method, endpoint, headers={"Authorization": "Bearer   "})
            self.assertEqual(res3.status_code, 401, f"{method} {endpoint} should return 401 for empty Bearer token")
            self.assertEqual(res3.headers.get("cache-control"), "private, no-store")

        fake_auth = {"Authorization": "Bearer ah_tok_dummy_fake"}

        # NUL byte in video_id returns 400
        for method in ("GET", "PUT", "DELETE"):
            res_nul = client.request(method, "/api/v1/user/favorites/telegram:vid%00bad", headers=fake_auth)
            self.assertEqual(res_nul.status_code, 400)
            self.assertEqual(res_nul.headers.get("cache-control"), "private, no-store")

        # Invalid sort parameter
        res_bad_sort = client.get("/api/v1/user/favorites?sort=invalid_sort", headers=fake_auth)
        self.assertEqual(res_bad_sort.status_code, 400)
        self.assertEqual(res_bad_sort.headers.get("cache-control"), "private, no-store")

        # Invalid direction parameter
        res_bad_dir = client.get("/api/v1/user/favorites?direction=sideways", headers=fake_auth)
        self.assertEqual(res_bad_dir.status_code, 400)
        self.assertEqual(res_bad_dir.headers.get("cache-control"), "private, no-store")

    def test_taxonomy_endpoints_require_bearer_authorization_and_validate_inputs(self):
        """Taxonomy endpoints (categories, studios, tags, performers) strictly require bearer authorization and validate bounds."""
        client = TestClient(desktop_test_app)

        endpoints = [
            "/api/v1/catalog/categories",
            "/api/v1/catalog/studios",
            "/api/v1/catalog/tags",
            "/api/v1/catalog/performers",
            "/api/v1/catalog/performers/performer-1",
        ]

        # 1. Require bearer authorization
        for path in endpoints:
            res_no_auth = client.get(path)
            self.assertEqual(res_no_auth.status_code, 401, f"{path} did not reject unauthenticated request with 401")
            self.assertEqual(res_no_auth.headers.get("cache-control"), "private, no-store")

            res_bad_auth = client.get(path, headers={"Authorization": "Basic viewer:secret"})
            self.assertEqual(res_bad_auth.status_code, 401, f"{path} did not reject non-bearer auth with 401")
            self.assertEqual(res_bad_auth.headers.get("cache-control"), "private, no-store")

        fake_auth = {"Authorization": "Bearer ah_tok_test_dummy"}

        # Ensure unauthenticated request or missing/invalid token returns 401 BEFORE parameter validation (400)
        res_unauth_bad_sort = client.get("/api/v1/catalog/performers?sort=invalid_sort")
        self.assertEqual(res_unauth_bad_sort.status_code, 401)
        self.assertEqual(res_unauth_bad_sort.headers.get("cache-control"), "private, no-store")

        res_unauth_bad_id = client.get("/api/v1/catalog/performers/performer%00bad")
        self.assertEqual(res_unauth_bad_id.status_code, 401)
        self.assertEqual(res_unauth_bad_id.headers.get("cache-control"), "private, no-store")

        with patch("resolver.desktop_api._db_connection"), patch("resolver.desktop_api._verify_client_session", return_value={"user_id": "usr_1"}):
            # 2. Reject negative offset / zero limit / excessive limit
            for path in ["/api/v1/catalog/categories", "/api/v1/catalog/studios", "/api/v1/catalog/tags", "/api/v1/catalog/performers"]:
                res_neg_offset = client.get(f"{path}?offset=-1", headers=fake_auth)
                self.assertIn(res_neg_offset.status_code, (400, 422))
                self.assertEqual(res_neg_offset.headers.get("cache-control"), "private, no-store")

                res_zero_limit = client.get(f"{path}?limit=0", headers=fake_auth)
                self.assertIn(res_zero_limit.status_code, (400, 422))
                self.assertEqual(res_zero_limit.headers.get("cache-control"), "private, no-store")

                res_over_limit = client.get(f"{path}?limit=500", headers=fake_auth)
                self.assertIn(res_over_limit.status_code, (400, 422))
                self.assertEqual(res_over_limit.headers.get("cache-control"), "private, no-store")

            # 3. Reject invalid sort and direction on performers
            res_bad_sort = client.get("/api/v1/catalog/performers?sort=invalid_sort", headers=fake_auth)
            self.assertEqual(res_bad_sort.status_code, 400)
            self.assertEqual(res_bad_sort.headers.get("cache-control"), "private, no-store")

            res_bad_dir = client.get("/api/v1/catalog/performers?direction=upwards", headers=fake_auth)
            self.assertEqual(res_bad_dir.status_code, 400)
            self.assertEqual(res_bad_dir.headers.get("cache-control"), "private, no-store")

            # 4. Reject NUL bytes in performer_id and filters
            res_nul = client.get("/api/v1/catalog/performers/performer%00bad", headers=fake_auth)
            self.assertEqual(res_nul.status_code, 400)
            self.assertEqual(res_nul.headers.get("cache-control"), "private, no-store")

            res_nul_q = client.get("/api/v1/catalog/categories?q=test%00bad", headers=fake_auth)
            self.assertEqual(res_nul_q.status_code, 400)
            self.assertEqual(res_nul_q.headers.get("cache-control"), "private, no-store")


    def test_playback_endpoints_reject_unexpected_query_parameters_and_nul(self):
        """Playback endpoints strictly forbid unapproved query parameters and reject NUL bytes."""
        client = TestClient(desktop_test_app)
        auth = {"Authorization": "Bearer ah_tok_test_dummy"}

        # Capabilities with unexpected query params -> 400
        res = client.get("/api/v1/playback/capabilities?foo=bar", headers=auth)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")

        # Renditions with unexpected query params -> 400
        res = client.get("/api/v1/playback/renditions/telegram:vid-1?foo=bar", headers=auth)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")

        # Stream with unexpected query params (only rendition_id is allowed) -> 400
        res = client.get("/api/v1/playback/stream/telegram:vid-1?mode=proxy", headers=auth)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")
        self.assertIn("Unexpected query parameter", res.json()["error"])

        # Stream with NUL byte in rendition_id -> 400
        res = client.get("/api/v1/playback/stream/telegram:vid-1?rendition_id=alt%00bad", headers=auth)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")
        self.assertIn("NUL bytes", res.json()["error"])

        # Stream with NUL byte in video_id -> 400
        res = client.get("/api/v1/playback/stream/telegram:vid%00bad", headers=auth)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")
        self.assertIn("NUL bytes", res.json()["error"])

    def test_playback_descriptor_allowlisting_and_zero_leaks(self):
        """Playback descriptor exposes only canonical ID, safe media metadata, safe variant choices, and same-origin stream path."""
        client = TestClient(desktop_test_app)
        auth = {"Authorization": "Bearer ah_tok_test_dummy"}

        class MockTelegramProvider:
            def is_channel_authorized(self, channel_ref: str) -> bool:
                return True

            async def get_video_variants(self, ref: str) -> dict:
                return {
                    "status": "ready",
                    "video_processing_pending": False,
                    "has_alternatives": True,
                    "variants": [
                        {
                            "id": "original",
                            "is_original": True,
                            "width": 1920,
                            "height": 1080,
                            "mime_type": "video/mp4",
                            "file_size": 1500000000,
                            "duration": 3600.0,
                        },
                        {
                            "id": "alt_888999",
                            "is_original": False,
                            "width": 1280,
                            "height": 720,
                            "mime_type": "video/mp4",
                            "file_size": 750000000,
                            "duration": 3600.0,
                        },
                    ],
                }

        mock_prov = MockTelegramProvider()

        with patch("resolver.desktop_api._db_connection"), \
             patch("resolver.desktop_api._verify_client_session", return_value={"user_id": "usr_1"}), \
             patch("resolver.desktop_api._get_stream_candidate_ids", return_value=["telegram:canonical_vid_1"]), \
             patch("resolver.desktop_api._is_video_visible_and_playable", return_value=True), \
             patch("resolver.providers.get_provider", return_value=mock_prov):

            res = client.get("/api/v1/playback/renditions/telegram:alias_loser_99", headers=auth)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.headers.get("cache-control"), "private, no-store")

            data = res.json()
            self.assertEqual(data["video_id"], "telegram:canonical_vid_1")
            self.assertEqual(data["stream_path"], "/api/v1/playback/stream/telegram:canonical_vid_1")
            self.assertIn("renditions", data)
            self.assertEqual(len(data["renditions"]), 2)

            orig = data["renditions"][0]
            self.assertEqual(orig["id"], "original")
            self.assertTrue(orig["is_default"])
            self.assertEqual(orig["stream_path"], "/api/v1/playback/stream/telegram:canonical_vid_1")
            self.assertEqual(orig["width"], 1920)
            self.assertEqual(orig["height"], 1080)

            alt = data["renditions"][1]
            self.assertEqual(alt["id"], "alt_888999")
            self.assertFalse(alt["is_default"])
            self.assertEqual(alt["stream_path"], "/api/v1/playback/stream/telegram:canonical_vid_1?rendition_id=alt_888999")
            self.assertEqual(alt["width"], 1280)
            self.assertEqual(alt["height"], 720)

            # Strict verification of zero sensitive leaks
            raw_text = res.text
            self.assertNotIn("tg://", raw_text)
            self.assertNotIn("t.me", raw_text)
            self.assertNotIn(".strm", raw_text)
            self.assertNotIn(".nfo", raw_text)
            self.assertNotIn("is_admin", raw_text)

    def test_playback_stream_dummy_bytes_range_and_head(self):
        """Direct Play streaming forwards original encoded bytes with proper 200, 206, 416 headers and cleanup."""
        client = TestClient(desktop_test_app)
        auth = {"Authorization": "Bearer ah_tok_test_dummy"}
        dummy_bytes = b"0123456789ABCDEF" * 64  # 1024 bytes total

        class DummyStreamProvider:
            def __init__(self):
                self.closed = False

            def is_channel_authorized(self, channel_ref: str) -> bool:
                return True

            async def file_size(self, ref: str) -> int:
                return len(dummy_bytes)

            async def resolve_stream(self, raw_id: str, variant: str | None = None) -> str:
                return f"tg://{raw_id}"

            async def download_range(self, ref: str, offset: int, length: int | None):
                end = (offset + length) if length is not None else len(dummy_bytes)
                slice_data = dummy_bytes[offset:end]

                async def _gen():
                    try:
                        yield slice_data
                    finally:
                        self.closed = True

                return _gen()

        prov = DummyStreamProvider()

        with patch("resolver.desktop_api._db_connection"), \
             patch("resolver.desktop_api._verify_client_session", return_value={"user_id": "usr_1"}), \
             patch("resolver.desktop_api._get_stream_candidate_ids", return_value=["telegram:cand_stream_1"]), \
             patch("resolver.desktop_api._is_video_visible_and_playable", return_value=True), \
             patch("resolver.providers.get_provider", return_value=prov):

            # 1. Full GET without range header (200 OK)
            res_full = client.get("/api/v1/playback/stream/telegram:cand_stream_1", headers=auth)
            self.assertEqual(res_full.status_code, 200)
            self.assertEqual(res_full.content, dummy_bytes)
            self.assertEqual(res_full.headers.get("accept-ranges"), "bytes")
            self.assertEqual(res_full.headers.get("content-length"), "1024")
            self.assertEqual(res_full.headers.get("cache-control"), "private, no-store")

            # 2. Bounded byte range GET (206 Partial Content)
            res_range = client.get(
                "/api/v1/playback/stream/telegram:cand_stream_1",
                headers={**auth, "Range": "bytes=0-15"},
            )
            self.assertEqual(res_range.status_code, 206)
            self.assertEqual(res_range.content, dummy_bytes[0:16])
            self.assertEqual(res_range.headers.get("content-range"), "bytes 0-15/1024")
            self.assertEqual(res_range.headers.get("content-length"), "16")
            self.assertEqual(res_range.headers.get("accept-ranges"), "bytes")
            self.assertEqual(res_range.headers.get("cache-control"), "private, no-store")

            # 3. Mid-file byte range GET (206 Partial Content)
            res_mid = client.get(
                "/api/v1/playback/stream/telegram:cand_stream_1",
                headers={**auth, "Range": "bytes=100-199"},
            )
            self.assertEqual(res_mid.status_code, 206)
            self.assertEqual(res_mid.content, dummy_bytes[100:200])
            self.assertEqual(res_mid.headers.get("content-range"), "bytes 100-199/1024")
            self.assertEqual(res_mid.headers.get("content-length"), "100")
            self.assertEqual(res_mid.headers.get("cache-control"), "private, no-store")

            # 4. Out-of-bounds byte range GET (416 Range Not Satisfiable)
            res_416 = client.get(
                "/api/v1/playback/stream/telegram:cand_stream_1",
                headers={**auth, "Range": "bytes=2000-3000"},
            )
            self.assertEqual(res_416.status_code, 416)
            self.assertEqual(res_416.headers.get("content-range"), "bytes */1024")
            self.assertEqual(res_416.headers.get("cache-control"), "private, no-store")

            # 5. Full HEAD request without range (200 OK with empty body)
            res_head = client.head("/api/v1/playback/stream/telegram:cand_stream_1", headers=auth)
            self.assertEqual(res_head.status_code, 200)
            self.assertEqual(res_head.content, b"")
            self.assertEqual(res_head.headers.get("content-length"), "1024")
            self.assertEqual(res_head.headers.get("accept-ranges"), "bytes")
            self.assertEqual(res_head.headers.get("cache-control"), "private, no-store")

            # 6. Range HEAD request (206 Partial Content with empty body)
            res_head_rng = client.head(
                "/api/v1/playback/stream/telegram:cand_stream_1",
                headers={**auth, "Range": "bytes=0-15"},
            )
            self.assertEqual(res_head_rng.status_code, 206)
            self.assertEqual(res_head_rng.content, b"")
            self.assertEqual(res_head_rng.headers.get("content-range"), "bytes 0-15/1024")
            self.assertEqual(res_head_rng.headers.get("content-length"), "16")
            self.assertEqual(res_head_rng.headers.get("accept-ranges"), "bytes")
            self.assertEqual(res_head_rng.headers.get("cache-control"), "private, no-store")

            # 7. Out-of-bounds Range HEAD request (416 with empty body)
            res_head_416 = client.head(
                "/api/v1/playback/stream/telegram:cand_stream_1",
                headers={**auth, "Range": "bytes=2000-3000"},
            )
            self.assertEqual(res_head_416.status_code, 416)
            self.assertEqual(res_head_416.content, b"")
            self.assertEqual(res_head_416.headers.get("content-range"), "bytes */1024")
            self.assertEqual(res_head_416.headers.get("cache-control"), "private, no-store")

    def test_playback_preplay_source_fallback(self):
        """Pre-play source fallback selects the next candidate when primary candidate fails before streaming."""
        client = TestClient(desktop_test_app)
        auth = {"Authorization": "Bearer ah_tok_test_dummy"}
        dummy_bytes = b"FALLBACK_SOURCE_BYTES_12345678"

        class FailingFirstProvider:
            def __init__(self):
                self.calls = []

            def is_channel_authorized(self, channel_ref: str) -> bool:
                return True

            async def file_size(self, ref: str) -> int:
                return len(dummy_bytes)

            async def resolve_stream(self, raw_id: str, variant: str | None = None) -> str:
                self.calls.append(raw_id)
                if "failing" in raw_id:
                    raise RuntimeError("Upstream preflight MTProto connection timed out")
                return f"tg://{raw_id}"

            async def download_range(self, ref: str, offset: int, length: int | None):
                async def _gen():
                    yield dummy_bytes

                return _gen()

        prov = FailingFirstProvider()

        with patch("resolver.desktop_api._db_connection"), \
             patch("resolver.desktop_api._verify_client_session", return_value={"user_id": "usr_1"}), \
             patch("resolver.desktop_api._get_stream_candidate_ids", return_value=["telegram:failing_source", "telegram:working_source"]), \
             patch("resolver.desktop_api._is_video_visible_and_playable", return_value=True), \
             patch("resolver.providers.get_provider", return_value=prov):

            res = client.get("/api/v1/playback/stream/telegram:requested_video", headers=auth)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.content, dummy_bytes)
            self.assertEqual(res.headers.get("cache-control"), "private, no-store")
            # Verify both candidates were attempted and working_source was selected
            self.assertIn("failing_source", prov.calls)
            self.assertIn("working_source", prov.calls)

    def test_playback_all_sources_fail_returns_502_actionable_error(self):
        """When all pre-play source candidates fail, returns actionable 502 with ProblemDetails."""
        client = TestClient(desktop_test_app)
        auth = {"Authorization": "Bearer ah_tok_test_dummy"}

        class AlwaysFailingProvider:
            def is_channel_authorized(self, channel_ref: str) -> bool:
                return True

            async def resolve_stream(self, raw_id: str, variant: str | None = None) -> str:
                raise RuntimeError("All MTProto DC connections refused")

        with patch("resolver.desktop_api._db_connection"), \
             patch("resolver.desktop_api._verify_client_session", return_value={"user_id": "usr_1"}), \
             patch("resolver.desktop_api._get_stream_candidate_ids", return_value=["telegram:fail_1", "telegram:fail_2"]), \
             patch("resolver.desktop_api._is_video_visible_and_playable", return_value=True), \
             patch("resolver.providers.get_provider", return_value=AlwaysFailingProvider()):

            res = client.get("/api/v1/playback/stream/telegram:requested_video", headers=auth)
            self.assertEqual(res.status_code, 502)
            self.assertEqual(res.headers.get("cache-control"), "private, no-store")

            prob = res.json()
            self.assertEqual(prob.get("status"), 502)
            self.assertEqual(prob.get("code"), "SOURCE_UNAVAILABLE")
            self.assertIn("could be reached", prob.get("detail", ""))

    def test_playback_stream_pinned_rendition(self):
        """Passing rendition_id pins the requested variant representation across range requests."""
        client = TestClient(desktop_test_app)
        auth = {"Authorization": "Bearer ah_tok_test_dummy"}
        dummy_alt_bytes = b"ALT_720P_VARIANT_BYTES"

        class VariantPinningProvider:
            def __init__(self):
                self.pinned_variants = []

            def is_channel_authorized(self, channel_ref: str) -> bool:
                return True

            async def file_size(self, ref: str) -> int:
                return len(dummy_alt_bytes)

            async def resolve_stream(self, raw_id: str, variant: str | None = None) -> str:
                self.pinned_variants.append(variant)
                return f"tg://{raw_id}:variant:{variant}"

            async def download_range(self, ref: str, offset: int, length: int | None):
                async def _gen():
                    yield dummy_alt_bytes

                return _gen()

        prov = VariantPinningProvider()

        with patch("resolver.desktop_api._db_connection"), \
             patch("resolver.desktop_api._verify_client_session", return_value={"user_id": "usr_1"}), \
             patch("resolver.desktop_api._get_stream_candidate_ids", return_value=["telegram:cand_stream_1"]), \
             patch("resolver.desktop_api._is_video_visible_and_playable", return_value=True), \
             patch("resolver.providers.get_provider", return_value=prov):

            res = client.get(
                "/api/v1/playback/stream/telegram:cand_stream_1?rendition_id=alt_720p",
                headers=auth,
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.content, dummy_alt_bytes)
            self.assertIn("alt_720p", prov.pinned_variants)

    def test_playback_capabilities_internal_exception_returns_500_not_401(self):
        """Internal server/DB exceptions on capabilities endpoint return 500/no-store, never 401."""
        client = TestClient(desktop_test_app)
        auth = {"Authorization": "Bearer ah_tok_test_dummy"}

        with patch("resolver.desktop_api._db_connection"), \
             patch("resolver.desktop_api._verify_client_session", side_effect=RuntimeError("Unexpected DB connection drop")):
            res = client.get("/api/v1/playback/capabilities", headers=auth)
            self.assertEqual(res.status_code, 500)
            self.assertEqual(res.headers.get("cache-control"), "private, no-store")
            self.assertIn("Failed to retrieve playback capabilities", res.json().get("error", ""))

    def test_playback_stream_proxy_error_does_not_echo_raw_exception(self):
        """Stream proxy errors return fixed detail and strictly omit raw tg://, paths, or credentials."""
        client = TestClient(desktop_test_app)
        auth = {"Authorization": "Bearer ah_tok_test_dummy"}

        sensitive_exc_msg = (
            "tg://channel_secret_ref:123:456?secret_token=super_secret_tg_key "
            "and local path /mnt/secure/storage/file.mp4"
        )

        class DummyWorkingProvider:
            def is_channel_authorized(self, channel_ref: str) -> bool:
                return True

            async def resolve_stream(self, raw_id: str, variant: str | None = None) -> str:
                return f"tg://{raw_id}"

        async def _mock_proxy_fail(*args, **kwargs):
            raise RuntimeError(sensitive_exc_msg)

        with patch("resolver.desktop_api._db_connection"), \
             patch("resolver.desktop_api._verify_client_session", return_value={"user_id": "usr_1"}), \
             patch("resolver.desktop_api._get_stream_candidate_ids", return_value=["telegram:cand_stream_1"]), \
             patch("resolver.desktop_api._is_video_visible_and_playable", return_value=True), \
             patch("resolver.providers.get_provider", return_value=DummyWorkingProvider()), \
             patch("resolver.telegram_proxy.proxy_telegram_response", side_effect=_mock_proxy_fail):

            res = client.get("/api/v1/playback/stream/telegram:cand_stream_1", headers=auth)
            self.assertEqual(res.status_code, 502)
            self.assertEqual(res.headers.get("cache-control"), "private, no-store")

            body_text = res.text
            self.assertNotIn("tg://", body_text)
            self.assertNotIn("super_secret_tg_key", body_text)
            self.assertNotIn("/mnt/secure", body_text)

            prob = res.json()
            self.assertEqual(prob.get("status"), 502)
            self.assertEqual(prob.get("code"), "STREAM_PROXY_ERROR")
            self.assertEqual(prob.get("detail"), "Failed to proxy media stream from upstream provider.")

    def test_playback_stream_rendition_id_validation_and_not_found(self):
        """Rendition ID rejects invalid characters with 400 and returns 404 if rendition is not found."""
        client = TestClient(desktop_test_app)
        auth = {"Authorization": "Bearer ah_tok_test_dummy"}

        # 1. Invalid characters in rendition_id -> 400 Bad Request
        res_bad = client.get(
            "/api/v1/playback/stream/telegram:cand_1?rendition_id=../../malicious$var",
            headers=auth,
        )
        self.assertEqual(res_bad.status_code, 400)
        self.assertEqual(res_bad.headers.get("cache-control"), "private, no-store")
        self.assertIn("rendition_id contains invalid characters", res_bad.json().get("error", ""))

        # 2. Rendition not found in provider -> 404 Not Found
        class ProviderVariantNotFound:
            def is_channel_authorized(self, channel_ref: str) -> bool:
                return True

            async def resolve_stream(self, raw_id: str, variant: str | None = None) -> str:
                raise ValueError(f"Variant '{variant}' not found")

        with patch("resolver.desktop_api._db_connection"), \
             patch("resolver.desktop_api._verify_client_session", return_value={"user_id": "usr_1"}), \
             patch("resolver.desktop_api._get_stream_candidate_ids", return_value=["telegram:cand_1"]), \
             patch("resolver.desktop_api._is_video_visible_and_playable", return_value=True), \
             patch("resolver.providers.get_provider", return_value=ProviderVariantNotFound()):

            res_404 = client.get(
                "/api/v1/playback/stream/telegram:cand_1?rendition_id=nonexistent_720p",
                headers=auth,
            )
            self.assertEqual(res_404.status_code, 404)
            self.assertEqual(res_404.headers.get("cache-control"), "private, no-store")
            self.assertEqual(res_404.json().get("code"), "RENDITION_NOT_FOUND")

    def test_api_v1_error_responses_enforce_no_store_headers(self):
        """All 404 and 405 error responses on /api/v1 strictly enforce Cache-Control: private, no-store."""
        client = TestClient(desktop_test_app)

        # 404 Not Found on nonexistent endpoint
        res_404 = client.get("/api/v1/nonexistent_prefix")
        self.assertEqual(res_404.status_code, 404)
        self.assertEqual(res_404.headers.get("cache-control"), "private, no-store")

        # 405 Method Not Allowed on GET endpoint
        res_405 = client.post("/api/v1/playback/capabilities")
        self.assertEqual(res_405.status_code, 405)
        self.assertEqual(res_405.headers.get("cache-control"), "private, no-store")

    def test_mount_desktop_api_precedes_legacy_catch_all(self):
        """mount_desktop_api correctly precedes legacy catch-all redirect routes."""
        from fastapi import FastAPI
        from fastapi.responses import RedirectResponse

        app_with_catchall = FastAPI()

        @app_with_catchall.get("/api/{rest:path}")
        async def legacy_catchall(rest: str):
            return RedirectResponse(f"/legacy/{rest}", status_code=307)

        mounted = mount_desktop_api(app_with_catchall, force=True)
        self.assertTrue(mounted)

        client = TestClient(app_with_catchall)
        # Desktop route is evaluated BEFORE legacy catch-all, returning 200 rather than 307
        res = client.get("/api/v1/version", follow_redirects=False)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json().get("status"), "ok")

    def test_production_resolver_containment_by_default(self):
        """Production resolver app does not mount Desktop routes by default and preserves legacy routing."""
        client = TestClient(resolver_production_app)

        # POST /api/v1/auth/login returns 405 Method Not Allowed due to legacy GET catch-all redirect
        res = client.post("/api/v1/auth/login", json={"username": "user", "password": "pwd"})
        self.assertEqual(res.status_code, 405)

        # POST /api/v1/auth/refresh returns 405 Method Not Allowed
        res_ref = client.post("/api/v1/auth/refresh", json={"refresh_token": "ah_ref_dummy"})
        self.assertEqual(res_ref.status_code, 405)

        # POST /api/v1/auth/revoke returns 405 Method Not Allowed
        res_rev = client.post("/api/v1/auth/revoke", json={"refresh_token": "ah_ref_dummy"})
        self.assertEqual(res_rev.status_code, 405)

        # GET /api/v1/catalog/videos is caught by legacy /api/{rest} redirect to /admin/api/v1/catalog/videos
        res_cat = client.get("/api/v1/catalog/videos", follow_redirects=False)
        self.assertEqual(res_cat.status_code, 307)
        self.assertIn("/admin/api/v1/catalog/videos", res_cat.headers.get("location", ""))

    def test_explicit_opt_in_staging_mount(self):
        """mount_desktop_api mounts routes when forced or when environment variable is enabled."""
        from fastapi import FastAPI

        test_app = FastAPI()
        # Default without env var does not mount
        with patch.dict(os.environ, {}, clear=True):
            mounted = mount_desktop_api(test_app, force=False)
            self.assertFalse(mounted)

        # With ADULT_HUB_ENABLE_DESKTOP_API=true mounts
        with patch.dict(os.environ, {"ADULT_HUB_ENABLE_DESKTOP_API": "true"}):
            mounted = mount_desktop_api(test_app, force=False)
            self.assertTrue(mounted)

        # Ensure default resolver.app does not mount Desktop API
        from resolver.app import app as resolver_default_app
        default_paths = {getattr(r, "path", None) for r in resolver_default_app.routes}
        self.assertNotIn("/api/v1/version", default_paths)

    def test_production_resolver_opt_in_mounts_single_version_and_desktop_routes(self):
        """Verify production resolver app mounts Desktop routes when ADULT_HUB_ENABLE_DESKTOP_API=true in an isolated process.

        Verifies:
        - Exactly one public /api/v1/version route is mounted across effective app routes and owned by get_version_discovery
        - Version response advertises Desktop API and direct_play capabilities
        - Public catalog and playback routes are mounted and return 401 (not legacy 307) without Bearer
        - Client API pairing routes remain unmounted (404/405)
        - Disabled default resolver app still returns 307 redirect for version, catalog, and playback endpoints
        """
        env = dict(os.environ)
        env["ADULT_HUB_ENABLE_DESKTOP_API"] = "true"
        env.pop("DATABASE_URL", None)
        env.pop("STANDALONE_DATABASE_URL", None)

        script = """
import json
import sys
from starlette.testclient import TestClient

import resolver.app
import resolver.desktop_api
from common.app_version import __version__ as APP_VERSION

app = resolver.app.app

def get_all_routes(router):
    routes = []
    for r in getattr(router, "routes", []):
        if hasattr(r, "effective_route_contexts"):
            routes.extend(r.effective_route_contexts())
        elif hasattr(r, "original_router"):
            routes.extend(get_all_routes(r.original_router))
        elif hasattr(r, "router"):
            routes.extend(get_all_routes(r.router))
        elif hasattr(r, "routes"):
            routes.extend(get_all_routes(r))
        else:
            routes.append(r)
    return routes

routes = get_all_routes(app.router)
version_routes = [r for r in routes if getattr(r, "path", None) == "/api/v1/version"]

assert len(version_routes) == 1, f"Expected exactly 1 version route, found {len(version_routes)}"
ver_route = version_routes[0]
assert ver_route.endpoint is resolver.desktop_api.get_version_discovery, (
    f"Version route handler {ver_route.endpoint} is not resolver.desktop_api.get_version_discovery"
)

client = TestClient(app)

res_ver = client.get("/api/v1/version")
assert res_ver.status_code == 200, f"Expected 200, got {res_ver.status_code}"
assert res_ver.headers.get("cache-control") == "public, max-age=300"
ver_data = res_ver.json()

res_cat = client.get("/api/v1/catalog/videos", follow_redirects=False)
assert res_cat.status_code == 401, f"Expected 401, got {res_cat.status_code}"
assert res_cat.headers.get("cache-control") == "private, no-store"

res_play = client.get("/api/v1/playback/capabilities", follow_redirects=False)
assert res_play.status_code == 401, f"Expected 401, got {res_play.status_code}"
assert res_play.headers.get("cache-control") == "private, no-store"

assert not [r for r in routes if str(getattr(r, "path", "")).startswith("/api/v1/auth/pair")], "pairing routes must not be mounted"
res_pair = client.post("/api/v1/auth/pair/init")
assert res_pair.status_code in (404, 405), f"Expected 404/405, got {res_pair.status_code}"

result = {
    "version_route_count": len(version_routes),
    "handler_is_desktop": ver_route.endpoint is resolver.desktop_api.get_version_discovery,
    "handler_name": ver_route.endpoint.__name__,
    "version_status": res_ver.status_code,
    "version_cache_control": res_ver.headers.get("cache-control"),
    "version_data": ver_data,
    "catalog_status": res_cat.status_code,
    "catalog_cache_control": res_cat.headers.get("cache-control"),
    "playback_status": res_play.status_code,
    "playback_cache_control": res_play.headers.get("cache-control"),
    "pair_status": res_pair.status_code,
}
print("__PAYLOAD_START__" + json.dumps(result) + "__PAYLOAD_END__")
"""
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        proc = subprocess.run([sys.executable, "-c", script], env=env, cwd=repo_root, capture_output=True, text=True)
        self.assertEqual(
            proc.returncode,
            0,
            f"Isolated opt-in verification failed with returncode {proc.returncode}:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
        )
        self.assertIn("__PAYLOAD_START__", proc.stdout)
        payload_str = proc.stdout.split("__PAYLOAD_START__", 1)[1].split("__PAYLOAD_END__", 1)[0]
        data = json.loads(payload_str)
        self.assertEqual(data["version_route_count"], 1)
        self.assertTrue(data["handler_is_desktop"])
        self.assertEqual(data["handler_name"], "get_version_discovery")
        self.assertEqual(data["version_status"], 200)
        self.assertEqual(data["version_cache_control"], "public, max-age=300")

        ver = data["version_data"]
        self.assertEqual(ver.get("api_version"), "1.0.0")
        self.assertEqual(ver.get("server_version"), APP_VERSION)
        self.assertEqual(ver.get("status"), "ok")
        caps = ver.get("capabilities", {})
        self.assertTrue(caps.get("desktop_api"))
        self.assertFalse(caps.get("tv_pairing"))
        self.assertTrue(caps.get("direct_play"))
        self.assertFalse(caps.get("user_state"))
        self.assertFalse(caps.get("transcoding"))

        endpoints = ver.get("endpoints", {})
        self.assertEqual(endpoints.get("auth"), "/api/v1/auth")
        self.assertEqual(endpoints.get("catalog"), "/api/v1/catalog")
        self.assertEqual(endpoints.get("playback"), "/api/v1/playback")

        self.assertEqual(data["catalog_status"], 401)
        self.assertEqual(data["catalog_cache_control"], "private, no-store")
        self.assertEqual(data["playback_status"], 401)
        self.assertEqual(data["playback_cache_control"], "private, no-store")
        self.assertIn(data["pair_status"], (404, 405))

        # Disabled default must redirect legacy paths. Run in a child process with the flag explicitly false,
        # so the result does not depend on the runner's environment or import order.
        disabled_env = dict(os.environ)
        disabled_env["ADULT_HUB_ENABLE_DESKTOP_API"] = "false"
        disabled_env.pop("DATABASE_URL", None)
        disabled_env.pop("STANDALONE_DATABASE_URL", None)
        disabled_script = """
import json
from starlette.testclient import TestClient
import resolver.app

client = TestClient(resolver.app.app)
out = {}
for key, path in [
    ("version", "/api/v1/version"),
    ("catalog", "/api/v1/catalog/videos"),
    ("playback", "/api/v1/playback/capabilities"),
]:
    res = client.get(path, follow_redirects=False)
    out[key] = [res.status_code, res.headers.get("location", "")]
print("__PAYLOAD_START__" + json.dumps(out) + "__PAYLOAD_END__")
"""
        disabled_proc = subprocess.run(
            [sys.executable, "-c", disabled_script], env=disabled_env, cwd=repo_root, capture_output=True, text=True
        )
        self.assertEqual(
            disabled_proc.returncode,
            0,
            f"Disabled-default verification failed:\nSTDOUT:\n{disabled_proc.stdout}\nSTDERR:\n{disabled_proc.stderr}",
        )
        disabled_payload = disabled_proc.stdout.split("__PAYLOAD_START__", 1)[1].split("__PAYLOAD_END__", 1)[0]
        disabled = json.loads(disabled_payload)
        for key, legacy_path in [
            ("version", "/admin/api/v1/version"),
            ("catalog", "/admin/api/v1/catalog/videos"),
            ("playback", "/admin/api/v1/playback/capabilities"),
        ]:
            status, location = disabled[key]
            self.assertEqual(status, 307, key)
            self.assertIn(legacy_path, location, key)

    def test_api_v1_cache_policy_and_strict_path_prefix(self):
        """Ensures /api/v1/ authenticated routes have private, no-store and loose prefixes are not matched."""
        client = TestClient(desktop_test_app)

        # 1. Unauthenticated public version discovery keeps public caching
        res_ver = client.get("/api/v1/version")
        self.assertEqual(res_ver.status_code, 200)
        self.assertIn("public", res_ver.headers.get("Cache-Control", ""))

        # 2. Authenticated artwork endpoint success returns private, no-store
        auth_header = {"Authorization": "Bearer ah_tok_test_dummy"}
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\xaa" * 64
        with patch("resolver.desktop_api._db_connection"), \
             patch("resolver.desktop_api._verify_client_session", return_value={"user_id": "usr_1"}), \
             patch("resolver.desktop_api._resolve_artwork_asset", return_value=fake_jpeg):
            res_art = client.get(
                "/api/v1/artwork/video/telegram:vid-1/primary",
                headers=auth_header,
            )
            self.assertEqual(res_art.status_code, 200)
            self.assertEqual(res_art.headers.get("Cache-Control"), "private, no-store")

        # 3. Path matching does not match loose prefix like /api/v1fake
        res_loose = client.get("/api/v1fake_route")
        # Should be 404 and not touched by /api/v1/ cache policy enforcement
        self.assertEqual(res_loose.status_code, 404)

    def test_artwork_endpoints_require_bearer_authorization(self):
        """Artwork endpoints strictly require Bearer authorization before parameter or filesystem access."""
        client = TestClient(desktop_test_app)
        test_routes = [
            ("GET", "/api/v1/artwork/video/telegram:vid-1/primary"),
            ("HEAD", "/api/v1/artwork/video/telegram:vid-1/primary"),
            ("GET", "/api/v1/artwork/video/telegram:vid-1/poster"),
            ("GET", "/api/v1/artwork/performer/stashdb:perf-1"),
            ("GET", "/api/v1/artwork/performer/stashdb:perf-1/primary"),
            ("GET", "/api/v1/artwork/channel/-100123"),
            ("GET", "/api/v1/artwork/channel/-100123/primary"),
            ("GET", "/api/v1/artwork/video/telegram:vid-1"),
        ]
        for method, route in test_routes:
            # Missing auth header
            res1 = client.request(method, route)
            self.assertEqual(res1.status_code, 401, f"{method} {route} should return 401 on missing auth")
            self.assertEqual(res1.headers.get("cache-control"), "private, no-store")

            # Non-bearer scheme
            res2 = client.request(method, route, headers={"Authorization": "Basic dXNlcjpwYXNz"})
            self.assertEqual(res2.status_code, 401, f"{method} {route} should return 401 on Basic auth")
            self.assertEqual(res2.headers.get("cache-control"), "private, no-store")

            # Empty Bearer token
            res3 = client.request(method, route, headers={"Authorization": "Bearer "})
            self.assertEqual(res3.status_code, 401, f"{method} {route} should return 401 on empty Bearer")
            self.assertEqual(res3.headers.get("cache-control"), "private, no-store")

        # Verify auth is verified before parameter inspection (e.g. traversal attempt without auth returns 401)
        res_traversal_unauth = client.get("/api/v1/artwork/video/..%2f..%2fetc%2fpasswd/primary")
        self.assertEqual(res_traversal_unauth.status_code, 401)
        self.assertEqual(res_traversal_unauth.headers.get("cache-control"), "private, no-store")

    def test_artwork_endpoints_allowlists_and_input_validation(self):
        """Artwork endpoints validate entity types, kinds, reject path traversals, NUL bytes, and URI schemes."""
        client = TestClient(desktop_test_app)
        auth = {"Authorization": "Bearer ah_tok_test_dummy"}

        with patch("resolver.desktop_api._db_connection"), \
             patch("resolver.desktop_api._verify_client_session", return_value={"user_id": "usr_1"}):

            # Invalid entity_type returns 404
            res_bad_entity = client.get("/api/v1/artwork/unsupported_entity/vid-1/primary", headers=auth)
            self.assertEqual(res_bad_entity.status_code, 404)
            self.assertEqual(res_bad_entity.headers.get("cache-control"), "private, no-store")
            self.assertEqual(res_bad_entity.headers.get("content-type"), "application/problem+json")
            self.assertEqual(res_bad_entity.json().get("title"), "Entity Not Found")
            self.assertIn("Unsupported artwork entity type", res_bad_entity.json().get("detail", ""))

            # Invalid kind on video returns 404
            res_bad_video_kind = client.get("/api/v1/artwork/video/vid-1/unknown_kind", headers=auth)
            self.assertEqual(res_bad_video_kind.status_code, 404)
            self.assertEqual(res_bad_video_kind.headers.get("cache-control"), "private, no-store")
            self.assertEqual(res_bad_video_kind.headers.get("content-type"), "application/problem+json")
            self.assertEqual(res_bad_video_kind.json().get("title"), "Artwork Not Found")
            self.assertIn("Unsupported artwork kind", res_bad_video_kind.json().get("detail", ""))

            # Unsupported kind on performer returns 404 (only primary allowed)
            res_bad_perf_kind = client.get("/api/v1/artwork/performer/perf-1/backdrop", headers=auth)
            self.assertEqual(res_bad_perf_kind.status_code, 404)
            self.assertEqual(res_bad_perf_kind.headers.get("cache-control"), "private, no-store")

            # Unsupported kind on channel returns 404 (only primary allowed)
            res_bad_chan_kind = client.get("/api/v1/artwork/channel/chan-1/backdrop", headers=auth)
            self.assertEqual(res_bad_chan_kind.status_code, 404)
            self.assertEqual(res_bad_chan_kind.headers.get("cache-control"), "private, no-store")

            # Path traversal in identifier returns 400
            for bad_id in [
                "..%2f..%2fetc%2fpasswd",
                "..%5c..%5cwindows",
                "test%00evil",
                "http:%2f%2fevil.com%2fpic.jpg",
                "https:%2f%2fevil.com%2fpic.jpg",
                "file:%2f%2fetc%2fpasswd",
                "a" * 501,  # exceeds max length
            ]:
                res_traversal = client.get(f"/api/v1/artwork/video/{bad_id}/primary", headers=auth)
                self.assertEqual(res_traversal.status_code, 400, f"Expected 400 for bad id {bad_id}")
                self.assertEqual(res_traversal.headers.get("cache-control"), "private, no-store")
                self.assertEqual(res_traversal.headers.get("content-type"), "application/problem+json")

            # Absent/unmapped asset returns 404 without leaking internal paths
            with patch("resolver.desktop_api._resolve_artwork_asset", return_value=None):
                res_absent = client.get("/api/v1/artwork/video/telegram:absent_vid/primary", headers=auth)
                self.assertEqual(res_absent.status_code, 404)
                self.assertEqual(res_absent.headers.get("cache-control"), "private, no-store")
                self.assertNotIn("/media", res_absent.text)
                self.assertNotIn("telegram:", res_absent.text)

    def test_artwork_endpoints_serving_headers_etag_and_head(self):
        """Artwork endpoints serve valid images with proper content type, private cache, ETag, 304, and HEAD."""
        client = TestClient(desktop_test_app)
        auth = {"Authorization": "Bearer ah_tok_test_dummy"}
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\xaa" * 64

        with patch("resolver.desktop_api._db_connection"), \
             patch("resolver.desktop_api._verify_client_session", return_value={"user_id": "usr_1"}), \
             patch("resolver.desktop_api._resolve_artwork_asset", return_value=fake_jpeg):

            # GET returns 200 with headers and ETag
            res = client.get("/api/v1/artwork/video/telegram:vid-1/primary", headers=auth)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.content, fake_jpeg)
            self.assertEqual(res.headers.get("content-type"), "image/jpeg")
            self.assertEqual(res.headers.get("cache-control"), "private, no-store")
            etag = res.headers.get("etag")
            self.assertIsNotNone(etag)
            self.assertTrue(etag.startswith('"') and etag.endswith('"'))

            # Conditional GET with matching If-None-Match returns 304 Not Modified
            res_304 = client.get(
                "/api/v1/artwork/video/telegram:vid-1/primary",
                headers={**auth, "If-None-Match": etag},
            )
            self.assertEqual(res_304.status_code, 304)
            self.assertEqual(res_304.content, b"")
            self.assertEqual(res_304.headers.get("etag"), etag)
            self.assertEqual(res_304.headers.get("cache-control"), "private, no-store")

            # Conditional GET with non-matching If-None-Match returns 200
            res_200 = client.get(
                "/api/v1/artwork/video/telegram:vid-1/primary",
                headers={**auth, "If-None-Match": '"other-etag"'},
            )
            self.assertEqual(res_200.status_code, 200)
            self.assertEqual(res_200.content, fake_jpeg)

            # HEAD request returns 200 with headers but empty body
            res_head = client.head("/api/v1/artwork/video/telegram:vid-1/primary", headers=auth)
            self.assertEqual(res_head.status_code, 200)
            self.assertEqual(res_head.content, b"")
            self.assertEqual(res_head.headers.get("content-type"), "image/jpeg")
            self.assertEqual(res_head.headers.get("content-length"), str(len(fake_jpeg)))
            self.assertEqual(res_head.headers.get("etag"), etag)
            self.assertEqual(res_head.headers.get("cache-control"), "private, no-store")

    def test_artwork_asset_resolution_and_security_checks(self):
        """_resolve_artwork_asset safely accesses allowed files, checks boundaries, and ignores symlinks/traversals."""
        import tempfile
        from pathlib import Path
        from resolver.desktop_api import _resolve_artwork_asset
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir).resolve()
            media_root = tmp_root / "media"
            media_root.mkdir()

            # Create a valid JPEG
            fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\xbb" * 64
            img_file = media_root / "test_chan.jpg"
            img_file.write_bytes(fake_jpeg)

            # Outside file
            outside_dir = tmp_root / "outside"
            outside_dir.mkdir()
            outside_file = outside_dir / "secret.jpg"
            outside_file.write_bytes(fake_jpeg)

            # Symlink pointing outside media_root
            symlink_file = media_root / "symlink_chan.jpg"
            try:
                symlink_file.symlink_to(outside_file)
                has_symlink = True
            except OSError:
                has_symlink = False

            conn = MagicMock()
            conn.execute.return_value.fetchall.return_value = []
            conn.execute.return_value.fetchone.return_value = None

            with patch("resolver.admin.MEDIA_DIR", str(media_root)), \
                 patch.dict(os.environ, {"MEDIA_DIR": str(media_root)}):

                # Channel artwork resolution inside media_root succeeds
                data = _resolve_artwork_asset(conn, "channel", "test_chan", "icon")
                self.assertEqual(data, fake_jpeg)

                # Symlinked file is rejected
                if has_symlink:
                    symlink_data = _resolve_artwork_asset(conn, "channel", "symlink_chan", "icon")
                    self.assertIsNone(symlink_data)

                # Non-existent artwork returns None
                absent_data = _resolve_artwork_asset(conn, "channel", "nonexistent", "icon")
                self.assertIsNone(absent_data)


class TestDesktopApiPostgresLive(PgTestCase):
    """Live integration tests running against disposable PostgreSQL 17."""

    def setUp(self):
        super().setUp()
        self.app = create_desktop_integration_app(db_url=self.db_url)
        self.client = TestClient(self.app)

    def tearDown(self):
        super().tearDown()

    def _bootstrap_and_login(self, username="desktop_user", password="desktop_secure_password_123"):
        with db.connect(self.db_url) as conn:
            bootstrap_owner(conn, username, password)

        res = self.client.post(
            "/api/v1/auth/login",
            json={
                "username": username,
                "password": password,
                "device_name": "Desktop Test Machine",
                "device_id": "desktop-dev-001",
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")
        data = res.json()
        return data["access_token"], data["refresh_token"], data["user"]

    def test_desktop_login_response_omits_is_admin_and_sets_private_cache(self):
        """Owner login issues valid tokens, sets private cache, and strictly omits is_admin."""
        access_tok, refresh_tok, user = self._bootstrap_and_login("alice", "AlicePassword999!")

        self.assertTrue(access_tok.startswith("ah_tok_"))
        self.assertTrue(refresh_tok.startswith("ah_ref_"))
        self.assertEqual(user["username"], "alice")
        self.assertIn("id", user)
        self.assertNotIn("is_admin", user)  # Strict non-exposure

    def test_desktop_single_use_refresh_rotation_and_replay_revocation(self):
        """Refresh rotates refresh token and subsequent replay of old token revokes session."""
        _, refresh_tok, _ = self._bootstrap_and_login("bob", "BobPassword999!")

        # First refresh succeeds
        res_refresh = self.client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_tok})
        self.assertEqual(res_refresh.status_code, 200)
        self.assertEqual(res_refresh.headers.get("cache-control"), "private, no-store")
        data = res_refresh.json()
        self.assertTrue(data["access_token"])
        new_refresh = data["refresh_token"]
        self.assertNotEqual(new_refresh, refresh_tok)

        # Replaying spent refresh_tok raises 401 replay attack and revokes family
        res_replay = self.client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_tok})
        self.assertEqual(res_replay.status_code, 401)
        self.assertEqual(res_replay.headers.get("cache-control"), "private, no-store")
        self.assertIn("revoked", res_replay.json()["error"].lower())

        # Further refresh with new_refresh also fails because family was revoked
        res_post_replay = self.client.post("/api/v1/auth/refresh", json={"refresh_token": new_refresh})
        self.assertEqual(res_post_replay.status_code, 401)
        self.assertEqual(res_post_replay.headers.get("cache-control"), "private, no-store")

    def test_authenticated_catalog_videos_browse_and_detail(self):
        """Authenticated catalog browse and detail queries return allowlisted fields without leaks."""
        access_tok, _, _ = self._bootstrap_and_login("carol", "CarolPassword999!")
        auth_header = {"Authorization": f"Bearer {access_tok}"}

        # Seed catalog data using canonical database schema (date_released, first_seen with timezone, provider)
        with db.connect(self.db_url) as conn:
            conn.execute(
                """INSERT INTO videos (
                       id, provider, title, duration, date_released, first_seen,
                       studio, plot, categories, tags, performers, playable, file_size_bytes
                   ) VALUES (
                       'vid-101', 'telegram', 'Desktop Feature Film', 3600, '2026-01-01',
                       '2026-01-02 00:00:00+00', 'Studio Alpha', 'A sample plot.',
                       '["Feature"]'::jsonb, '["Desktop"]'::jsonb, '[]'::jsonb, 1, 104857600
                   )"""
            )
            conn.commit()

        # Browse
        res = self.client.get("/api/v1/catalog/videos", headers=auth_header)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")
        data = res.json()
        self.assertEqual(data["total_count"], 1)
        self.assertEqual(len(data["items"]), 1)

        item = data["items"][0]
        self.assertEqual(item["id"], "vid-101")
        self.assertEqual(item["title"], "Desktop Feature Film")
        self.assertEqual(item["duration_seconds"], 3600)
        self.assertEqual(item["release_date"], "2026-01-01")
        self.assertEqual(item["date_added"], "2026-01-02T00:00:00Z")
        self.assertEqual(item["studio"], "Studio Alpha")
        self.assertEqual(item["plot"], "A sample plot.")

        # Verify zero metadata leaks
        self.assertNotIn("local_files", item)
        self.assertNotIn("telegram_url", item)
        self.assertNotIn("playable", item)
        self.assertNotIn("dead_stream", item)
        self.assertNotIn("is_admin", item)

        # Detail
        res_detail = self.client.get("/api/v1/catalog/videos/vid-101", headers=auth_header)
        self.assertEqual(res_detail.status_code, 200)
        self.assertEqual(res_detail.headers.get("cache-control"), "private, no-store")
        detail = res_detail.json()
        self.assertEqual(detail["id"], "vid-101")
        self.assertEqual(detail["title"], "Desktop Feature Film")
        self.assertEqual(detail["release_date"], "2026-01-01")

        # Nonexistent video returns 404
        res_missing = self.client.get("/api/v1/catalog/videos/vid-nonexistent", headers=auth_header)
        self.assertEqual(res_missing.status_code, 404)
        self.assertEqual(res_missing.headers.get("cache-control"), "private, no-store")
        self.assertIn("Video not found", res_missing.json()["error"])

    def test_authenticated_catalog_videos_browse_with_filters(self):
        """Authenticated catalog browse supports studio, min_duration, and sort filters."""
        access_tok, _, _ = self._bootstrap_and_login("filter_user", "FilterPass123!")
        auth_header = {"Authorization": f"Bearer {access_tok}"}

        with db.connect(self.db_url) as conn:
            conn.execute(
                """INSERT INTO videos (id, provider, title, duration, date_released, first_seen, studio, playable)
                   VALUES ('vid-short', 'telegram', 'Short Film', 600, '2026-02-01', '2026-02-01 00:00:00+00', 'Studio Alpha', 1),
                          ('vid-long', 'telegram', 'Long Feature', 7200, '2026-02-02', '2026-02-02 00:00:00+00', 'Studio Beta', 1)""",
            )
            conn.commit()

        # Filter by studio
        res_studio = self.client.get("/api/v1/catalog/videos?studio=Studio+Alpha", headers=auth_header)
        self.assertEqual(res_studio.status_code, 200)
        data_studio = res_studio.json()
        self.assertEqual(data_studio["total_count"], 1)
        self.assertEqual(data_studio["items"][0]["id"], "vid-short")

        # Filter by min_duration
        res_dur = self.client.get("/api/v1/catalog/videos?min_duration=3600", headers=auth_header)
        self.assertEqual(res_dur.status_code, 200)
        data_dur = res_dur.json()
        self.assertEqual(data_dur["total_count"], 1)
        self.assertEqual(data_dur["items"][0]["id"], "vid-long")

        # Sort by duration
        res_sort = self.client.get("/api/v1/catalog/videos?sort=duration", headers=auth_header)
        self.assertEqual(res_sort.status_code, 200)
        items = res_sort.json()["items"]
        self.assertGreaterEqual(len(items), 2)
        self.assertEqual(items[0]["id"], "vid-long")

        # Sort by duration descending (explicit)
        res_sort_desc = self.client.get("/api/v1/catalog/videos?sort=duration&direction=desc", headers=auth_header)
        self.assertEqual(res_sort_desc.status_code, 200)
        items_desc = res_sort_desc.json()["items"]
        self.assertGreaterEqual(len(items_desc), 2)
        self.assertEqual(items_desc[0]["id"], "vid-long")

        # Sort by duration ascending (explicit)
        res_sort_asc = self.client.get("/api/v1/catalog/videos?sort=duration&direction=asc", headers=auth_header)
        self.assertEqual(res_sort_asc.status_code, 200)
        items_asc = res_sort_asc.json()["items"]
        self.assertGreaterEqual(len(items_asc), 2)
        self.assertEqual(items_asc[0]["id"], "vid-short")

    def test_authenticated_catalog_channels_browse_and_detail(self):
        """Authenticated channels catalog returns registered Telegram channels."""
        access_tok, _, _ = self._bootstrap_and_login("dave", "DavePassword999!")
        auth_header = {"Authorization": f"Bearer {access_tok}"}

        # Browse channels
        res = self.client.get("/api/v1/catalog/channels", headers=auth_header)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")
        data = res.json()
        self.assertIn("items", data)
        self.assertIn("total_count", data)

        # Query unknown channel returns 404
        res_unknown = self.client.get("/api/v1/catalog/channels/unknown-channel-id-999", headers=auth_header)
        self.assertEqual(res_unknown.status_code, 404)
        self.assertEqual(res_unknown.headers.get("cache-control"), "private, no-store")
        self.assertIn("Channel not found", res_unknown.json()["error"])

    def test_revoked_session_token_is_rejected_on_catalog(self):
        """Revoking session immediately invalidates access token for catalog queries."""
        access_tok, refresh_tok, _ = self._bootstrap_and_login("eve", "EvePassword999!")
        auth_header = {"Authorization": f"Bearer {access_tok}"}

        # Access before revocation succeeds
        res_before = self.client.get("/api/v1/catalog/videos", headers=auth_header)
        self.assertEqual(res_before.status_code, 200)

        # Revoke session
        res_revoke = self.client.post("/api/v1/auth/revoke", json={"refresh_token": refresh_tok})
        self.assertEqual(res_revoke.status_code, 200)
        self.assertEqual(res_revoke.headers.get("cache-control"), "private, no-store")
        self.assertTrue(res_revoke.json()["revoked"])

        # Access after revocation fails with 401 Unauthorized
        res_after = self.client.get("/api/v1/catalog/videos", headers=auth_header)
        self.assertEqual(res_after.status_code, 401)
        self.assertEqual(res_after.headers.get("cache-control"), "private, no-store")
        self.assertIn("expired", res_after.json()["error"].lower())

    def test_disabled_account_access_token_is_rejected(self):
        """Tokens belonging to disabled users are rejected on catalog access."""
        access_tok, _, user = self._bootstrap_and_login("frank", "FrankPassword999!")
        auth_header = {"Authorization": f"Bearer {access_tok}"}

        # Disable user
        with db.connect(self.db_url) as conn:
            conn.execute("UPDATE client_users SET is_active = false WHERE id = %s", (user["id"],))
            conn.commit()

        # Access fails with 401
        res = self.client.get("/api/v1/catalog/videos", headers=auth_header)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.headers.get("cache-control"), "private, no-store")

    def test_authenticated_playback_descriptor_and_stream_with_failover(self):  # noqa: C901
        """Authenticated playback descriptor resolves aliases and streams with pre-play source failover."""
        access_tok, _, _ = self._bootstrap_and_login("playback_user", "PlayPass12345!")
        auth_header = {"Authorization": f"Bearer {access_tok}"}
        dummy_media = b"POSTGRES_LIVE_FAILOVER_STREAM_BYTES_999"

        with db.connect(self.db_url) as conn:
            # Seed canonical video
            conn.execute(
                """INSERT INTO videos (id, provider, title, duration, date_released, first_seen, file_size_bytes, playable)
                   VALUES ('telegram:keeper_vid', 'telegram', 'Canonical Playback Video', 1800, '2026-03-01', '2026-03-01 00:00:00+00', 2000000, 1)""",
            )
            # Seed duplicate decision mapping loser to keeper
            if "video_duplicate_decisions" in db.table_names(conn):
                conn.execute(
                    """INSERT INTO video_duplicate_decisions (loser_id, keeper_id, operation_id, state, created_at)
                       VALUES ('telegram:loser_alias', 'telegram:keeper_vid', 'op_failover_1', 'active', '2026-03-01 00:00:00+00')""",
                )
            # Seed failover secondary source in video_sources
            if "video_sources" in db.table_names(conn):
                conn.execute(
                    """INSERT INTO video_sources (video_id, source_id, provider, is_active, priority, created_at)
                       VALUES ('telegram:keeper_vid', 'telegram:keeper_vid', 'telegram', 1, 1, '2026-03-01 00:00:00+00'),
                              ('telegram:keeper_vid', 'telegram:secondary_source', 'telegram', 1, 2, '2026-03-01 00:00:00+00')""",
                )
            conn.commit()

        class LiveFailoverProvider:
            def __init__(self):
                self.calls = []

            def is_channel_authorized(self, channel_ref: str) -> bool:
                return True

            async def file_size(self, ref: str) -> int:
                return len(dummy_media)

            async def get_video_variants(self, ref: str) -> dict:
                return {
                    "status": "ready",
                    "video_processing_pending": False,
                    "has_alternatives": True,
                    "variants": [
                        {
                            "id": "original",
                            "is_original": True,
                            "width": 1920,
                            "height": 1080,
                            "mime_type": "video/mp4",
                            "file_size": 2000000,
                            "duration": 1800.0,
                        },
                        {
                            "id": "alt_720",
                            "is_original": False,
                            "width": 1280,
                            "height": 720,
                            "mime_type": "video/mp4",
                            "file_size": 1000000,
                            "duration": 1800.0,
                        },
                    ],
                }

            async def resolve_stream(self, raw_id: str, variant: str | None = None) -> str:
                self.calls.append(raw_id)
                if "keeper_vid" in raw_id:
                    # Primary candidate fails preflight
                    raise RuntimeError("Primary DC dropped connection")
                return f"tg://{raw_id}"

            async def download_range(self, ref: str, offset: int, length: int | None):
                async def _gen():
                    yield dummy_media

                return _gen()

        prov = LiveFailoverProvider()

        with patch("resolver.providers.get_provider", return_value=prov):
            # 1. Capabilities discovery
            res_caps = self.client.get("/api/v1/playback/capabilities", headers=auth_header)
            self.assertEqual(res_caps.status_code, 200)
            self.assertEqual(res_caps.headers.get("cache-control"), "private, no-store")
            self.assertIn("http_range_rfc7233", res_caps.json()["streaming_protocols"])

            # 2. Playback descriptor via duplicate loser alias resolves to keeper canonical ID
            res_desc = self.client.get("/api/v1/playback/renditions/telegram:loser_alias", headers=auth_header)
            self.assertEqual(res_desc.status_code, 200)
            self.assertEqual(res_desc.headers.get("cache-control"), "private, no-store")
            desc_data = res_desc.json()
            self.assertEqual(desc_data["video_id"], "telegram:keeper_vid")
            self.assertEqual(desc_data["stream_path"], "/api/v1/playback/stream/telegram:keeper_vid")
            self.assertEqual(len(desc_data["renditions"]), 2)

            # 3. Stream with pre-play source failover (primary keeper_vid fails -> secondary_source plays)
            res_stream = self.client.get("/api/v1/playback/stream/telegram:loser_alias", headers=auth_header)
            self.assertEqual(res_stream.status_code, 200)
            self.assertEqual(res_stream.content, dummy_media)
            self.assertEqual(res_stream.headers.get("cache-control"), "private, no-store")
            self.assertIn("keeper_vid", prov.calls)
            self.assertIn("secondary_source", prov.calls)

            # 4. Dead stream is rejected with 404
            with db.connect(self.db_url) as conn:
                if "dead_streams" in db.table_names(conn):
                    conn.execute("INSERT INTO dead_streams (video_id) VALUES ('telegram:keeper_vid')")
                    conn.commit()

            res_dead = self.client.get("/api/v1/playback/renditions/telegram:keeper_vid", headers=auth_header)
            self.assertEqual(res_dead.status_code, 404)
            self.assertEqual(res_dead.headers.get("cache-control"), "private, no-store")

    def test_taxonomy_excludes_videos_from_disabled_channels(self):
        """Taxonomy counts (categories, studios, tags, performers) never include videos from configured-disabled channels."""
        access_tok, _refresh, _user = self._bootstrap_and_login("tax_disabled_owner", "TaxDisabledPassword1!")
        auth = {"Authorization": f"Bearer {access_tok}"}
        with db.connect(self.db_url) as conn:
            conn.execute(
                """INSERT INTO videos (id, provider, title, duration, categories, tags, performers, studio, playable, file_size_bytes)
                   VALUES
                   ('telegram:-100555:1:1', 'telegram', 'Hidden Channel Video', 1200,
                    '["HiddenCat"]'::jsonb, '["HiddenTag"]'::jsonb, '["Hidden Performer"]'::jsonb, 'HiddenStudio', 1, 100000000),
                   ('telegram:-100111:1:1', 'telegram', 'Visible Channel Video', 1200,
                    '["VisibleCat"]'::jsonb, '["VisibleTag"]'::jsonb, '["Visible Performer"]'::jsonb, 'VisibleStudio', 1, 100000000)
                   ON CONFLICT (id) DO NOTHING"""
            )
            conn.commit()

        disabled_meta = [{"id": "-100555", "enabled": False}]
        with patch("resolver.client_catalog._get_configured_channel_metadata", return_value=disabled_meta):
            cats = {i["name"] for i in self.client.get("/api/v1/catalog/categories", headers=auth).json()["items"]}
            studios = {i["name"] for i in self.client.get("/api/v1/catalog/studios", headers=auth).json()["items"]}
            tags = {i["name"] for i in self.client.get("/api/v1/catalog/tags", headers=auth).json()["items"]}
            perfs = {i["name"] for i in self.client.get("/api/v1/catalog/performers", headers=auth).json()["items"]}

        self.assertIn("VisibleCat", cats)
        self.assertNotIn("HiddenCat", cats)
        self.assertIn("VisibleStudio", studios)
        self.assertNotIn("HiddenStudio", studios)
        self.assertIn("VisibleTag", tags)
        self.assertNotIn("HiddenTag", tags)
        self.assertIn("Visible Performer", perfs)
        self.assertNotIn("Hidden Performer", perfs)

    def test_watch_progress_owner_scoped_crud_and_duplicate_alias_resolution(self):
        """Watch progress PUT/GET/DELETE enforce owner scoping, input validation, and canonical duplicate alias resolution."""
        # 1. Bootstrap two separate test users
        from datetime import datetime, timedelta, timezone
        with db.connect(self.db_url) as conn:
            bootstrap_owner(conn, username="owner_viewer", password="owner_password_123")
            # Create second user
            conn.execute(
                "INSERT INTO client_users (id, username, password_hash, is_admin, is_active) "
                "VALUES ('usr_user2', 'viewer2', 'hash2', false, true) ON CONFLICT DO NOTHING"
            )
            # Create session for second user
            now_utc = datetime.now(timezone.utc)
            access_expires = now_utc + timedelta(days=1)
            refresh_expires = now_utc + timedelta(days=30)
            conn.execute(
                "INSERT INTO client_sessions (id, user_id, device_id, access_token_hash, refresh_token_hash, access_expires_at, refresh_expires_at) "
                "VALUES ('sess_u2', 'usr_user2', 'dev_u2', %s, 'ref_h2', %s, %s) ON CONFLICT DO NOTHING",
                (_safe_hash_token("ah_tok_user2"), access_expires, refresh_expires),
            )
            # Ensure duplicate decisions and sources exist
            tables = db.table_names(conn)
            if "videos" in tables:
                conn.execute(
                    "INSERT INTO videos (id, title, duration, playable) "
                    "VALUES ('telegram:prog_keeper', 'Progress Keeper Video', 1200, 1) ON CONFLICT DO NOTHING"
                )
                conn.execute(
                    "INSERT INTO videos (id, title, duration, playable) "
                    "VALUES ('telegram:prog_loser', 'Progress Loser Video', 1200, 1) ON CONFLICT DO NOTHING"
                )
            if "video_duplicate_decisions" in tables:
                conn.execute(
                    "INSERT INTO video_duplicate_decisions (loser_id, keeper_id, operation_id, state, created_at) "
                    "VALUES ('telegram:prog_loser', 'telegram:prog_keeper', 'op_prog_1', 'active', '2026-11-22 19:00:00+00') ON CONFLICT DO NOTHING"
                )
            conn.commit()

        # Login as user 1
        res_login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "owner_viewer", "password": "owner_password_123"},
        )
        self.assertEqual(res_login.status_code, 200)
        auth_u1 = {"Authorization": f"Bearer {res_login.json()['access_token']}"}
        auth_u2 = {"Authorization": "Bearer ah_tok_user2"}

        # 2. User 1: 404 before progress is written
        res_empty = self.client.get("/api/v1/user/watch/telegram:prog_keeper", headers=auth_u1)
        self.assertEqual(res_empty.status_code, 404)
        self.assertEqual(res_empty.headers.get("cache-control"), "private, no-store")

        # Check /api/v1/me/progress alias as well
        res_alias_empty = self.client.get("/api/v1/me/progress/telegram:prog_keeper", headers=auth_u1)
        self.assertEqual(res_alias_empty.status_code, 404)

        # 3. User 1: Put progress via /api/v1/user/watch
        # Client sends mismatched played_percentage (99.0); backend must recompute from position/duration (25.0)
        put_payload = {
            "position_seconds": 300,
            "duration_seconds": 1200,
            "is_played": False,
            "played_percentage": 99.0,
            "last_watched_at": "2026-11-22T19:30:00Z",
        }
        res_put = self.client.put("/api/v1/user/watch/telegram:prog_keeper", json=put_payload, headers=auth_u1)
        self.assertEqual(res_put.status_code, 200)
        self.assertEqual(res_put.headers.get("cache-control"), "private, no-store")
        self.assertTrue(res_put.json().get("ok"))

        # 4. User 1: Get progress via both paths
        res_get1 = self.client.get("/api/v1/user/watch/telegram:prog_keeper", headers=auth_u1)
        self.assertEqual(res_get1.status_code, 200)
        self.assertEqual(res_get1.headers.get("cache-control"), "private, no-store")
        data1 = res_get1.json()
        self.assertEqual(data1["video_id"], "telegram:prog_keeper")
        self.assertEqual(data1["position_seconds"], 300)
        self.assertEqual(data1["duration_seconds"], 1200)
        self.assertEqual(data1["played_percentage"], 25.0)
        self.assertFalse(data1["is_played"])
        self.assertIn("2026-11-22", data1["last_watched_at"])

        res_get_alias = self.client.get("/api/v1/me/progress/telegram:prog_keeper", headers=auth_u1)
        self.assertEqual(res_get_alias.status_code, 200)
        self.assertEqual(res_get_alias.json()["position_seconds"], 300)

        # 5. User Isolation: User 2 reading keeper gets 404
        res_u2 = self.client.get("/api/v1/user/watch/telegram:prog_keeper", headers=auth_u2)
        self.assertEqual(res_u2.status_code, 404)

        # 6. Duplicate Alias Resolution on Read: query via loser_id returns keeper progress
        res_loser_read = self.client.get("/api/v1/user/watch/telegram:prog_loser", headers=auth_u1)
        self.assertEqual(res_loser_read.status_code, 200)
        self.assertEqual(res_loser_read.json()["position_seconds"], 300)
        self.assertEqual(res_loser_read.json()["video_id"], "telegram:prog_keeper")

        # 7. Duplicate Alias Resolution on Write: PUT via loser_id upserts keeper progress
        put_update = {
            "position_seconds": 600,
            "duration_seconds": 1200,
            "is_played": False,
        }
        res_put_loser = self.client.put("/api/v1/me/progress/telegram:prog_loser", json=put_update, headers=auth_u1)
        self.assertEqual(res_put_loser.status_code, 200)

        res_get_updated = self.client.get("/api/v1/user/watch/telegram:prog_keeper", headers=auth_u1)
        self.assertEqual(res_get_updated.json()["position_seconds"], 600)
        self.assertEqual(res_get_updated.json()["played_percentage"], 50.0)

        # 8. Conflict Resolution: if both keeper and loser have historical progress rows
        with db.connect(self.db_url) as conn:
            # Insert older historical row for loser with higher played percentage
            conn.execute(
                "INSERT INTO client_user_watch_progress "
                "(user_id, video_id, position_seconds, duration_seconds, played_percentage, is_played, last_watched_at) "
                "VALUES ('usr_user2', 'telegram:prog_loser', 900, 1200, 75.0, false, '2026-11-22T19:00:00Z') "
                "ON CONFLICT (user_id, video_id) DO UPDATE SET played_percentage = 75.0, position_seconds = 900, last_watched_at = '2026-11-22T19:00:00Z'"
            )
            # Insert newer progress for keeper with lower played percentage
            conn.execute(
                "INSERT INTO client_user_watch_progress "
                "(user_id, video_id, position_seconds, duration_seconds, played_percentage, is_played, last_watched_at) "
                "VALUES ('usr_user2', 'telegram:prog_keeper', 200, 1200, 16.67, false, '2026-11-22T20:00:00Z') "
                "ON CONFLICT (user_id, video_id) DO UPDATE SET played_percentage = 16.67, position_seconds = 200, last_watched_at = '2026-11-22T20:00:00Z'"
            )
            conn.commit()

        # Reading keeper for user2 should yield the newer canonical write beating older alias history
        res_u2_conflict = self.client.get("/api/v1/user/watch/telegram:prog_keeper", headers=auth_u2)
        self.assertEqual(res_u2_conflict.status_code, 200)
        self.assertEqual(res_u2_conflict.json()["position_seconds"], 200)
        self.assertEqual(res_u2_conflict.json()["played_percentage"], 16.67)

        # 9. Delete watch progress
        res_del = self.client.delete("/api/v1/user/watch/telegram:prog_keeper", headers=auth_u1)
        self.assertEqual(res_del.status_code, 200)
        self.assertEqual(res_del.headers.get("cache-control"), "private, no-store")
        self.assertTrue(res_del.json().get("deleted"))

        # User 1 now gets 404
        self.assertEqual(self.client.get("/api/v1/user/watch/telegram:prog_keeper", headers=auth_u1).status_code, 404)
        # User 2 progress is still untouched (isolation)
        self.assertEqual(self.client.get("/api/v1/user/watch/telegram:prog_keeper", headers=auth_u2).status_code, 200)

    def test_user_favorites_postgres_flow_and_canonical_alias_resolution(self):
        """Authenticated favorites end-to-end: bearer auth, owner isolation, canonical aliases, single-card list, catalog integration."""
        with db.connect(self.db_url) as conn:
            tables = db.table_names(conn)
            # Ensure users exist
            conn.execute(
                "INSERT INTO client_users (id, username, password_hash, is_admin, is_active) "
                "VALUES ('usr_fav_u1', 'fav_viewer', 'hash1', false, true) ON CONFLICT (id) DO NOTHING"
            )
            conn.execute(
                "INSERT INTO client_users (id, username, password_hash, is_admin, is_active) "
                "VALUES ('usr_fav_u2', 'fav_other', 'hash2', false, true) ON CONFLICT (id) DO NOTHING"
            )
            conn.execute(
                "INSERT INTO client_sessions (id, user_id, device_name, device_id, access_token_hash, refresh_token_hash, access_expires_at, refresh_expires_at) "
                "VALUES ('sess_fav_u1', 'usr_fav_u1', 'Desktop', 'dev_fav_1', %s, 'ref_fav_u1', '2030-01-01 00:00:00+00', '2030-01-01 00:00:00+00') ON CONFLICT (id) DO NOTHING",
                (_safe_hash_token("ah_tok_fav_u1"),),
            )
            conn.execute(
                "INSERT INTO client_sessions (id, user_id, device_name, device_id, access_token_hash, refresh_token_hash, access_expires_at, refresh_expires_at) "
                "VALUES ('sess_fav_u2', 'usr_fav_u2', 'Desktop', 'dev_fav_2', %s, 'ref_fav_u2', '2030-01-01 00:00:00+00', '2030-01-01 00:00:00+00') ON CONFLICT (id) DO NOTHING",
                (_safe_hash_token("ah_tok_fav_u2"),),
            )

            # Seed videos: canonical keeper, loser duplicate, and a second active source representation
            conn.execute(
                "INSERT INTO videos (id, title, duration, categories, tags, file_size_bytes, first_seen, date_released) "
                "VALUES ('telegram:fav_keeper', 'Keeper Title', 1800, '[\"Action\"]'::jsonb, '[\"HD\"]'::jsonb, 500000000, '2026-11-20 12:00:00+00', '2026-11-20') "
                "ON CONFLICT (id) DO UPDATE SET title = 'Keeper Title'"
            )
            conn.execute(
                "INSERT INTO videos (id, title, duration, categories, tags, file_size_bytes, first_seen, date_released) "
                "VALUES ('telegram:fav_loser', 'Loser Title', 1800, '[\"Action\"]'::jsonb, '[\"HD\"]'::jsonb, 500000000, '2026-11-19 12:00:00+00', '2026-11-19') "
                "ON CONFLICT (id) DO UPDATE SET title = 'Loser Title'"
            )
            if "video_sources" in tables:
                conn.execute(
                    "INSERT INTO video_sources (video_id, source_id, provider, channel_id, is_active, created_at) "
                    "VALUES ('telegram:fav_keeper', 'telegram:fav_source2', 'telegram', '-100999', 1, '2026-11-19 12:00:00+00') "
                    "ON CONFLICT DO NOTHING"
                )
            if "video_duplicate_decisions" in tables:
                conn.execute(
                    "INSERT INTO video_duplicate_decisions (loser_id, keeper_id, operation_id, state, created_at) "
                    "VALUES ('telegram:fav_loser', 'telegram:fav_keeper', 'op_fav_1', 'active', '2026-11-22 19:00:00+00') "
                    "ON CONFLICT DO NOTHING"
                )
            # Clean favorites for test users
            if "client_user_favorites" in tables:
                conn.execute("DELETE FROM client_user_favorites WHERE user_id IN ('usr_fav_u1', 'usr_fav_u2')")
            conn.commit()

        auth_u1 = {"Authorization": "Bearer ah_tok_fav_u1"}
        auth_u2 = {"Authorization": "Bearer ah_tok_fav_u2"}

        # 1. Initially neither user has keeper favorited
        res_check0 = self.client.get("/api/v1/user/favorites/telegram:fav_keeper", headers=auth_u1)
        self.assertEqual(res_check0.status_code, 200)
        self.assertEqual(res_check0.json()["video_id"], "telegram:fav_keeper")
        self.assertFalse(res_check0.json()["is_favorite"])
        self.assertEqual(res_check0.headers.get("cache-control"), "private, no-store")

        res_list0 = self.client.get("/api/v1/user/favorites", headers=auth_u1)
        self.assertEqual(res_list0.status_code, 200)
        self.assertEqual(res_list0.json()["total_count"], 0)
        self.assertEqual(len(res_list0.json()["items"]), 0)

        # 2. User 1 adds favorite via canonical keeper ID
        res_add = self.client.put("/api/v1/user/favorites/telegram:fav_keeper", headers=auth_u1)
        self.assertEqual(res_add.status_code, 200)
        self.assertEqual(res_add.json()["video_id"], "telegram:fav_keeper")
        self.assertTrue(res_add.json()["is_favorite"])

        # Check status by canonical keeper ID
        res_chk_keeper = self.client.get("/api/v1/user/favorites/telegram:fav_keeper", headers=auth_u1)
        self.assertEqual(res_chk_keeper.status_code, 200)
        self.assertTrue(res_chk_keeper.json()["is_favorite"])

        # Check status by loser alias ID -> resolves to keeper and is_favorite=True
        res_chk_loser = self.client.get("/api/v1/user/favorites/telegram:fav_loser", headers=auth_u1)
        self.assertEqual(res_chk_loser.status_code, 200)
        self.assertEqual(res_chk_loser.json()["video_id"], "telegram:fav_keeper")
        self.assertTrue(res_chk_loser.json()["is_favorite"])

        # Check status by source alias ID -> resolves to keeper and is_favorite=True
        res_chk_source = self.client.get("/api/v1/user/favorites/telegram:fav_source2", headers=auth_u1)
        self.assertEqual(res_chk_source.status_code, 200)
        self.assertEqual(res_chk_source.json()["video_id"], "telegram:fav_keeper")
        self.assertTrue(res_chk_source.json()["is_favorite"])

        # 3. Owner Isolation: User 2 checks status -> is_favorite=False, list=empty
        res_u2_chk = self.client.get("/api/v1/user/favorites/telegram:fav_keeper", headers=auth_u2)
        self.assertEqual(res_u2_chk.status_code, 200)
        self.assertFalse(res_u2_chk.json()["is_favorite"])

        res_u2_list = self.client.get("/api/v1/user/favorites", headers=auth_u2)
        self.assertEqual(res_u2_list.status_code, 200)
        self.assertEqual(res_u2_list.json()["total_count"], 0)

        # 4. Single-card list guarantee: even if multiple alias rows exist in client_user_favorites
        with db.connect(self.db_url) as conn:
            conn.execute(
                "INSERT INTO client_user_favorites (user_id, video_id, created_at) "
                "VALUES ('usr_fav_u1', 'telegram:fav_loser', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING"
            )
            conn.execute(
                "INSERT INTO client_user_favorites (user_id, video_id, created_at) "
                "VALUES ('usr_fav_u1', 'telegram:fav_source2', CURRENT_TIMESTAMP) ON CONFLICT DO NOTHING"
            )
            conn.commit()

        # Listing favorites for User 1 must return EXACTLY ONE canonical keeper card
        res_u1_list = self.client.get("/api/v1/user/favorites", headers=auth_u1)
        self.assertEqual(res_u1_list.status_code, 200)
        fav_data = res_u1_list.json()
        self.assertEqual(fav_data["total_count"], 1)
        self.assertEqual(len(fav_data["items"]), 1)
        card = fav_data["items"][0]
        self.assertEqual(card["id"], "telegram:fav_keeper")
        self.assertEqual(card["title"], "Keeper Title")
        self.assertTrue(card.get("user_state", {}).get("is_favorite"))

        # 5. Catalog integration: GET /api/v1/catalog/videos populates user_state.is_favorite
        res_cat_u1 = self.client.get("/api/v1/catalog/videos?q=Keeper", headers=auth_u1)
        self.assertEqual(res_cat_u1.status_code, 200)
        u1_items = res_cat_u1.json().get("items", [])
        self.assertTrue(len(u1_items) >= 1)
        k_item_u1 = next(it for it in u1_items if it["id"] == "telegram:fav_keeper")
        self.assertTrue(k_item_u1.get("user_state", {}).get("is_favorite"))

        # User 2 sees is_favorite=False on the same catalog item
        res_cat_u2 = self.client.get("/api/v1/catalog/videos?q=Keeper", headers=auth_u2)
        self.assertEqual(res_cat_u2.status_code, 200)
        u2_items = res_cat_u2.json().get("items", [])
        k_item_u2 = next(it for it in u2_items if it["id"] == "telegram:fav_keeper")
        self.assertFalse(k_item_u2.get("user_state", {}).get("is_favorite"))

        # Filter by is_favorite=true
        res_fav_filter_u1 = self.client.get("/api/v1/catalog/videos?is_favorite=true&q=Keeper", headers=auth_u1)
        self.assertEqual(res_fav_filter_u1.status_code, 200)
        self.assertEqual(res_fav_filter_u1.json()["total_count"], 1)

        res_fav_filter_u2 = self.client.get("/api/v1/catalog/videos?is_favorite=true&q=Keeper", headers=auth_u2)
        self.assertEqual(res_fav_filter_u2.status_code, 200)
        self.assertEqual(res_fav_filter_u2.json()["total_count"], 0)

        # Catalog detail endpoint resolves aliases and populates user_state.is_favorite
        res_det_u1 = self.client.get("/api/v1/catalog/videos/telegram:fav_loser", headers=auth_u1)
        self.assertEqual(res_det_u1.status_code, 200)
        self.assertEqual(res_det_u1.json()["id"], "telegram:fav_keeper")
        self.assertTrue(res_det_u1.json().get("user_state", {}).get("is_favorite"))

        res_det_u2 = self.client.get("/api/v1/catalog/videos/telegram:fav_loser", headers=auth_u2)
        self.assertEqual(res_det_u2.status_code, 200)
        self.assertFalse(res_det_u2.json().get("user_state", {}).get("is_favorite"))

        # 6. Delete via loser alias ID removes favorite for user
        res_del_loser = self.client.delete("/api/v1/user/favorites/telegram:fav_loser", headers=auth_u1)
        self.assertEqual(res_del_loser.status_code, 200)
        self.assertEqual(res_del_loser.json()["video_id"], "telegram:fav_keeper")
        self.assertFalse(res_del_loser.json()["is_favorite"])

        # Check status is now False for all aliases
        self.assertFalse(self.client.get("/api/v1/user/favorites/telegram:fav_keeper", headers=auth_u1).json()["is_favorite"])
        self.assertFalse(self.client.get("/api/v1/user/favorites/telegram:fav_loser", headers=auth_u1).json()["is_favorite"])
        self.assertFalse(self.client.get("/api/v1/user/favorites/telegram:fav_source2", headers=auth_u1).json()["is_favorite"])
        self.assertEqual(self.client.get("/api/v1/user/favorites", headers=auth_u1).json()["total_count"], 0)

        # 7. Add via loser alias ID resolves to canonical keeper on write
        res_add_loser = self.client.put("/api/v1/user/favorites/telegram:fav_loser", headers=auth_u1)
        self.assertEqual(res_add_loser.status_code, 200)
        self.assertEqual(res_add_loser.json()["video_id"], "telegram:fav_keeper")
        self.assertTrue(res_add_loser.json()["is_favorite"])

        self.assertTrue(self.client.get("/api/v1/user/favorites/telegram:fav_keeper", headers=auth_u1).json()["is_favorite"])

        # 8. Ascending and descending latest sort and deterministic pagination
        with db.connect(self.db_url) as conn:
            conn.execute(
                "INSERT INTO videos (id, title, duration, categories, tags, file_size_bytes, first_seen, date_released) "
                "VALUES ('telegram:fav_keeper_newer', 'Newer Favorite', 2400, '[\"Action\"]'::jsonb, '[\"HD\"]'::jsonb, 600000000, '2026-11-25 12:00:00+00', '2026-11-25') "
                "ON CONFLICT (id) DO UPDATE SET title = 'Newer Favorite'"
            )
            conn.commit()

        res_add_newer = self.client.put("/api/v1/user/favorites/telegram:fav_keeper_newer", headers=auth_u1)
        self.assertEqual(res_add_newer.status_code, 200)

        # Default sort=latest defaults to descending (newer first)
        res_desc = self.client.get("/api/v1/user/favorites?sort=latest&direction=desc", headers=auth_u1)
        self.assertEqual(res_desc.status_code, 200)
        desc_items = res_desc.json()["items"]
        self.assertEqual(len(desc_items), 2)
        self.assertEqual(desc_items[0]["id"], "telegram:fav_keeper_newer")
        self.assertEqual(desc_items[1]["id"], "telegram:fav_keeper")

        # Explicit ascending direction (older first)
        res_asc = self.client.get("/api/v1/user/favorites?sort=latest&direction=asc", headers=auth_u1)
        self.assertEqual(res_asc.status_code, 200)
        asc_items = res_asc.json()["items"]
        self.assertEqual(len(asc_items), 2)
        self.assertEqual(asc_items[0]["id"], "telegram:fav_keeper")
        self.assertEqual(asc_items[1]["id"], "telegram:fav_keeper_newer")

        # Deterministic pagination with ascending order
        page1 = self.client.get("/api/v1/user/favorites?sort=latest&direction=asc&offset=0&limit=1", headers=auth_u1).json()
        self.assertEqual(page1["total_count"], 2)
        self.assertTrue(page1["has_more"])
        self.assertEqual(len(page1["items"]), 1)
        self.assertEqual(page1["items"][0]["id"], "telegram:fav_keeper")

        page2 = self.client.get("/api/v1/user/favorites?sort=latest&direction=asc&offset=1&limit=1", headers=auth_u1).json()
        self.assertEqual(page2["total_count"], 2)
        self.assertFalse(page2["has_more"])
        self.assertEqual(len(page2["items"]), 1)
        self.assertEqual(page2["items"][0]["id"], "telegram:fav_keeper_newer")

    def test_catalog_taxonomy_postgres_flow_and_filtering(self):
        """Taxonomy catalog API end-to-end: categories, studios, tags, performers list/detail and movie filtering."""
        auth = {"Authorization": "Bearer ah_tok_tax_u1"}

        with db.connect(self.db_url) as conn:
            tables = db.table_names(conn)

            # Dedicated owner and session for taxonomy catalog reads (independent of favorites fixtures)
            conn.execute(
                "INSERT INTO client_users (id, username, password_hash, is_admin, is_active) "
                "VALUES ('usr_tax_u1', 'tax_viewer', 'hash_tax', false, true) ON CONFLICT (id) DO NOTHING"
            )
            conn.execute(
                "INSERT INTO client_sessions (id, user_id, device_name, device_id, access_token_hash, refresh_token_hash, access_expires_at, refresh_expires_at) "
                "VALUES ('sess_tax_u1', 'usr_tax_u1', 'Desktop', 'dev_tax_1', %s, 'ref_tax_u1', '2030-01-01 00:00:00+00', '2030-01-01 00:00:00+00') ON CONFLICT (id) DO NOTHING",
                (_safe_hash_token("ah_tok_tax_u1"),),
            )

            # Seed videos with rich taxonomy metadata
            conn.execute(
                """INSERT INTO videos (
                       id, provider, title, duration, date_released, first_seen,
                       studio, plot, categories, tags, performers, playable, file_size_bytes
                   ) VALUES (
                       'telegram:tax_vid1', 'telegram', 'Taxonomy Feature One', 3600, '2026-06-01',
                       '2026-06-01 12:00:00+00', 'Dragon Media', 'Sample plot one',
                       '["Action", "Romance"]'::jsonb, '["4K", "Pool"]'::jsonb,
                       '["Alex Frost", "Sam River"]'::jsonb,
                       1, 1000000000
                   ) ON CONFLICT (id) DO UPDATE SET title = 'Taxonomy Feature One'"""
            )
            conn.execute(
                """INSERT INTO videos (
                       id, provider, title, duration, date_released, first_seen,
                       studio, plot, categories, tags, performers, playable, file_size_bytes
                   ) VALUES (
                       'telegram:tax_vid2', 'telegram', 'Taxonomy Feature Two', 2400, '2026-06-02',
                       '2026-06-02 12:00:00+00', 'Dragon Media', 'Sample plot two',
                       '["Action", "Comedy"]'::jsonb, '["HD", "Outdoor"]'::jsonb,
                       '["Alex Frost"]'::jsonb,
                       1, 800000000
                   ) ON CONFLICT (id) DO UPDATE SET title = 'Taxonomy Feature Two'"""
            )
            conn.execute(
                """INSERT INTO videos (
                       id, provider, title, duration, date_released, first_seen,
                       studio, plot, categories, tags, performers, playable, file_size_bytes
                   ) VALUES (
                       'telegram:tax_vid3', 'telegram', 'Taxonomy Feature Three', 1800, '2026-06-03',
                       '2026-06-03 12:00:00+00', 'Alpha Studio', 'Sample plot three',
                       '["Sci-Fi"]'::jsonb, '["HD"]'::jsonb,
                       '["Morgan Chase"]'::jsonb,
                       1, 600000000
                   ) ON CONFLICT (id) DO UPDATE SET title = 'Taxonomy Feature Three'"""
            )

            # Seed performer_catalog, performer_bios, and performer_photos if present
            if "performer_catalog" in tables:
                conn.execute(
                    "INSERT INTO performer_catalog (id, source, source_id, name, active) "
                    "VALUES ('stashdb:perf-uuid-1', 'stashdb', 'uuid-1', 'Alex Frost', true) "
                    "ON CONFLICT (id) DO UPDATE SET name = 'Alex Frost'"
                )
                conn.execute(
                    "INSERT INTO performer_catalog (id, source, source_id, name, active) "
                    "VALUES ('stashdb:perf-uuid-2', 'stashdb', 'uuid-2', 'Morgan Chase', true) "
                    "ON CONFLICT (id) DO UPDATE SET name = 'Morgan Chase'"
                )
            if "performer_bios" in tables:
                conn.execute(
                    "INSERT INTO performer_bios (name, bio, birth, nationality, aliases) "
                    "VALUES ('Alex Frost', 'Award-winning performer.', '1995-04-12', 'US', '[\"Alexander Frost\", \"A. Frost\"]'::jsonb) "
                    "ON CONFLICT (name) DO UPDATE SET bio = 'Award-winning performer.'"
                )
            if "performer_photos" in tables:
                conn.execute(
                    "INSERT INTO performer_photos (name, thumb) "
                    "VALUES ('Alex Frost', 'https://example.com/alex.jpg') "
                    "ON CONFLICT (name) DO UPDATE SET thumb = 'https://example.com/alex.jpg'"
                )
            conn.commit()

        # 1. Categories endpoint
        res_cat = self.client.get("/api/v1/catalog/categories", headers=auth)
        self.assertEqual(res_cat.status_code, 200)
        self.assertEqual(res_cat.headers.get("cache-control"), "private, no-store")
        cat_data = res_cat.json()
        self.assertEqual(cat_data["total_count"], 4)  # Action, Comedy, Romance, Sci-Fi
        cat_names = [it["name"] for it in cat_data["items"]]
        self.assertIn("Action", cat_names)
        action_item = next(it for it in cat_data["items"] if it["name"] == "Action")
        self.assertEqual(action_item["count"], 2)  # In vid1 and vid2

        # Categories search filter
        res_cat_q = self.client.get("/api/v1/catalog/categories?q=Action", headers=auth).json()
        self.assertEqual(res_cat_q["total_count"], 1)
        self.assertEqual(res_cat_q["items"][0]["name"], "Action")

        # 2. Studios endpoint
        res_std = self.client.get("/api/v1/catalog/studios", headers=auth)
        self.assertEqual(res_std.status_code, 200)
        std_data = res_std.json()
        self.assertEqual(std_data["total_count"], 2)  # Dragon Media, Alpha Studio
        dragon_item = next(it for it in std_data["items"] if it["name"] == "Dragon Media")
        self.assertEqual(dragon_item["count"], 2)  # vid1, vid2

        # Studios search filter
        res_std_q = self.client.get("/api/v1/catalog/studios?q=Alpha", headers=auth).json()
        self.assertEqual(res_std_q["total_count"], 1)
        self.assertEqual(res_std_q["items"][0]["name"], "Alpha Studio")

        # 3. Tags endpoint
        res_tag = self.client.get("/api/v1/catalog/tags", headers=auth)
        self.assertEqual(res_tag.status_code, 200)
        tag_data = res_tag.json()
        self.assertEqual(tag_data["total_count"], 4)  # 4K, HD, Outdoor, Pool
        hd_item = next(it for it in tag_data["items"] if it["name"] == "HD")
        self.assertEqual(hd_item["count"], 2)

        # 4. Performers endpoint (list)
        res_perf = self.client.get("/api/v1/catalog/performers", headers=auth)
        self.assertEqual(res_perf.status_code, 200)
        perf_data = res_perf.json()
        self.assertEqual(perf_data["total_count"], 3)  # Alex Frost, Morgan Chase, Sam River
        alex_ref = next(it for it in perf_data["items"] if it["name"] == "Alex Frost")
        self.assertEqual(alex_ref["id"], "stashdb:perf-uuid-1")
        self.assertEqual(alex_ref["video_count"], 2)
        self.assertIsNone(alex_ref.get("photo_url"))

        sam_ref = next(it for it in perf_data["items"] if it["name"] == "Sam River")
        self.assertEqual(sam_ref["id"], "name:Sam River")
        self.assertEqual(sam_ref["video_count"], 1)

        # Performers search and letter filters
        res_perf_q = self.client.get("/api/v1/catalog/performers?q=Morgan", headers=auth).json()
        self.assertEqual(res_perf_q["total_count"], 1)
        self.assertEqual(res_perf_q["items"][0]["name"], "Morgan Chase")

        res_perf_let = self.client.get("/api/v1/catalog/performers?letter=A", headers=auth).json()
        self.assertEqual(res_perf_let["total_count"], 1)
        self.assertEqual(res_perf_let["items"][0]["name"], "Alex Frost")

        # Performers sorting by video_count DESC
        res_perf_sort = self.client.get("/api/v1/catalog/performers?sort=video_count&direction=desc", headers=auth).json()
        self.assertEqual(res_perf_sort["items"][0]["name"], "Alex Frost")
        self.assertEqual(res_perf_sort["items"][0]["video_count"], 2)

        # 5. Performer detail endpoint
        res_det_vetted = self.client.get("/api/v1/catalog/performers/stashdb:perf-uuid-1", headers=auth)
        self.assertEqual(res_det_vetted.status_code, 200)
        det_alex = res_det_vetted.json()
        self.assertEqual(det_alex["id"], "stashdb:perf-uuid-1")
        self.assertEqual(det_alex["name"], "Alex Frost")
        self.assertEqual(det_alex["video_count"], 2)
        self.assertEqual(det_alex.get("bio"), "Award-winning performer.")
        self.assertEqual(det_alex.get("nationality"), "US")
        self.assertEqual(det_alex.get("birth"), "1995-04-12")
        self.assertIn("Alexander Frost", det_alex.get("aliases", []))
        self.assertEqual(det_alex.get("photo_url"), "/api/v1/artwork/performer/stashdb%3Aperf-uuid-1")
        self.assertNotIn("example.com", res_det_vetted.text)

        # Detail lookup by explicit name: prefix resolves vetted catalog
        res_det_by_name = self.client.get("/api/v1/catalog/performers/name:Alex%20Frost", headers=auth)
        self.assertEqual(res_det_by_name.status_code, 200)
        self.assertEqual(res_det_by_name.json()["id"], "stashdb:perf-uuid-1")

        # Detail lookup by bare name without name: prefix is ID-only and returns 404
        res_det_bare = self.client.get("/api/v1/catalog/performers/Alex%20Frost", headers=auth)
        self.assertEqual(res_det_bare.status_code, 404)
        self.assertEqual(res_det_bare.headers.get("cache-control"), "private, no-store")

        # Detail lookup for 404
        res_det_404 = self.client.get("/api/v1/catalog/performers/nonexistent_performer_xyz", headers=auth)
        self.assertEqual(res_det_404.status_code, 404)
        self.assertEqual(res_det_404.headers.get("cache-control"), "private, no-store")

        # 6. Filter videos catalog by taxonomy
        # Filter by category
        res_vid_cat = self.client.get("/api/v1/catalog/videos?category=Action", headers=auth).json()
        self.assertEqual(res_vid_cat["total_count"], 2)

        # Filter by studio
        res_vid_std = self.client.get("/api/v1/catalog/videos?studio=Dragon%20Media", headers=auth).json()
        self.assertEqual(res_vid_std["total_count"], 2)

        # Filter by performer vetted ID
        res_vid_pid = self.client.get("/api/v1/catalog/videos?performer_id=stashdb:perf-uuid-1", headers=auth).json()
        self.assertEqual(res_vid_pid["total_count"], 2)

        # Filter by performer name
        res_vid_pname = self.client.get("/api/v1/catalog/videos?performer=Alex%20Frost", headers=auth).json()
        self.assertEqual(res_vid_pname["total_count"], 2)


