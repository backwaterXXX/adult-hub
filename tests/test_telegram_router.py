"""Focused deterministic unit tests for TelegramAccountRouter."""

import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from resolver.providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    _MEMORY_CHANNEL_ACCESS,
    set_channel_access,
    set_memory_only_mode,
    upsert_account,
)
from resolver.providers.telegram_router import TelegramAccountRouter


async def _all_accounts_authorized(account_ids):
    """Fixture session files are not real Telegram sessions, so treat every listed account as authorized.

    The authorization filter itself is covered by tests/test_telegram_authorization.py.
    """
    return list(account_ids)


class TestTelegramAccountRouter(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _MEMORY_ACCOUNTS.clear()
        _MEMORY_CHANNEL_ACCESS.clear()
        set_memory_only_mode(True)
        self.router = TelegramAccountRouter()
        auth_patch = patch(
            "resolver.providers.telegram_router.authorized_account_ids", _all_accounts_authorized
        )
        auth_patch.start()
        self.addCleanup(auth_patch.stop)

    def tearDown(self):
        set_memory_only_mode(False)

    async def test_least_loaded_selection(self):
        # Setup 2 temporary session files
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f1, \
             tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f2:
            f1.write(b"data1")
            f2.write(b"data2")
            p1, p2 = f1.name, f2.name

        try:
            upsert_account("acc1", label="Account 1", session_file=p1, enabled=True)
            upsert_account("acc2", label="Account 2", session_file=p2, enabled=True)

            # Initially both have 0 leases
            selected = await self.router.select_eligible_account("-100123")
            self.assertIn(selected, {"acc1", "acc2"})

            # Simulate 1 active lease on acc1
            self.router._active_leases["acc1"] = 1
            self.router._active_leases["acc2"] = 0

            # Should pick acc2 (least loaded)
            selected = await self.router.select_eligible_account("-100123")
            self.assertEqual(selected, "acc2")

            # Simulate 2 active leases on acc2
            self.router._active_leases["acc2"] = 2
            # Should pick acc1 (1 < 2)
            selected = await self.router.select_eligible_account("-100123")
            self.assertEqual(selected, "acc1")
        finally:
            import os
            os.remove(p1)
            os.remove(p2)

    async def test_prioritizes_verified_accessible(self):
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f1, \
             tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f2:
            f1.write(b"data1")
            f2.write(b"data2")
            p1, p2 = f1.name, f2.name

        try:
            upsert_account("acc1", label="Account 1", session_file=p1, enabled=True)
            upsert_account("acc2", label="Account 2", session_file=p2, enabled=True)

            # acc2 is verified accessible, acc1 is unknown
            set_channel_access("acc2", "-100123", "accessible")

            selected = await self.router.select_eligible_account("-100123")
            self.assertEqual(selected, "acc2")
        finally:
            import os
            os.remove(p1)
            os.remove(p2)

    async def test_skips_cooling_down_and_inaccessible(self):
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f1, \
             tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f2:
            f1.write(b"data1")
            f2.write(b"data2")
            p1, p2 = f1.name, f2.name

        try:
            upsert_account("acc1", label="Account 1", session_file=p1, enabled=True)
            upsert_account("acc2", label="Account 2", session_file=p2, enabled=True)

            # acc1 is cooling down
            self.router.record_flood_wait("acc1", 100)
            self.assertTrue(self.router.is_account_cooling_down("acc1"))

            # acc2 should be selected
            selected = await self.router.select_eligible_account("-100123")
            self.assertEqual(selected, "acc2")

            # Now mark acc2 inaccessible
            set_channel_access("acc2", "-100123", "inaccessible")

            # Neither is available (one in cooldown, one inaccessible)
            with self.assertRaises(RuntimeError) as ctx:
                await self.router.select_eligible_account("-100123")
            msg = str(ctx.exception)
            self.assertTrue(
                "No eligible Telegram accounts available" in msg or "cooling down" in msg
            )
        finally:
            import os
            os.remove(p1)
            os.remove(p2)

    async def test_lease_affinity_and_release(self):
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            f.write(b"data")
            p = f.name

        try:
            upsert_account("acc1", session_file=p, enabled=True)
            mock_client = AsyncMock()
            mock_client.is_connected = MagicMock(return_value=True)
            mock_client.is_user_authorized = AsyncMock(return_value=True)
            self.router._client_factory = lambda path, aid, ahash: mock_client

            # Acquire lease with affinity
            account_id, client, release = await self.router.acquire_channel_lease("-100123", affinity_account="acc1")
            self.assertEqual(account_id, "acc1")
            self.assertEqual(self.router._active_leases["acc1"], 1)

            # Release lease
            release()
            await asyncio.sleep(0.01)  # wait for release task
            self.assertEqual(self.router._active_leases["acc1"], 0)
        finally:
            import os
            os.remove(p)

    async def test_affinity_account_validation_and_refusal(self):
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            f.write(b"data")
            p = f.name

        try:
            upsert_account("acc_dis", session_file=p, enabled=False)
            upsert_account("acc_cool", session_file=p, enabled=True)
            self.router.record_flood_wait("acc_cool", 60)
            upsert_account("acc_no_sess", session_file="/nonexistent/missing.session", enabled=True)

            upsert_account("acc_blocked", session_file=p, enabled=True)
            set_channel_access("acc_blocked", "-100777", "no_access")

            # 1. Disabled account refused
            with self.assertRaises(RuntimeError) as ctx1:
                await self.router.acquire_channel_lease("-100777", affinity_account="acc_dis")
            self.assertIn("disabled", str(ctx1.exception))

            # 2. Cooling down account refused
            with self.assertRaises(RuntimeError) as ctx2:
                await self.router.acquire_channel_lease("-100777", affinity_account="acc_cool")
            self.assertIn("FloodWait cooldown", str(ctx2.exception))

            # 3. Missing session file refused
            with self.assertRaises(RuntimeError) as ctx3:
                await self.router.acquire_channel_lease("-100777", affinity_account="acc_no_sess")
            self.assertIn("missing", str(ctx3.exception))

            # 4. Confirmed no-access channel refused
            with self.assertRaises(RuntimeError) as ctx4:
                await self.router.acquire_channel_lease("-100777", affinity_account="acc_blocked")
            self.assertIn("confirmed no access", str(ctx4.exception))
        finally:
            import os
            if os.path.isfile(p):
                os.remove(p)

    async def test_reset_single_account_does_not_affect_others(self):
        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f1, \
             tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f2, \
             tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f3:
            for f in (f1, f2, f3):
                f.write(b"session")
            p1, p2, p3 = f1.name, f2.name, f3.name

        try:
            upsert_account("acc_a", session_file=p1, enabled=True)
            upsert_account("acc_b", session_file=p2, enabled=True)
            upsert_account("acc_c", session_file=p3, enabled=True)

            mock_a = AsyncMock()
            mock_a.is_connected = MagicMock(return_value=True)
            mock_b = AsyncMock()
            mock_b.is_connected = MagicMock(return_value=True)
            mock_c = AsyncMock()
            mock_c.is_connected = MagicMock(return_value=True)

            client_map = {p1: mock_a, p2: mock_b, p3: mock_c}
            self.router._client_factory = lambda path, aid, ahash: client_map[path]

            # Connect all 3
            await self.router.get_client_for_account("acc_a")
            await self.router.get_client_for_account("acc_b")
            await self.router.get_client_for_account("acc_c")

            self.router._active_leases["acc_a"] = 3
            self.router._active_leases["acc_b"] = 5
            self.router._active_leases["acc_c"] = 2

            # Reset only Account C
            await self.router.reset_account_client("acc_c")

            # Account C client was disconnected and removed from router clients
            mock_c.disconnect.assert_awaited()
            self.assertNotIn("acc_c", self.router._clients)

            # Accounts A and B remain connected and active counts preserved!
            self.assertIn("acc_a", self.router._clients)
            self.assertIn("acc_b", self.router._clients)
            self.assertEqual(self.router._active_leases["acc_a"], 3)
            self.assertEqual(self.router._active_leases["acc_b"], 5)
            mock_a.disconnect.assert_not_called()
            mock_b.disconnect.assert_not_called()
        finally:
            import os
            for p in (p1, p2, p3):
                if os.path.isfile(p):
                    os.remove(p)

    async def test_bounded_recheck_policy_and_status_distinctions(self):
        from datetime import datetime, timedelta, timezone

        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f:
            f.write(b"session")
            p = f.name

        try:
            upsert_account("acc_recheck", session_file=p, enabled=True)

            # Record no_access checked 2 hours ago (> 3600s TTL)
            set_channel_access("acc_recheck", "-100555", "no_access")
            rec = _MEMORY_CHANNEL_ACCESS[("acc_recheck", "-100555")]
            rec.last_checked_at = datetime.now(timezone.utc) - timedelta(hours=2)

            # Because > 1h has elapsed, bounded recheck policy permits selection for revalidation
            selected = await self.router.select_eligible_account("-100555")
            self.assertEqual(selected, "acc_recheck")

            # If checked recently (< 1h), account is excluded
            rec.last_checked_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            with self.assertRaises(RuntimeError):
                await self.router.select_eligible_account("-100555")

            # Verify distinction among status types in verify_channel_access
            mock_client = AsyncMock()
            mock_client.is_connected = MagicMock(return_value=True)
            mock_client.is_user_authorized = AsyncMock(return_value=True)

            class ChatAdminRequiredError(Exception):
                pass

            mock_client.get_entity.side_effect = ChatAdminRequiredError("Chat admin required")
            self.router._client_factory = lambda path, aid, ahash: mock_client

            res_perm = await self.router.verify_channel_access("acc_recheck", "-100555")
            self.assertEqual(res_perm, "no_access")

            mock_client.get_entity.side_effect = ConnectionResetError("TCP reset")
            res_temp = await self.router.verify_channel_access("acc_recheck", "-100555")
            self.assertEqual(res_temp, "temporary_error")
        finally:
            import os
            if os.path.isfile(p):
                os.remove(p)

    def test_handle_request_exception_narrows_status_changes(self):
        """Round 2 Finding 6: Local validation errors and expired references must NOT

        change channel access status to temporary_error. Only FloodWait, permission,
        and transport/network failures update access state.
        """
        set_channel_access("acc_flt", "-100888", "accessible")

        # 1. ValueError (local validation) must NOT change status
        self.router.handle_request_exception("acc_flt", "-100888", ValueError("Invalid param"))
        self.assertEqual(_MEMORY_CHANNEL_ACCESS[("acc_flt", "-100888")].access_status, "accessible")

        # 2. FileReferenceExpiredError must NOT change status
        class FileReferenceExpiredError(Exception):
            pass

        self.router.handle_request_exception("acc_flt", "-100888", FileReferenceExpiredError("Expired"))
        self.assertEqual(_MEMORY_CHANNEL_ACCESS[("acc_flt", "-100888")].access_status, "accessible")

        # 3. Network error MUST mark temporary_error
        self.router.handle_request_exception("acc_flt", "-100888", ConnectionResetError("Peer reset"))
        self.assertEqual(_MEMORY_CHANNEL_ACCESS[("acc_flt", "-100888")].access_status, "temporary_error")

        # 4. Access error MUST mark no_access
        class ChannelPrivateError(Exception):
            pass

        self.router.handle_request_exception("acc_flt", "-100888", ChannelPrivateError("Forbidden"))
        self.assertEqual(_MEMORY_CHANNEL_ACCESS[("acc_flt", "-100888")].access_status, "no_access")


if __name__ == "__main__":
    unittest.main()
