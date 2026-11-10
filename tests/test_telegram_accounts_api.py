"""Tests for Admin v2 Telegram Multi-Account Management API endpoints."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]

from fastapi import FastAPI
from fastapi.testclient import TestClient

import resolver.admin_v2_telegram_auth as admin_v2_telegram_auth
from resolver.providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    _MEMORY_CHANNEL_ACCESS,
    TelegramAccountRecord,
    get_account,
    set_channel_access,
    set_memory_only_mode,
    upsert_account,
)


from fastapi.responses import JSONResponse


class MockAdmin:
    ADMIN_PUBLIC = True
    JSONResponse = JSONResponse

    def _admin_blocked(self, request):
        return False

    def _write_token(self):
        return ""

    def _wake_worker(self, worker):
        return True


class TestTelegramAccountsApi(unittest.TestCase):
    def setUp(self):
        _MEMORY_ACCOUNTS.clear()
        _MEMORY_CHANNEL_ACCESS.clear()
        set_memory_only_mode(True)
        self.admin = MockAdmin()
        self.app = FastAPI()
        router = admin_v2_telegram_auth.build_router(self.admin)
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.headers = {"Origin": "http://testserver"}

    def tearDown(self):
        set_memory_only_mode(False)

    def test_list_accounts_empty_and_populated(self):
        res = self.client.get("/api/v2/telegram/accounts")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIsInstance(data["accounts"], list)

        # Add account
        upsert_account("acc1", label="First Account", phone_mask="+48 123 *** 789", enabled=True)
        set_channel_access("acc1", "-100123", "accessible")

        res2 = self.client.get("/api/v2/telegram/accounts")
        self.assertEqual(res2.status_code, 200)
        accounts = res2.json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["account_id"], "acc1")
        self.assertEqual(accounts[0]["label"], "First Account")
        self.assertEqual(accounts[0]["phone_mask"], "+48 123 *** 789")
        self.assertEqual(len(accounts[0]["channels"]), 1)
        self.assertEqual(accounts[0]["channels"][0]["channel_id"], "-100123")

    def test_add_account_success_and_validation(self):
        # Invalid account ID
        res_inv = self.client.post(
            "/api/v2/telegram/accounts",
            json={"account_id": "invalid/path"},
            headers=self.headers,
        )
        self.assertEqual(res_inv.status_code, 400)

        # Success
        res_ok = self.client.post(
            "/api/v2/telegram/accounts",
            json={"account_id": "worker_backup", "label": "Backup Bot"},
            headers=self.headers,
        )
        self.assertEqual(res_ok.status_code, 201)
        acc = res_ok.json()["account"]
        self.assertEqual(acc["account_id"], "worker_backup")
        self.assertEqual(acc["label"], "Backup Bot")

        # Duplicate rejected
        res_dup = self.client.post(
            "/api/v2/telegram/accounts",
            json={"account_id": "worker_backup"},
            headers=self.headers,
        )
        self.assertEqual(res_dup.status_code, 409)

    def test_update_account(self):
        upsert_account("acc_edit", label="Old Label", enabled=True)

        res = self.client.patch(
            "/api/v2/telegram/accounts/acc_edit",
            json={"label": "New Label", "enabled": False},
            headers=self.headers,
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()["account"]
        self.assertEqual(data["label"], "New Label")
        self.assertFalse(data["enabled"])
        self.assertNotIn("session_file", data)

        stored = get_account("acc_edit")
        self.assertEqual(stored.label, "New Label")
        self.assertFalse(stored.enabled)

    def test_update_account_invalid_types_rejected(self):
        upsert_account("acc_type_check", label="Initial", enabled=True)

        # String 'false' instead of boolean -> 400 Bad Request
        res_str_bool = self.client.patch(
            "/api/v2/telegram/accounts/acc_type_check",
            json={"enabled": "false"},
            headers=self.headers,
        )
        self.assertEqual(res_str_bool.status_code, 400)
        self.assertEqual(res_str_bool.json()["code"], "invalid_enabled")

        # Non-string label -> 400 Bad Request
        res_bad_label = self.client.patch(
            "/api/v2/telegram/accounts/acc_type_check",
            json={"label": 12345},
            headers=self.headers,
        )
        self.assertEqual(res_bad_label.status_code, 400)
        self.assertEqual(res_bad_label.json()["code"], "invalid_label")

    def test_admin_blocked_read_endpoints(self):
        upsert_account("acc_gate", label="Gate Check", enabled=True)
        set_channel_access("acc_gate", "-100123", "accessible")

        with patch.object(self.admin, "_admin_blocked", return_value=True):
            res_accounts = self.client.get("/api/v2/telegram/accounts")
            self.assertEqual(res_accounts.status_code, 403)
            self.assertIn("Admin is only available locally", res_accounts.text)

            res_channels = self.client.get("/api/v2/telegram/channels/access")
            self.assertEqual(res_channels.status_code, 403)
            self.assertIn("Admin is only available locally", res_channels.text)

    def test_api_responses_omit_session_file(self):
        upsert_account("acc_sec", label="Sec Check", session_file="/secret/path/acc_sec.session", enabled=True)

        res_list = self.client.get("/api/v2/telegram/accounts")
        self.assertEqual(res_list.status_code, 200)
        for acc in res_list.json()["accounts"]:
            self.assertNotIn("session_file", acc)

        res_add = self.client.post(
            "/api/v2/telegram/accounts",
            json={"account_id": "acc_sec_2", "label": "Sec 2"},
            headers=self.headers,
        )
        self.assertEqual(res_add.status_code, 201)
        self.assertNotIn("session_file", res_add.json()["account"])

        res_patch = self.client.patch(
            "/api/v2/telegram/accounts/acc_sec_2",
            json={"label": "Sec 2 Updated"},
            headers=self.headers,
        )
        self.assertEqual(res_patch.status_code, 200)
        self.assertNotIn("session_file", res_patch.json()["account"])

    def test_delete_account_requires_explicit_confirmation(self):
        upsert_account("acc_del", label="To Delete", enabled=True)

        # Without confirmation -> 400
        res_no_conf = self.client.delete(
            "/api/v2/telegram/accounts/acc_del",
            headers=self.headers,
        )
        self.assertEqual(res_no_conf.status_code, 400)
        self.assertEqual(res_no_conf.json()["code"], "confirmation_required")
        self.assertIsNotNone(get_account("acc_del"))

        # With confirmation -> 200
        res_conf = self.client.delete(
            "/api/v2/telegram/accounts/acc_del?confirm=true",
            headers=self.headers,
        )
        self.assertEqual(res_conf.status_code, 200)
        self.assertTrue(res_conf.json()["deleted"])
        self.assertIsNone(get_account("acc_del"))

    def test_delete_missing_account_reports_404(self):
        # Account does not exist -> 404 Not Found
        res = self.client.delete(
            "/api/v2/telegram/accounts/non_existent_account?confirm=true",
            headers=self.headers,
        )
        self.assertEqual(res.status_code, 404)
        data = res.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["code"], "not_found")

    def test_delete_account_db_failure_reports_500_and_does_not_unlink(self):
        set_memory_only_mode(False)
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            session_file = f.name
            f.write(b"session_bytes")

        try:
            _MEMORY_ACCOUNTS["acc_db_fail_api"] = TelegramAccountRecord(
                account_id="acc_db_fail_api",
                session_file=session_file,
            )

            with patch("resolver.providers.telegram_account_store._is_db_available", return_value=False):
                res = self.client.delete(
                    "/api/v2/telegram/accounts/acc_db_fail_api?confirm=true",
                    headers=self.headers,
                )
                self.assertEqual(res.status_code, 500)
                data = res.json()
                self.assertFalse(data["ok"])
                self.assertEqual(data["code"], "delete_failed")

            # Session file MUST NOT be unlinked
            self.assertTrue(os.path.isfile(session_file))
        finally:
            if os.path.isfile(session_file):
                os.remove(session_file)

    def test_test_account_endpoint(self):
        upsert_account("acc_test", label="Test Account", enabled=True)

        with patch.object(
            admin_v2_telegram_auth.get_telegram_login_manager(),
            "get_status",
            new_callable=AsyncMock,
        ) as mock_status:
            mock_status.return_value = {"ok": True, "authorized": True, "status": "authorized"}

            with patch("resolver.providers.telegram_router.get_telegram_router") as mock_router_getter:
                mock_router = AsyncMock()
                mock_router.verify_channel_access.return_value = "accessible"
                mock_router_getter.return_value = mock_router

                with patch("resolver.providers.telegram_config._get_channels", return_value=["-100999"]):
                    res = self.client.post(
                        "/api/v2/telegram/accounts/acc_test/test",
                        headers=self.headers,
                    )
                    self.assertEqual(res.status_code, 200)
                    data = res.json()
                    self.assertTrue(data["ok"])
                    self.assertTrue(data["authorized"])
                    self.assertEqual(data["channels"], {"-100999": "accessible"})

    def test_channels_access_endpoint(self):
        upsert_account("acc_ch", enabled=True)
        set_channel_access("acc_ch", "-100111", "accessible")
        set_channel_access("acc_ch", "-100222", "inaccessible", error_detail="ChatAdminRequired")

        res = self.client.get("/api/v2/telegram/channels/access")
        self.assertEqual(res.status_code, 200)
        channels = res.json()["channels"]
        self.assertEqual(len(channels), 2)
        statuses = {c["channel_id"]: c["access_status"] for c in channels}
        self.assertEqual(statuses["-100111"], "accessible")
        self.assertEqual(statuses["-100222"], "inaccessible")


if __name__ == "__main__":
    unittest.main()
