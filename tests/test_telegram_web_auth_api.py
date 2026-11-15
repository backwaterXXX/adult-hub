"""Automated test suite for Adult Hub Telegram web login HTTP API endpoints,
CSRF/token enforcement, sources list integration, and sensitive parameter redaction.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]

import resolver.admin_v2_telegram_auth as admin_v2_telegram_auth
from resolver.providers.telegram_login import (
    ActiveLoginChallenge,
    TelegramLoginManager,
)


class TestTelegramWebAuthApi(unittest.TestCase):
    """Test REST API routes, Origin/CSRF validation, and Bearer token enforcement."""

    def setUp(self):
        self.app = FastAPI()
        from starlette.responses import JSONResponse

        self.mock_admin = SimpleNamespace(
            _admin_blocked=lambda req: False,
            _write_token=lambda: "",
            ADMIN_PUBLIC=False,
            JSONResponse=JSONResponse,
            asyncio=asyncio,
        )
        router = admin_v2_telegram_auth.build_router(self.mock_admin)
        self.app.include_router(router)
        self.client = TestClient(self.app, base_url="http://testserver")

    def test_get_status_endpoint(self):
        with patch.object(
            admin_v2_telegram_auth.get_telegram_login_manager(),
            "get_status",
            new_callable=AsyncMock,
        ) as mock_status:
            mock_status.return_value = {"ok": True, "status": "ready", "authorized": False}
            res = self.client.get("/api/v2/telegram/auth/status")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"ok": True, "status": "ready", "authorized": False})

    def test_internal_status_endpoint_allowed_when_admin_blocked(self):
        self.mock_admin._admin_blocked = lambda req: True
        with patch.object(
            admin_v2_telegram_auth.get_telegram_login_manager(),
            "get_status",
            new_callable=AsyncMock,
        ) as mock_status:
            mock_status.return_value = {"ok": True, "status": "authorized", "authorized": True}
            res = self.client.get("/internal/telegram/auth/status")
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["status"], "authorized")

    def test_wake_worker_status_returned_in_submit_code(self):
        headers = {"Origin": "http://testserver"}
        self.mock_admin._wake_worker = MagicMock()
        with patch.object(
            admin_v2_telegram_auth.get_telegram_login_manager(),
            "submit_code",
            new_callable=AsyncMock,
        ) as mock_code:
            mock_code.return_value = {"ok": True, "state": "authorized"}
            res = self.client.post(
                "/api/v2/telegram/auth/code",
                json={"code": "12345"},
                headers=headers,
            )
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json().get("sync_notified"))
            self.mock_admin._wake_worker.assert_called_once_with("sync")

    def test_csrf_origin_enforcement_on_start(self):
        # Missing Origin header -> 403
        res = self.client.post("/api/v2/telegram/auth/start", json={"phone": "+48123456789"})
        self.assertEqual(res.status_code, 403)
        self.assertIn("Origin required", res.json()["error"])

        # Mismatched Origin header -> 403
        res = self.client.post(
            "/api/v2/telegram/auth/start",
            json={"phone": "+48123456789"},
            headers={"Origin": "http://evil-attacker.com"},
        )
        self.assertEqual(res.status_code, 403)

        # Matching Origin header -> allowed through CSRF check
        with patch.object(
            admin_v2_telegram_auth.get_telegram_login_manager(),
            "start_login",
            new_callable=AsyncMock,
        ) as mock_start:
            mock_start.return_value = {
                "ok": True,
                "state": "code_requested",
                "phone_mask": "+48 *** *** 789",
                "expires_in_seconds": 600,
            }
            res = self.client.post(
                "/api/v2/telegram/auth/start",
                json={"phone": "+48123456789"},
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["phone_mask"], "+48 *** *** 789")

    def test_bearer_write_token_enforcement(self):
        # When ADMIN_WRITE_TOKEN is configured:
        self.mock_admin._write_token = lambda: "configured_secret_token"

        headers_no_token = {"Origin": "http://testserver"}
        res = self.client.post(
            "/api/v2/telegram/auth/code",
            json={"code": "12345"},
            headers=headers_no_token,
        )
        self.assertEqual(res.status_code, 403)

        headers_bad_token = {"Origin": "http://testserver", "Authorization": "Bearer wrong_token"}
        res = self.client.post(
            "/api/v2/telegram/auth/code",
            json={"code": "12345"},
            headers=headers_bad_token,
        )
        self.assertEqual(res.status_code, 403)

        headers_valid = {
            "Origin": "http://testserver",
            "Authorization": "Bearer configured_secret_token",
        }
        with patch.object(
            admin_v2_telegram_auth.get_telegram_login_manager(),
            "submit_code",
            new_callable=AsyncMock,
        ) as mock_code:
            mock_code.return_value = {"ok": True, "state": "authorized"}
            res = self.client.post(
                "/api/v2/telegram/auth/code",
                json={"code": "12345"},
                headers=headers_valid,
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["state"], "authorized")

    def test_sensitive_parameters_redacted_from_responses(self):
        headers = {"Origin": "http://testserver"}
        with patch.object(
            admin_v2_telegram_auth.get_telegram_login_manager(),
            "submit_password",
            new_callable=AsyncMock,
        ) as mock_pwd:
            mock_pwd.return_value = {"ok": True, "state": "authorized"}
            res = self.client.post(
                "/api/v2/telegram/auth/password",
                json={"password": "super_secret_password_123"},
                headers=headers,
            )
            self.assertEqual(res.status_code, 200)
            # Response must NEVER reflect back raw password
            self.assertNotIn("super_secret_password_123", res.text)

    def test_invalid_whitespace_token_fails_closed(self):
        self.mock_admin._write_token = lambda: None  # Invalid token
        headers = {"Origin": "http://testserver"}
        res = self.client.post(
            "/api/v2/telegram/auth/start",
            json={"phone": "+48123456789"},
            headers=headers,
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Admin write token configuration is invalid", res.json()["error"])

    def test_resend_code_endpoint(self):
        headers = {"Origin": "http://testserver"}
        with patch.object(
            admin_v2_telegram_auth.get_telegram_login_manager(),
            "resend_code",
            new_callable=AsyncMock,
        ) as mock_resend:
            mock_resend.return_value = {"ok": True, "expires_in_seconds": 600}
            res = self.client.post("/api/v2/telegram/auth/resend", json={}, headers=headers)
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json()["expires_in_seconds"], 600)

    def test_cancel_login_endpoint(self):
        headers = {"Origin": "http://testserver"}
        with patch.object(
            admin_v2_telegram_auth.get_telegram_login_manager(),
            "cancel_login",
            new_callable=AsyncMock,
        ) as mock_cancel:
            mock_cancel.return_value = {"ok": True, "message": "Cancelled"}
            res = self.client.post("/api/v2/telegram/auth/cancel", json={}, headers=headers)
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["ok"])

    def test_account_id_validation_and_default_boundary_in_endpoints(self):
        headers = {"Origin": "http://testserver"}

        # Traversal in GET query param -> 400 invalid_account_id
        res_get_trav = self.client.get("/api/v2/telegram/auth/status?account_id=../../traversal")
        self.assertEqual(res_get_trav.status_code, 400)
        self.assertEqual(res_get_trav.json()["code"], "invalid_account_id")

        # Valid non-default account is supported in multi-account
        res_get_other = self.client.get("/api/v2/telegram/auth/status?account_id=secondary")
        self.assertEqual(res_get_other.status_code, 200)
        self.assertEqual(res_get_other.json()["account_id"], "secondary")

        # Traversal in POST start -> 400 invalid_account_id
        res_post_trav = self.client.post(
            "/api/v2/telegram/auth/start",
            json={"phone": "+48123456789", "account_id": "../traversal"},
            headers=headers,
        )
        self.assertEqual(res_post_trav.status_code, 400)
        self.assertEqual(res_post_trav.json()["code"], "invalid_account_id")

        # Valid non-default account in POST start is accepted
        with patch.object(
            admin_v2_telegram_auth.get_telegram_login_manager(),
            "start_login",
            new_callable=AsyncMock,
        ) as mock_start:
            mock_start.return_value = {"ok": True, "state": "code_requested", "phone_mask": "+48 123 *** 789"}
            res_post_other = self.client.post(
                "/api/v2/telegram/auth/start",
                json={"phone": "+48123456789", "account_id": "other_account"},
                headers=headers,
            )
            self.assertEqual(res_post_other.status_code, 200)
            self.assertTrue(res_post_other.json()["ok"])

    def test_invalid_phone_in_start_endpoint_returns_400(self):
        headers = {"Origin": "http://testserver"}
        res = self.client.post(
            "/api/v2/telegram/auth/start",
            json={"phone": "invalid_phone_number"},
            headers=headers,
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "invalid_phone")

    def test_submit_password_endpoint_preserves_spaces(self):
        headers = {"Origin": "http://testserver"}
        with patch.object(
            admin_v2_telegram_auth.get_telegram_login_manager(),
            "submit_password",
            new_callable=AsyncMock,
        ) as mock_pwd:
            mock_pwd.return_value = {"ok": True, "state": "authorized"}
            res = self.client.post(
                "/api/v2/telegram/auth/password",
                json={"password": "  spaces_around_password  "},
                headers=headers,
            )
            self.assertEqual(res.status_code, 200)
            mock_pwd.assert_awaited_once_with("  spaces_around_password  ", account_id="default")



class TestSourcesListIntegration(unittest.TestCase):
    """Test that /api/v2/sources/list includes telegram_auth information."""

    def test_sources_list_includes_telegram_auth(self):
        import resolver.admin_v2_sources as admin_v2_sources
        from starlette.responses import JSONResponse

        app = FastAPI()
        mock_admin = SimpleNamespace(
            _admin_blocked=lambda req: False,
            _write_token=lambda: "",
            ADMIN_PUBLIC=False,
            CONFIG_PATH="/config/config.yaml",
            JSONResponse=JSONResponse,
            asyncio=asyncio,
            admin_data=SimpleNamespace(
                configured_sources=lambda path: {
                    "available": True,
                    "sources": [],
                    "providers": {},
                }
            ),
        )
        router = admin_v2_sources.build_router(mock_admin)
        app.include_router(router)
        client = TestClient(app, base_url="http://testserver")

        with patch(
            "resolver.providers.telegram_login.get_telegram_login_manager",
        ) as mock_get_mgr:
            mock_mgr = MagicMock()
            mock_mgr.get_status = AsyncMock(return_value={
                "ok": True,
                "status": "ready",
                "authorized": False,
                "api_configured": True,
            })
            mock_get_mgr.return_value = mock_mgr
            res = client.get("/api/v2/sources/list")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertIn("telegram_auth", data)
            self.assertEqual(data["telegram_auth"]["status"], "ready")

    def test_sources_list_does_not_open_telegram_connection(self):
        import resolver.admin_v2_sources as admin_v2_sources
        from starlette.responses import JSONResponse

        app = FastAPI()
        mock_admin = SimpleNamespace(
            _admin_blocked=lambda req: False,
            _write_token=lambda: "",
            ADMIN_PUBLIC=False,
            CONFIG_PATH="/config/config.yaml",
            JSONResponse=JSONResponse,
            asyncio=asyncio,
            admin_data=SimpleNamespace(
                configured_sources=lambda path: {
                    "available": True,
                    "sources": [],
                    "providers": {},
                }
            ),
        )
        router = admin_v2_sources.build_router(mock_admin)
        app.include_router(router)
        client = TestClient(app, base_url="http://testserver")

        with patch("resolver.providers.telegram_login.is_session_authorized", new_callable=AsyncMock) as mock_mtproto:
            res = client.get("/api/v2/sources/list")
            self.assertEqual(res.status_code, 200)
            mock_mtproto.assert_not_called()



class TestTelegramLoginDbPersistenceFailure(unittest.IsolatedAsyncioTestCase):
    async def test_authorization_db_failure_does_not_report_success_or_leak_session(self):
        """Finding 2: Transient DB failure during login authorization persistence must not

        silently report login success or leave promoted session files behind.
        """
        mgr = TelegramLoginManager()

        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = os.path.join(tmpdir, "telegram.test_persist.session")
            pending_path = os.path.join(tmpdir, ".telegram.test_persist.pending.session")
            with open(pending_path, "wb") as f:
                f.write(b"pending_sqlite_session_data")

            mock_client = AsyncMock()
            mock_client.sign_in = AsyncMock()
            mock_client.is_user_authorized = AsyncMock(return_value=True)
            mock_client.is_connected = MagicMock(return_value=False)
            mock_client.disconnect = AsyncMock()

            challenge = ActiveLoginChallenge(
                account_id="test_persist",
                phone="+48123456789",
                phone_code_hash="hash123",
                client=mock_client,
                pending_path=pending_path,
                target_path=target_path,
            )
            mgr._challenges["test_persist"] = challenge

            # Simulate transient DB failure when persisting account to store
            with patch("resolver.providers.telegram_account_store.upsert_account",
                       side_effect=RuntimeError("Database connection lost")):
                res = await mgr.submit_code("12345", account_id="test_persist")

                # Must NOT report login success!
                self.assertFalse(res.get("ok", False))
                self.assertNotEqual(res.get("state"), "authorized")
                self.assertEqual(res.get("code"), "verification_error")

                # Must NOT leave authorized target session file on disk!
                self.assertFalse(os.path.isfile(target_path))

                # Must NOT be in cached auth!
                self.assertNotIn("test_persist", mgr._cached_auth)



if __name__ == "__main__":
    unittest.main()
