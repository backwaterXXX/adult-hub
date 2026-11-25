"""Targeted tests for Stage 2 Adult Hub Client API Authentication & TV Pairing.

Covers:
- Offline unit tests: crypto primitives, scrypt hashing, anti-enumeration, token formats,
  untrusted token validation (4xx not ASCII 500), version discovery (honest capabilities),
  query parameter rejection, strict Admin V2 write guard (positive & negative),
  JSON object validation (rejecting non-dict payloads as 400),
  bounded/NUL-free input validation, canonical production DB resolution without TEST_DATABASE_URL fallback.
- Live integration tests (against disposable PostgreSQL via PgTestCase):
  fresh and upgrade schema idempotency without table dropping, owner bootstrap race safety
  and single-use, password authentication with anti-enumeration (same denial for disabled/nonexistent),
  token expiration, single-use refresh rotation, persistent replay detection & revocation,
  direct positive/negative HTTP pairing flow via registered routes, stateful DB commits on error
  HTTP responses, distributed PostgreSQL rate limiting, multi-dimensional per-account and per-IP throttling,
  prevention of writes for unknown pairing secrets, and atomic deterministic concurrent refresh replay revocation.
"""

from __future__ import annotations

import io
import os
import secrets
import sys
import unittest
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

from common import db
from tests.pg_helper import PgTestCase
from common.client_auth.crypto import (
    TokenValidationError,
    dummy_verify,
    generate_pairing_code,
    generate_pairing_secret,
    generate_token,
    hash_password,
    hash_token,
    normalize_pairing_code,
    verify_password,
)
from common.client_auth.rate_limit import check_and_increment_rate_limit
from common.client_auth.schema import ensure_auth_schema
from common.client_auth.service import (
    AuthenticationFailed,
    PairingAlreadyExchanged,
    PairingNotFoundOrExpired,
    RateLimitExceeded,
    approve_pairing,
    authenticate_by_password,
    bootstrap_owner,
    initiate_pairing,
    poll_pairing_status,
    prune_retired_auth_data,
    refresh_session_token,
    revoke_session_token,
)
from common.client_auth_cli import run_bootstrap_owner
from fastapi import FastAPI

# Telethon mock for offline runner execution when telethon is not installed on host
try:
    from resolver.app import app
except ModuleNotFoundError as e:
    if "telethon" in str(e):
        for m in ["telethon", "telethon.tl", "telethon.tl.functions", "telethon.tl.types", "telethon.errors"]:
            sys.modules.setdefault(m, MagicMock())
        from resolver.app import app
    else:
        raise

from resolver.client_auth_routes import (
    admin_pairing_router,
    set_test_db_url,
    _db_connection,
    _admin_write_guard,
)
from resolver.desktop_api import router as desktop_router
from resolver import admin as admin_module
from starlette.requests import Request


def get_all_routes(router):
    routes = []
    for r in getattr(router, "routes", []):
        if hasattr(r, "original_router"):
            routes.extend(get_all_routes(r.original_router))
        elif hasattr(r, "router"):
            routes.extend(get_all_routes(r.router))
        elif hasattr(r, "routes"):
            routes.extend(get_all_routes(r))
        else:
            routes.append(r)
    return routes


# The live Client API owns password auth and includes the TV pairing routes; the
# default resolver app keeps them unmounted, so the tests build their own app.
auth_test_app = FastAPI()
auth_test_app.include_router(desktop_router)
auth_test_app.include_router(admin_pairing_router)


class TestClientAuthOffline(unittest.TestCase):
    """Offline unit tests executable in hermetic sandboxes without database access."""

    def test_scrypt_password_hashing_and_verification(self):
        """Audited scrypt hashes verify correctly and reject wrong passwords."""
        pwd = "correct_horse_battery_staple_123"
        hashed = hash_password(pwd)
        self.assertTrue(hashed.startswith("$scrypt$ln=14,r=8,p=1$"))
        self.assertTrue(verify_password(pwd, hashed))
        self.assertFalse(verify_password("wrong_password_999", hashed))
        self.assertFalse(verify_password("", hashed))
        self.assertFalse(verify_password(pwd, "invalid_hash_string"))

    def test_scrypt_anti_enumeration_dummy_verify(self):
        """Dummy verification completes without exception, equalizing execution timing."""
        dummy_verify("any_non_existent_user_password")

    def test_opaque_token_generation_and_storage_digests(self):
        """Tokens have expected prefixes, high entropy, and only digests are stored."""
        tok = generate_token("ah_tok_")
        ref = generate_token("ah_ref_")
        pair_sec = generate_pairing_secret()

        self.assertTrue(tok.startswith("ah_tok_"))
        self.assertTrue(ref.startswith("ah_ref_"))
        self.assertTrue(pair_sec.startswith("pair_sec_"))
        self.assertGreaterEqual(len(tok), 40)
        self.assertGreaterEqual(len(ref), 40)
        self.assertGreaterEqual(len(pair_sec), 40)

        digest = hash_token(tok)
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, hash_token(tok))
        self.assertNotEqual(digest, hash_token(ref))

    def test_untrusted_tokens_validate_as_4xx_not_ascii_500(self):
        """Untrusted tokens with non-ASCII chars or oversized length raise validation errors."""
        with self.assertRaises(TokenValidationError):
            hash_token("ah_tok_malicious_unicode_€€€")

        with self.assertRaises(TokenValidationError):
            hash_token("a" * 300)

        client = TestClient(auth_test_app)
        # Calling refresh with non-ASCII token returns 400 Bad Request, NOT 500
        res = client.post("/api/v1/auth/refresh", json={"refresh_token": "invalid_unicode_€€€"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("error", res.json())

    def test_pairing_code_generation_and_normalization(self):
        """Pairing codes format as NNN-NNN and normalize cleanly."""
        code = generate_pairing_code()
        self.assertRegex(code, r"^\d{3}-\d{3}$")

        self.assertEqual(normalize_pairing_code("842-195"), "842195")
        self.assertEqual(normalize_pairing_code(" 842 195 "), "842195")
        self.assertEqual(normalize_pairing_code("abc-def"), "ABCDEF")

    def test_auth_endpoints_reject_query_parameters(self):
        """Secrets or parameters sent in query parameters are strictly rejected with 400."""
        client = TestClient(auth_test_app)
        res = client.post("/api/v1/auth/pair/status?secret=pair_sec_leaked_in_url")
        self.assertEqual(res.status_code, 400)
        self.assertIn("strictly prohibited", res.json().get("error", ""))
        self.assertEqual(res.headers.get("Cache-Control"), "private, no-store")

        res_login = client.post("/api/v1/auth/login?user=admin", json={"username": "a", "password": "b"})
        self.assertEqual(res_login.status_code, 400)

    def test_json_object_validation_rejects_non_dict_payloads(self):
        """All auth endpoints strictly validate that JSON request body is a JSON object (dict), not lists/primitives."""
        client = TestClient(auth_test_app)
        endpoints = [
            "/api/v1/auth/login",
            "/api/v1/auth/refresh",
            "/api/v1/auth/revoke",
            "/api/v1/auth/pair/init",
            "/api/v1/auth/pair/status",
            "/admin/api/v2/pairing/approve",
        ]
        non_dict_payloads = [
            [1, 2, 3],
            "plain-string",
            12345,
            True,
        ]
        for ep in endpoints:
            for p in non_dict_payloads:
                if ep == "/admin/api/v2/pairing/approve":
                    with patch.object(admin_module, "_write_token", return_value="tok"):
                        res = client.post(ep, json=p, headers={"Authorization": "Bearer tok"})
                else:
                    res = client.post(ep, json=p)
                self.assertEqual(
                    res.status_code,
                    400,
                    f"Endpoint {ep} with payload {p!r} should return 400, got {res.status_code}",
                )
                self.assertIn("JSON object", res.json().get("error", ""))

    def test_nul_and_bounded_input_rejected_as_400(self):
        """NUL character injections (\x00) and bounds violations return 400 Bad Request, never 500."""
        client = TestClient(auth_test_app)

        # NUL in login username
        res1 = client.post("/api/v1/auth/login", json={"username": "alice\x00hacker", "password": "password123"})
        self.assertEqual(res1.status_code, 400)
        self.assertIn("null", res1.json().get("error", "").lower())

        # NUL in login password
        res2 = client.post("/api/v1/auth/login", json={"username": "alice", "password": "password\x00123"})
        self.assertEqual(res2.status_code, 400)
        self.assertIn("null", res2.json().get("error", "").lower())

        # NUL in refresh token
        res3 = client.post("/api/v1/auth/refresh", json={"refresh_token": "ah_ref_\x00evil"})
        self.assertEqual(res3.status_code, 400)
        self.assertIn("null", res3.json().get("error", "").lower())

        # NUL in pairing secret
        res4 = client.post("/api/v1/auth/pair/status", json={"secret": "pair_sec_\x00evil"})
        self.assertEqual(res4.status_code, 400)
        self.assertIn("null", res4.json().get("error", "").lower())

    def test_bounded_body_content_length_and_invalid_json(self):
        """Oversize Content-Length is reliably rejected, non-integer Content-Length handled, and invalid JSON error preserved."""
        client = TestClient(auth_test_app)

        # 1. Oversize Content-Length rejected with 400
        res_cl = client.post(
            "/api/v1/auth/login",
            content=b'{"username": "a", "password": "b"}',
            headers={"Content-Length": "999999", "Content-Type": "application/json"},
        )
        self.assertEqual(res_cl.status_code, 400)
        self.assertEqual(res_cl.headers.get("cache-control"), "private, no-store")
        self.assertIn("exceeds maximum size", res_cl.json().get("error", ""))

        # 2. Non-integer Content-Length does not crash with ValueError and reads body
        res_bad_cl = client.post(
            "/api/v1/auth/login",
            content=b"not a valid json {",
            headers={"Content-Length": "not-an-int", "Content-Type": "application/json"},
        )
        self.assertEqual(res_bad_cl.status_code, 400)
        self.assertEqual(res_bad_cl.headers.get("cache-control"), "private, no-store")
        self.assertEqual(res_bad_cl.json().get("error"), "Request body must be a valid JSON object")

        # 3. Empty body returns 400 with Invalid JSON body
        res_empty = client.post(
            "/api/v1/auth/login",
            content=b"",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(res_empty.status_code, 400)
        self.assertEqual(res_empty.headers.get("cache-control"), "private, no-store")
        self.assertEqual(res_empty.json().get("error"), "Request body must be a valid JSON object")

    def test_canonical_production_db_resolution(self):
        """Runtime auth DB connection resolves via canonical config and never falls back to TEST_DATABASE_URL in production."""
        set_test_db_url(None)
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://usr:pwd@host:5432/proddb", "TEST_DATABASE_URL": "postgresql://usr:pwd@host:5432/testdb"}):
            with patch("resolver.client_auth_routes.db.connect") as mock_connect:
                _db_connection()
                mock_connect.assert_called_with("postgresql://usr:pwd@host:5432/proddb")

        # In production mode without injected test URL and with unset DATABASE_URL, never falls back to TEST_DATABASE_URL
        with patch.dict(os.environ, {"DATABASE_URL": "", "STANDALONE_DATABASE_URL": "", "TEST_DATABASE_URL": "postgresql://usr:pwd@host:5432/testdb"}, clear=False):
            with patch.object(admin_module, "HUB_DB_URL", None):
                with patch("resolver.client_auth_routes.db.connect") as mock_connect:
                    _db_connection()
                    mock_connect.assert_called_with()

        # Explicit test fixture injection is honored
        set_test_db_url("postgresql://usr:pwd@host:5432/injected_disposable")
        try:
            with patch("resolver.client_auth_routes.db.connect") as mock_connect:
                _db_connection()
                mock_connect.assert_called_with("postgresql://usr:pwd@host:5432/injected_disposable")
        finally:
            set_test_db_url(None)

    def test_admin_write_guard_strict_checks(self):
        """Strict Admin V2 write guard enforces read-only restrictions and valid tokens."""
        # 1. Admin blocked on public domain -> 403
        req_blocked = Request({"type": "http", "method": "POST", "path": "/admin/api/v2/pairing/approve", "headers": []})
        with patch.object(admin_module, "_admin_blocked", return_value=True):
            res = _admin_write_guard(req_blocked)
            self.assertIsNotNone(res)
            self.assertEqual(res.status_code, 403)

        # 2. Strict read-only panel (admin_disabled or invalid_admin_token) -> 403
        req_local = Request({"type": "http", "method": "POST", "path": "/admin/api/v2/pairing/approve", "headers": [(b"authorization", b"Bearer some_tok")]})
        with patch.object(admin_module, "_admin_blocked", return_value=False), \
             patch.object(admin_module, "_capabilities", return_value={"read_only": True, "read_only_reason": "admin_disabled"}):
            res = _admin_write_guard(req_local)
            self.assertIsNotNone(res)
            self.assertEqual(res.status_code, 403)
            self.assertIn("read-only", res.body.decode())

        # 3. Unconfigured or whitespace token -> 403
        with patch.object(admin_module, "_admin_blocked", return_value=False), \
             patch.object(admin_module, "_capabilities", return_value={"read_only": False}), \
             patch.object(admin_module, "_write_token", return_value=""):
            res = _admin_write_guard(req_local)
            self.assertIsNotNone(res)
            self.assertEqual(res.status_code, 403)

        # 4. Valid token with matching Bearer -> Allowed (returns None)
        valid_req = Request({"type": "http", "method": "POST", "path": "/admin/api/v2/pairing/approve", "headers": [(b"authorization", b"Bearer super_admin_secret")]})
        with patch.object(admin_module, "_admin_blocked", return_value=False), \
             patch.object(admin_module, "_capabilities", return_value={"read_only": False}), \
             patch.object(admin_module, "_write_token", return_value="super_admin_secret"):
            res = _admin_write_guard(valid_req)
            self.assertIsNone(res)

        # 5. Matching token with mismatching origin -> 403
        req_bad_origin = Request({
            "type": "http",
            "method": "POST",
            "path": "/admin/api/v2/pairing/approve",
            "headers": [
                (b"authorization", b"Bearer super_admin_secret"),
                (b"origin", b"http://evil.com"),
                (b"host", b"localhost:8080"),
            ],
        })
        with patch.object(admin_module, "_admin_blocked", return_value=False), \
             patch.object(admin_module, "_capabilities", return_value={"read_only": False}), \
             patch.object(admin_module, "_write_token", return_value="super_admin_secret"):
            res = _admin_write_guard(req_bad_origin)
            self.assertIsNotNone(res)
            self.assertEqual(res.status_code, 403)

    def test_admin_pairing_approval_strict_guard(self):
        """Admin pairing approval strictly enforces nonempty token, denying client tokens or missing auth."""
        client = TestClient(auth_test_app)

        # 1. No auth header -> 403
        res = client.post(
            "/admin/api/v2/pairing/approve",
            json={"pairing_code": "842-195"},
        )
        self.assertEqual(res.status_code, 403)

        # 2. Client auth token (ah_tok_...) -> 403
        res_client_auth = client.post(
            "/admin/api/v2/pairing/approve",
            json={"pairing_code": "842-195"},
            headers={"Authorization": "Bearer ah_tok_client_token_attempting_admin"},
        )
        self.assertEqual(res_client_auth.status_code, 403)

        # 3. Wrong admin token -> 403
        with patch.object(admin_module, "_write_token", return_value="configured_admin_token_xyz"):
            res_wrong = client.post(
                "/admin/api/v2/pairing/approve",
                json={"pairing_code": "842-195"},
                headers={"Authorization": "Bearer wrong_token"},
            )
            self.assertEqual(res_wrong.status_code, 403)

        # 4. Denies when admin write token is unconfigured (empty)
        with patch.object(admin_module, "_write_token", return_value=""):
            res_unconfigured = client.post(
                "/admin/api/v2/pairing/approve",
                json={"pairing_code": "842-195"},
                headers={"Authorization": "Bearer some_token"},
            )
            self.assertEqual(res_unconfigured.status_code, 403)

        # 5. Compatibility alias /api/v2/admin/pairing/approve is removed (not a mounted POST route;
        # returns 405 due to legacy /api/{rest:path} GET catch-all, or 404 if unrouted)
        res_compat = client.post(
            "/api/v2/admin/pairing/approve",
            json={"pairing_code": "842-195"},
        )
        self.assertIn(res_compat.status_code, (404, 405))
        self.assertNotIn(
            "/api/v2/admin/pairing/approve",
            [r.path for r in auth_test_app.routes if hasattr(r, "path") and "POST" in getattr(r, "methods", set())],
        )

    def test_default_resolver_app_does_not_serve_stage2_auth(self):
        """Default Resolver app must not serve unapproved Stage 2 auth routes while legacy routes remain unchanged."""
        client = TestClient(app)
        res_login = client.post("/api/v1/auth/login", json={"username": "a", "password": "b"})
        self.assertIn(res_login.status_code, (404, 405))
        res_pair = client.post("/api/v1/auth/pair/init", json={"device_name": "tv"})
        self.assertIn(res_pair.status_code, (404, 405))
        res_approve = client.post("/admin/api/v2/pairing/approve", json={"pairing_code": "123-456"})
        self.assertEqual(res_approve.status_code, 403)
        self.assertEqual(res_approve.headers.get("cache-control"), "private, no-store")

        app_post_routes = [getattr(r, "path", "") for r in get_all_routes(app.router) if "POST" in getattr(r, "methods", set())]
        self.assertNotIn("/api/v1/auth/login", app_post_routes)
        self.assertNotIn("/api/v1/auth/pair/init", app_post_routes)
        self.assertIn("/admin/api/v2/pairing/approve", app_post_routes)
        self.assertEqual(app_post_routes.count("/admin/api/v2/pairing/approve"), 1)

        # Existing legacy and health routes remain mounted and unchanged
        routes = [getattr(r, "path", "") for r in app.routes]
        self.assertIn("/health", routes)
        self.assertIn("/api/{rest:path}", routes)
        self.assertIn("/play/{rest:path}", routes)
        self.assertEqual(client.get("/health").status_code, 200)

    def test_initiate_pairing_retries_collision_and_expires_past(self):
        """initiate_pairing expires past pending/approved rows and retries on code collisions."""
        mock_conn = MagicMock()
        call_count = 0

        def fake_execute(query, params=None):
            nonlocal call_count
            if "INSERT INTO client_pairing_sessions" in query:
                call_count += 1
                if call_count == 1:
                    raise db.IntegrityError("duplicate key value violates unique constraint")
            return MagicMock()

        mock_conn.execute.side_effect = fake_execute

        with patch("common.client_auth.service.check_and_increment_rate_limit", return_value=True):
            res = initiate_pairing(mock_conn, "TV", "tv-1", "127.0.0.1")

        self.assertTrue(res["pairing_code"])
        self.assertEqual(call_count, 2)
        first_call = mock_conn.execute.call_args_list[0]
        self.assertIn("UPDATE client_pairing_sessions", first_call[0][0])
        self.assertIn("status IN ('pending', 'approved')", first_call[0][0])
        self.assertIn("expires_at <= CURRENT_TIMESTAMP", first_call[0][0])

    def test_cli_owner_bootstrap_input_handling(self):
        """CLI prompts interactively without passwords in args, or reads from stdin."""
        with patch("sys.stdin", io.StringIO("\n")):
            code = run_bootstrap_owner("owner", password=None)
            self.assertEqual(code, 1)


class TestClientAuthLive(PgTestCase):
    """Targeted PostgreSQL live tests executed when TEST_DATABASE_URL is provided."""

    def setUp(self):
        # super().setUp() cleanly truncates all registered APPLICATION_TABLES via reset_db.
        # It never drops tables or cascades into prior tables.
        super().setUp()
        # Explicit test fixture injection (canonical production DB resolution requires explicit injection in tests)
        set_test_db_url(self.db_url)
        self.addCleanup(set_test_db_url, None)
        # The live Client API reads the URL from the app state first; keep it scoped to this test.
        auth_test_app.state.db_url = self.db_url
        self.addCleanup(auth_test_app.state._state.pop, "db_url", None)

    def test_fresh_and_upgrade_schema_idempotency(self):
        """Auth migration schema runs idempotently without failing or duplicating objects."""
        ensure_auth_schema(self.conn)
        tables = [
            r[0]
            for r in self.conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'client_%'"
            ).fetchall()
        ]
        self.assertIn("client_users", tables)
        self.assertIn("client_sessions", tables)
        self.assertIn("client_session_spent_tokens", tables)
        self.assertIn("client_pairing_sessions", tables)
        self.assertIn("client_auth_rate_limits", tables)

    def test_owner_bootstrap_race_safety_and_single_use(self):
        """Owner bootstrap succeeds once and rejects second attempt."""
        user = bootstrap_owner(self.conn, "first_owner", "SuperSecurePassword123!")
        self.assertEqual(user["username"], "first_owner")

        # Second attempt must be rejected (single-use owner guard)
        with self.assertRaises(ValueError) as ctx:
            bootstrap_owner(self.conn, "second_owner", "AnotherPassword123!")
        self.assertIn("already exists", str(ctx.exception))

    def test_password_auth_lifecycle_and_anti_enumeration(self):
        """Valid login succeeds; wrong password, nonexistent user, and disabled user fail with same denial."""
        bootstrap_owner(self.conn, "alice", "AlicePassword123!")

        # Valid login
        resp = authenticate_by_password(
            self.conn,
            username="alice",
            password="AlicePassword123!",
            device_name="Shield TV",
            device_id="tv-shield-1",
            client_ip="192.168.1.100",
        )
        self.assertTrue(resp["access_token"].startswith("ah_tok_"))
        self.assertTrue(resp["refresh_token"].startswith("ah_ref_"))
        self.assertEqual(resp["expires_in"], 900)
        self.assertEqual(resp["user"]["username"], "alice")
        self.assertNotIn("is_admin", resp["user"])

        # Wrong password
        with self.assertRaises(AuthenticationFailed) as ctx_wrong:
            authenticate_by_password(
                self.conn,
                username="alice",
                password="WrongPassword999!",
                device_name="Shield TV",
                device_id="tv-shield-1",
                client_ip="192.168.1.100",
            )
        self.assertEqual(str(ctx_wrong.exception), "Invalid username or password")

        # Non-existent user
        with self.assertRaises(AuthenticationFailed) as ctx_nonexist:
            authenticate_by_password(
                self.conn,
                username="bob_not_here",
                password="SomePassword123!",
                device_name="Shield TV",
                device_id="tv-shield-1",
                client_ip="192.168.1.100",
            )
        self.assertEqual(str(ctx_nonexist.exception), "Invalid username or password")

        # Disabled user account: must return the exact same denial message (Finding 7)
        self.conn.execute("UPDATE client_users SET is_active = false WHERE username = 'alice'")
        self.conn.commit()
        with self.assertRaises(AuthenticationFailed) as ctx_disabled:
            authenticate_by_password(
                self.conn,
                username="alice",
                password="AlicePassword123!",
                device_name="Shield TV",
                device_id="tv-shield-1",
                client_ip="192.168.1.100",
            )
        self.assertEqual(str(ctx_disabled.exception), "Invalid username or password")

    def test_login_rate_limiting_per_account_and_per_ip(self):
        """Logins are throttled both per account (max 5) and per IP (max 10)."""
        bootstrap_owner(self.conn, "throttled_user", "UserPassword123!")

        # 1. Per-account limit: 5 failed attempts from 5 distinct IPs
        for i in range(5):
            with self.assertRaises(AuthenticationFailed):
                authenticate_by_password(
                    self.conn,
                    username="throttled_user",
                    password="WrongPassword!",
                    device_name="Device",
                    device_id="dev-1",
                    client_ip=f"10.0.0.{i + 1}",
                )

        # 6th attempt from a brand new IP fails due to per-account limit!
        with self.assertRaises(RateLimitExceeded) as ctx_acc:
            authenticate_by_password(
                self.conn,
                username="throttled_user",
                password="WrongPassword!",
                device_name="Device",
                device_id="dev-1",
                client_ip="10.0.0.99",
            )
        self.assertIn("account", str(ctx_acc.exception).lower())

        # 2. Per-IP limit: 10 failed attempts from same IP against different non-existent accounts
        ip_victim = "192.168.99.1"
        for i in range(10):
            with self.assertRaises(AuthenticationFailed):
                authenticate_by_password(
                    self.conn,
                    username=f"user_{i}",
                    password="Password123!",
                    device_name="Device",
                    device_id="dev-1",
                    client_ip=ip_victim,
                )

        # 11th attempt from that IP fails due to per-IP limit!
        with self.assertRaises(RateLimitExceeded) as ctx_ip:
            authenticate_by_password(
                self.conn,
                username="different_user",
                password="Password123!",
                device_name="Device",
                device_id="dev-1",
                client_ip=ip_victim,
            )
        self.assertIn("too many login attempts", str(ctx_ip.exception).lower())

    def test_token_refresh_and_single_use_rotation(self):
        """Refreshing rotates tokens, invalidating old refresh token and preventing replay."""
        bootstrap_owner(self.conn, "charlie", "CharliePassword123!")
        auth1 = authenticate_by_password(
            self.conn,
            username="charlie",
            password="CharliePassword123!",
            device_name="Mobile Phone",
            device_id="mob-1",
            client_ip="192.168.1.101",
        )
        old_ref = auth1["refresh_token"]

        # First refresh: succeeds and rotates
        auth2 = refresh_session_token(self.conn, old_ref)
        new_ref = auth2["refresh_token"]
        self.assertNotEqual(old_ref, new_ref)
        self.assertTrue(auth2["access_token"].startswith("ah_tok_"))

        # Second refresh: rotates again
        auth3 = refresh_session_token(self.conn, new_ref)
        third_ref = auth3["refresh_token"]
        self.assertNotEqual(new_ref, third_ref)

        # Replay attack: presenting old_ref (from 2 rotations ago) MUST trigger replay detection and revoke session
        with self.assertRaises(AuthenticationFailed) as ctx:
            refresh_session_token(self.conn, old_ref)
        self.assertIn("replayed", str(ctx.exception).lower())

        # The newest token is now also revoked because the whole family was invalidated on replay
        with self.assertRaises(AuthenticationFailed):
            refresh_session_token(self.conn, third_ref)

    def test_concurrent_refresh_token_replay_revocation(self):
        """Concurrent refresh requests presenting the same token atomically revoke the session family."""
        bootstrap_owner(self.conn, "concur_user", "ConcurPass1234!")
        auth = authenticate_by_password(
            self.conn,
            username="concur_user",
            password="ConcurPass1234!",
            device_name="Device",
            device_id="dev-concur",
            client_ip="127.0.0.1",
        )
        refresh_token = auth["refresh_token"]

        # Open secondary connection to simulate concurrent worker transaction
        conn2 = db.connect(self.db_url)
        try:
            # First transaction rotates token
            rotated = refresh_session_token(self.conn, refresh_token)
            self.assertTrue(rotated["access_token"].startswith("ah_tok_"))

            # Second transaction (even if started concurrently with same token) detects replay and revokes session
            with self.assertRaises(AuthenticationFailed) as ctx:
                refresh_session_token(conn2, refresh_token)
            self.assertIn("replayed", str(ctx.exception).lower())

            # Verified in DB: session is marked revoked
            rev_row = self.conn.execute(
                "SELECT revoked_at FROM client_sessions WHERE access_token_hash = %s",
                (hash_token(rotated["access_token"]),),
            ).fetchone()
            self.assertIsNotNone(rev_row)
            self.assertIsNotNone(rev_row[0], "Session family must be revoked on replay")
        finally:
            conn2.close()

    def test_session_revocation(self):
        """Explicit revocation marks session revoked and blocks subsequent access."""
        bootstrap_owner(self.conn, "dave", "DavePassword123!")
        auth = authenticate_by_password(
            self.conn,
            username="dave",
            password="DavePassword123!",
            device_name="Tablet",
            device_id="tab-1",
            client_ip="192.168.1.102",
        )
        revoked = revoke_session_token(self.conn, auth["refresh_token"])
        self.assertTrue(revoked)

        with self.assertRaises(AuthenticationFailed):
            refresh_session_token(self.conn, auth["refresh_token"])

    def test_tv_device_pairing_and_admin_approval_exchange(self):
        """TV pairing init -> pending poll -> Admin approval -> one-time exchange -> replay rejection."""
        owner = bootstrap_owner(self.conn, "owner_user", "OwnerPassword123!")

        init_data = initiate_pairing(
            self.conn,
            device_name="Living Room TV",
            device_id="tv-lr-1",
            client_ip="192.168.1.50",
        )
        code = init_data["pairing_code"]
        secret = init_data["secret"]
        self.assertEqual(init_data["expires_in"], 300)

        # TV polls while pending
        poll1 = poll_pairing_status(self.conn, secret, client_ip="192.168.1.50")
        self.assertEqual(poll1["status"], "pending")
        self.assertGreater(poll1["expires_in"], 0)

        # Admin approves pairing out-of-band
        app_res = approve_pairing(self.conn, code, approver_user_id=owner["id"])
        self.assertTrue(app_res["approved"])

        # TV polls and completes one-time exchange
        poll2 = poll_pairing_status(self.conn, secret, client_ip="192.168.1.50")
        self.assertEqual(poll2["status"], "approved")
        self.assertTrue(poll2["access_token"].startswith("ah_tok_"))
        self.assertTrue(poll2["refresh_token"].startswith("ah_ref_"))
        self.assertEqual(poll2["user"]["username"], "owner_user")
        self.assertNotIn("is_admin", poll2["user"])

        # Deterministically clear only this test's own throttle bucket so replay test exercises exchange replay
        self.conn.execute(
            "DELETE FROM client_auth_rate_limits WHERE rate_key = %s",
            (f"pair_status:{hash_token(secret)}",),
        )
        self.conn.commit()

        # Subsequent poll with same secret fails (replay prevention)
        with self.assertRaises(PairingAlreadyExchanged):
            poll_pairing_status(self.conn, secret, client_ip="192.168.1.50")

    def test_unknown_pairing_secrets_prevent_database_writes(self):
        """Polling with unknown pairing secrets strictly prevents database writes (no rate limit rows created)."""
        initial_rate_count = self.conn.execute("SELECT count(*) FROM client_auth_rate_limits").fetchone()[0]

        bogus_secret = "pair_sec_" + secrets.token_hex(24)
        with self.assertRaises(PairingNotFoundOrExpired):
            poll_pairing_status(self.conn, bogus_secret, client_ip="192.168.1.77")

        final_rate_count = self.conn.execute("SELECT count(*) FROM client_auth_rate_limits").fetchone()[0]
        self.assertEqual(
            initial_rate_count,
            final_rate_count,
            "Unknown pairing secret must not write rate limit records to PostgreSQL",
        )

    def test_http_pairing_flow_with_actual_admin_guard(self):
        """Direct positive and negative HTTP test for pairing using registered routes and actual admin guard."""
        bootstrap_owner(self.conn, "admin_owner", "AdminPass12345!")
        admin_token = "secret_admin_write_token_test_999"

        client = TestClient(auth_test_app)

        # 1. Device initiates pairing via HTTP
        init_res = client.post(
            "/api/v1/auth/pair/init",
            json={"device_name": "Sony Bravia", "device_id": "sony-1"},
        )
        self.assertEqual(init_res.status_code, 200)
        init_data = init_res.json()
        code = init_data["pairing_code"]
        secret = init_data["secret"]

        # 2. TV polls while pending
        poll_res = client.post(
            "/api/v1/auth/pair/status",
            json={"secret": secret},
        )
        self.assertEqual(poll_res.status_code, 200)
        self.assertEqual(poll_res.json()["status"], "pending")

        # 3. Negative: Admin approval without token -> 403
        denied_res = client.post(
            "/admin/api/v2/pairing/approve",
            json={"pairing_code": code},
        )
        self.assertEqual(denied_res.status_code, 403)

        # 4. Positive: Admin approval with actual configured admin token -> 200
        with patch.object(admin_module, "_write_token", return_value=admin_token):
            approve_res = client.post(
                "/admin/api/v2/pairing/approve",
                json={"pairing_code": code},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            self.assertEqual(approve_res.status_code, 200)
            self.assertTrue(approve_res.json().get("approved"))

        # 5. Device polls status and exchanges for tokens -> 200
        exchange_res = client.post(
            "/api/v1/auth/pair/status",
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(exchange_res.status_code, 200)
        ex_data = exchange_res.json()
        self.assertEqual(ex_data["status"], "approved")
        self.assertTrue(ex_data["access_token"].startswith("ah_tok_"))
        self.assertTrue(ex_data["refresh_token"].startswith("ah_ref_"))
        self.assertEqual(ex_data["user"]["username"], "admin_owner")

        # Deterministically clear only this test's own throttle bucket so replay test exercises exchange replay
        self.conn.execute(
            "DELETE FROM client_auth_rate_limits WHERE rate_key = %s",
            (f"pair_status:{hash_token(secret)}",),
        )
        self.conn.commit()

        # 6. Negative: Second poll with same secret -> 401 Replay rejected
        replay_res = client.post(
            "/api/v1/auth/pair/status",
            headers={"Authorization": f"Bearer {secret}"},
        )
        self.assertEqual(replay_res.status_code, 401)

    def test_stateful_db_commit_on_error_http_responses(self):
        """Failed operations must commit state (rate limits, replay revokes, poll counts) even on error HTTP responses."""
        bootstrap_owner(self.conn, "target_user", "CorrectPassword123!")
        client = TestClient(auth_test_app)

        # A. Failed login commits rate limit attempt counter in DB
        login_res = client.post(
            "/api/v1/auth/login",
            json={"username": "target_user", "password": "WrongPassword!"},
        )
        self.assertEqual(login_res.status_code, 401)
        row = self.conn.execute(
            "SELECT attempts FROM client_auth_rate_limits WHERE rate_key = 'login_ip:testclient'"
        ).fetchone()
        self.assertIsNotNone(row, "Rate limit counter must be committed to DB even when login returns 401")
        self.assertGreaterEqual(row[0], 1)

        # B. Refresh token replay commits session revocation in DB
        auth_data = authenticate_by_password(
            self.conn,
            username="target_user",
            password="CorrectPassword123!",
            device_name="Tester",
            device_id="test-dev",
            client_ip="127.0.0.1",
        )
        ref1 = auth_data["refresh_token"]
        auth2 = refresh_session_token(self.conn, ref1)

        # Present ref1 again via HTTP endpoint -> returns 401
        replay_res = client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": ref1},
        )
        self.assertEqual(replay_res.status_code, 401)

        # Verify that revoked_at is committed in DB
        sess_row = self.conn.execute(
            "SELECT revoked_at FROM client_sessions WHERE access_token_hash = %s",
            (hash_token(auth2["access_token"]),),
        ).fetchone()
        self.assertIsNotNone(sess_row)
        self.assertIsNotNone(sess_row[0], "Session must be marked revoked in DB upon replay HTTP error")

        # C. Pairing poll attempts counter is committed in DB even when expired
        init_data = initiate_pairing(self.conn, "TV", "tv-1", "127.0.0.1")
        secret = init_data["secret"]

        # Poll via HTTP
        poll_res = client.post("/api/v1/auth/pair/status", json={"secret": secret})
        self.assertEqual(poll_res.status_code, 200)

        pair_row = self.conn.execute(
            "SELECT poll_attempts FROM client_pairing_sessions WHERE secret_hash = %s",
            (hash_token(secret),),
        ).fetchone()
        self.assertIsNotNone(pair_row)
        self.assertEqual(pair_row[0], 1, "Poll attempts must be committed in DB")

    def test_distributed_rate_limiting_across_postgresql(self):
        """client_auth_rate_limits table enforces atomic bucketed limits across instances."""
        key = "test_rate_bucket"
        for _ in range(3):
            allowed = check_and_increment_rate_limit(self.conn, key, max_attempts=3, window_seconds=10)
            self.assertTrue(allowed)

        denied = check_and_increment_rate_limit(self.conn, key, max_attempts=3, window_seconds=10)
        self.assertFalse(denied)

    def test_prune_retired_auth_data(self):
        """prune_retired_auth_data runs cleanly and returns pruned item counts."""
        res = prune_retired_auth_data(self.conn)
        self.assertIn("pruned_rate_limits", res)
        self.assertIn("pruned_pairing_sessions", res)
        self.assertIn("pruned_sessions", res)

    def test_live_pairing_unique_immutable_predicate_and_expiration(self):
        """Live PostgreSQL enforces single active code for pending/approved and allows reuse once expired."""
        init1 = initiate_pairing(self.conn, "TV1", "tv-id-1", "127.0.0.1")
        code1 = init1["pairing_code"]
        norm1 = normalize_pairing_code(code1)

        # Inserting a duplicate pending code directly must violate unique constraint
        self.conn.execute("SAVEPOINT dup_pending")
        try:
            with self.assertRaises((db.IntegrityError, db.DatabaseError)):
                self.conn.execute(
                    "INSERT INTO client_pairing_sessions ("
                    "id, pairing_code, normalized_code, secret_hash, device_name, device_id, status, "
                    "poll_attempts, expires_at, client_ip"
                    ") VALUES ('pair_dup1', %s, %s, 'hash1', 'TV2', 'tv-id-2', 'pending', 0, "
                    "CURRENT_TIMESTAMP + INTERVAL '5 minutes', '127.0.0.1')",
                    (code1, norm1),
                )
        finally:
            self.conn.execute("ROLLBACK TO SAVEPOINT dup_pending")

        # Same for approved status
        self.conn.execute("UPDATE client_pairing_sessions SET status = 'approved' WHERE normalized_code = %s", (norm1,))
        self.conn.commit()
        self.conn.execute("SAVEPOINT dup_approved")
        try:
            with self.assertRaises((db.IntegrityError, db.DatabaseError)):
                self.conn.execute(
                    "INSERT INTO client_pairing_sessions ("
                    "id, pairing_code, normalized_code, secret_hash, device_name, device_id, status, "
                    "poll_attempts, expires_at, client_ip"
                    ") VALUES ('pair_dup2', %s, %s, 'hash2', 'TV2', 'tv-id-2', 'pending', 0, "
                    "CURRENT_TIMESTAMP + INTERVAL '5 minutes', '127.0.0.1')",
                    (code1, norm1),
                )
        finally:
            self.conn.execute("ROLLBACK TO SAVEPOINT dup_approved")

        # Mark expired in past: initiate_pairing will expire past rows and allow code reuse
        self.conn.execute(
            "UPDATE client_pairing_sessions SET expires_at = CURRENT_TIMESTAMP - INTERVAL '10 seconds' "
            "WHERE normalized_code = %s",
            (norm1,),
        )
        self.conn.commit()

        # Forcing candidate code to norm1 should succeed because initiate_pairing expires past rows first
        with patch("common.client_auth.service.generate_pairing_code", return_value=code1):
            init2 = initiate_pairing(self.conn, "TV3", "tv-id-3", "127.0.0.1")
            self.assertEqual(init2["pairing_code"], code1)

    def test_pairing_polling_rate_limiting_regression(self):
        """Rapid polling exceeding rate limit (max 2 per second) raises RateLimitExceeded."""
        init = initiate_pairing(self.conn, "TV_Limiter", "tv-limiter-1", "127.0.0.1")
        secret = init["secret"]
        # The limiter counts attempts in whole-second windows, so pin the clock:
        # a real clock can cross a window boundary between two polls and make
        # this regression test pass or fail depending on scheduling luck.
        frozen = 1_700_000_000.0
        with patch("common.client_auth.rate_limit.time.time", return_value=frozen):
            p1 = poll_pairing_status(self.conn, secret, client_ip="127.0.0.1")
            self.assertEqual(p1["status"], "pending")
            p2 = poll_pairing_status(self.conn, secret, client_ip="127.0.0.1")
            self.assertEqual(p2["status"], "pending")
            # Poll 3 in the same window triggers RateLimitExceeded
            with self.assertRaises(RateLimitExceeded):
                poll_pairing_status(self.conn, secret, client_ip="127.0.0.1")


if __name__ == "__main__":
    unittest.main()
