"""Deterministic tests for Milestone E: in-memory metrics, account status, channel access, and secrecy."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
RESOLVER_DIR = ROOT / "resolver"
if str(RESOLVER_DIR) not in sys.path:
    sys.path.insert(0, str(RESOLVER_DIR))

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import admin_v2_telegram_auth
from providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    _MEMORY_CHANNEL_ACCESS,
    list_channel_access,
    set_channel_access,
    set_memory_only_mode,
    upsert_account,
)
from providers.telegram_router import TelegramAccountRouter, get_telegram_router, reset_telegram_router


class MockAdmin:
    ADMIN_PUBLIC = True
    JSONResponse = JSONResponse

    def __init__(self, blocked: bool = False):
        self._blocked = blocked

    def _admin_blocked(self, request):
        return self._blocked

    def _write_token(self):
        return ""

    def _wake_worker(self, worker):
        return True


class TestMilestoneEMetrics(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _MEMORY_ACCOUNTS.clear()
        _MEMORY_CHANNEL_ACCESS.clear()
        set_memory_only_mode(True)
        self.router = TelegramAccountRouter()

    def tearDown(self):
        set_memory_only_mode(False)

    async def test_router_metrics_recording_and_retrieval(self):
        """Verify active_ops, bytes, errors, flood_wait, TTFB, and throughput are recorded accurately."""
        account_id = "test_acc"
        self.router._active_leases[account_id] = 2

        # Record transferred bytes
        self.router.record_bytes(account_id, 1048576)  # 1MB
        self.router.record_bytes(account_id, 2097152)  # 2MB
        # Total bytes = 3MB

        # Record TTFB buffer init latency
        self.router.record_buffer_init_latency(account_id, 120.0)
        self.router.record_buffer_init_latency(account_id, 80.0)
        # Average TTFB = 100.0 ms

        # Record an error and a flood wait
        self.router.record_error(account_id, "TimeoutError", "connection timed out")
        self.router.record_flood_wait(account_id, 15)

        metrics = self.router.get_account_metrics(account_id)
        self.assertEqual(metrics["active_ops"], 2)
        self.assertEqual(metrics["bytes_transferred"], 3145728)
        self.assertEqual(metrics["errors_count"], 1)
        self.assertEqual(metrics["flood_wait_count"], 1)
        self.assertEqual(metrics["buffer_init_latency_ms"], 100.0)
        self.assertGreater(metrics["cooldown_seconds"], 0)
        self.assertGreaterEqual(metrics["throughput_mb_s"], 0.0)
        self.assertIsNotNone(metrics["last_error"])
        self.assertEqual(metrics["last_error"]["type"], "TimeoutError")

    async def test_metrics_throughput_window_compaction(self):
        """Verify sliding window bounds memory by compacting older samples."""
        account_id = "acc_compact"
        # Append 120 samples
        for i in range(120):
            self.router.record_bytes(account_id, 1024)

        window = self.router._throughput_window[account_id]
        # Must be bounded to <= 100 entries
        self.assertLessEqual(len(window), 100)
        self.assertEqual(self.router._bytes_transferred[account_id], 120 * 1024)

    async def test_error_redaction_preserves_secrecy(self):
        """Verify error messages are scrubbed and capped to prevent token/path/phone leakage."""
        account_id = "acc_secret"
        sensitive_token = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
        sensitive_path = "/var/data/telegram.session"
        sensitive_phone = "+12025550199"
        sensitive_detail = (
            f"Failed with token={sensitive_token} standalone {sensitive_token} "
            f"for user phone={sensitive_phone} and session_path={sensitive_path}"
        )
        self.router.record_error(account_id, "AuthKeyError", sensitive_detail)

        metrics = self.router.get_account_metrics(account_id)
        last_err = metrics["last_error"]
        self.assertIsNotNone(last_err)
        # Max length bounded to 128 chars
        self.assertLessEqual(len(last_err["detail"]), 128)
        self.assertEqual(last_err["type"], "AuthKeyError")
        # Assert secret, path, and phone values are completely absent from detail
        self.assertNotIn(sensitive_token, last_err["detail"])
        self.assertNotIn(sensitive_path, last_err["detail"])
        self.assertNotIn(sensitive_phone, last_err["detail"])
        self.assertTrue("[redacted]" in last_err["detail"] or "[redacted-token]" in last_err["detail"])
        self.assertIn("[path redacted]", last_err["detail"])
        self.assertIn("[phone redacted]", last_err["detail"])

    async def test_error_redaction_relative_session_and_channel_id_preservation(self):
        """Direct test for Finding 5: redact relative .session references but preserve channel IDs like -1001234567890."""
        account_id = "acc_redact_test"
        raw_detail = "Failed fetching -1001234567890 via session account_worker.session with error"
        self.router.record_error(account_id, "ChannelPrivateError", raw_detail)

        metrics = self.router.get_account_metrics(account_id)
        detail = metrics["last_error"]["detail"]

        # Channel ID must be preserved and NOT redacted as phone
        self.assertIn("-1001234567890", detail)
        self.assertNotIn("[phone redacted]", detail)

        # Relative session reference must be redacted
        self.assertNotIn("account_worker.session", detail)
        self.assertIn("[session redacted]", detail)

    async def test_channel_less_flood_wait_persists_no_channel_access_record(self):
        """Direct test for Finding 2: handle_request_exception on empty channel_id persists no row, keeps cooldown and metrics."""
        account_id = "acc_general_flood"

        class SimulatedFloodWait(Exception):
            seconds = 45

        # Call handle_request_exception with empty channel_id
        self.router.handle_request_exception(account_id, "", SimulatedFloodWait("General flood wait"))

        # Account cooldown and error metrics must be recorded
        self.assertTrue(self.router.is_account_cooling_down(account_id))
        self.assertGreaterEqual(self.router.get_cooldown_remaining(account_id), 40)
        metrics = self.router.get_account_metrics(account_id)
        self.assertEqual(metrics["flood_wait_count"], 1)
        self.assertEqual(metrics["errors_count"], 1)

        # No channel access record must be persisted for empty channel or account
        records = list_channel_access(account_id=account_id)
        self.assertEqual(records, [])

    async def test_single_flood_wait_counted_once_with_cooldown(self):
        """Verify a single FloodWait increments error and flood_wait counts exactly once and sets cooldown."""
        import tempfile
        account_id = "acc_flood"
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            f.write(b"data")
            p = f.name

        try:
            upsert_account(account_id, label="Flood Acc", session_file=p, enabled=True)
            self.router._client_factory = lambda *args: AsyncMock()
            self.router._active_leases[account_id] = 0

            class SimulatedFloodWait(Exception):
                seconds = 25

            # Execute channel lease with FloodWait exception
            with self.assertRaises(SimulatedFloodWait):
                async with self.router.channel_lease("-100999", affinity_account=account_id):
                    raise SimulatedFloodWait("Flood wait triggered")

            metrics = self.router.get_account_metrics(account_id)
            # Single flood wait must increment errors_count and flood_wait_count exactly once
            self.assertEqual(metrics["errors_count"], 1)
            self.assertEqual(metrics["flood_wait_count"], 1)
            self.assertGreater(metrics["cooldown_seconds"], 0)
            self.assertTrue(self.router.is_account_cooling_down(account_id))
        finally:
            os.remove(p)

    async def test_download_stream_flood_wait_counted_exactly_once(self):
        """Verify that download_stream encountering FloodWait inside channel_lease counts error & flood exactly once."""
        import tempfile
        import providers.telegram as tg
        account_id = "acc_stream_flood"
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            f.write(b"data")
            p = f.name

        try:
            upsert_account(account_id, label="Stream Flood Acc", session_file=p, enabled=True)
            mock_client = AsyncMock()
            mock_client.is_connected = lambda: True

            class SimulatedFloodWait(Exception):
                seconds = 15

            router = get_telegram_router()
            router._client_factory = lambda *args: mock_client

            with patch("providers.telegram._get_cached_message", side_effect=SimulatedFloodWait("Rate limit")), \
                 patch("providers.telegram.is_channel_authorized", return_value=True), \
                 patch("providers.telegram._parse_ref", return_value=("-100123", 42, "unique1")):
                prov = tg.TelegramProvider()
                with self.assertRaises(SimulatedFloodWait):
                    async for _ in prov.download_range("-100123:42:unique1", 0, 1024, affinity_account=account_id):
                        pass

            metrics = router.get_account_metrics(account_id)
            self.assertEqual(metrics["errors_count"], 1)
            self.assertEqual(metrics["flood_wait_count"], 1)
            self.assertGreater(metrics["cooldown_seconds"], 0)
            self.assertTrue(router.is_account_cooling_down(account_id))
        finally:
            os.remove(p)


class TestMilestoneEApiAndSecrecy(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _MEMORY_ACCOUNTS.clear()
        _MEMORY_CHANNEL_ACCESS.clear()
        set_memory_only_mode(True)
        self.admin = MockAdmin(blocked=False)
        self.app = FastAPI()
        router = admin_v2_telegram_auth.build_router(self.admin)
        self.app.include_router(router)
        self.client = TestClient(self.app)

    async def asyncTearDown(self):
        set_memory_only_mode(False)
        await reset_telegram_router()

    def test_list_accounts_returns_metrics_and_channel_summary(self):
        """Verify GET /api/v2/telegram/accounts includes metrics and channel_summary."""
        upsert_account("acc1", label="First Account", enabled=True)
        set_channel_access("acc1", "-100111", "accessible")
        set_channel_access("acc1", "-100222", "no_access")

        router = get_telegram_router()
        router.record_bytes("acc1", 5242880)
        router.record_buffer_init_latency("acc1", 45.2)

        with patch("providers.telegram_router.get_telegram_router", return_value=router), \
             patch("admin_v2_telegram_auth.get_telegram_login_manager") as mock_mgr_getter:
            mock_mgr = AsyncMock()
            mock_mgr.get_status = AsyncMock(return_value={"status": "authorized", "authorized": True})
            mock_mgr_getter.return_value = mock_mgr

            res = self.client.get("/api/v2/telegram/accounts")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["ok"])
            self.assertEqual(len(data["accounts"]), 1)

            acc = data["accounts"][0]
            self.assertIn("metrics", acc)
            self.assertEqual(acc["metrics"]["bytes_transferred"], 5242880)
            self.assertEqual(acc["metrics"]["buffer_init_latency_ms"], 45.2)

            self.assertIn("channel_summary", acc)
            self.assertEqual(acc["channel_summary"]["accessible"], 1)
            self.assertEqual(acc["channel_summary"]["inaccessible"], 1)
            self.assertEqual(acc["channel_summary"]["total"], 2)

    def test_channels_access_endpoint_blocked_for_external(self):
        """Verify non-admin/blocked external requests receive 403 without data leakage."""
        blocked_admin = MockAdmin(blocked=True)
        blocked_app = FastAPI()
        blocked_router = admin_v2_telegram_auth.build_router(blocked_admin)
        blocked_app.include_router(blocked_router)
        client = TestClient(blocked_app)

        res = client.get("/api/v2/telegram/channels/access")
        self.assertEqual(res.status_code, 403)
        self.assertIn("error", res.json())
        # Does not leak channel IDs or account info
        self.assertNotIn("channels", res.json())


if __name__ == "__main__":
    unittest.main()
