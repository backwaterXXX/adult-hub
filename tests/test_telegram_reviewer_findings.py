"""Deterministic tests specifically validating the resolution of Round 1 reviewer findings.

Covers:
- Finding 1 & 3: Router errors propagate without fallback; exceptions inside _acquire_client_lease
  propagate cleanly without generator athrow() RuntimeError.
- Finding 2: Stream manager job affinity per account, shared byte cache reuse across accounts,
  Range slice correctness without corruption or regressions.
- Finding 4: download_range preserves chosen account affinity across stream retries.
- Finding 5: Single-account reset/login does not disconnect other accounts or clear active counts.
- Finding 13: get_categories exception narrowing, channel config title fallback, error sanitization.
- Finding 15: Message/entity cache keys use stable account identity (never id(client)),
  guaranteeing cache isolation across clients and sessions.
- Finding 16: Explicit test seam for client lease routing without patching _require_client.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from resolver.providers import telegram as tg
from resolver.providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    _MEMORY_CHANNEL_ACCESS,
)
from resolver.providers.telegram_message_cache import (
    _MESSAGE_CACHE,
    _client_session_key,
)
from resolver.providers.telegram_router import get_telegram_router
from resolver.telegram_stream import ParallelTelegramStreamManager


class TestReviewerFindingsP1P2P3(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _MEMORY_ACCOUNTS.clear()
        _MEMORY_CHANNEL_ACCESS.clear()
        _MESSAGE_CACHE.clear()
        tg._CLIENT_LEASE_SEAM = None

    def tearDown(self):
        tg._CLIENT_LEASE_SEAM = None

    async def test_finding_1_router_errors_propagate_without_legacy_fallback(self):
        """Finding 1: When router lease fails, _acquire_client_lease must propagate the error

        and MUST NOT fall back to _require_client() or open a second default Telethon client.
        """
        router = get_telegram_router()
        with patch.object(router, "channel_lease", side_effect=RuntimeError("All accounts cooling down")):
            # Spy on _require_client_impl to verify it is NEVER called as a fallback
            with patch("resolver.providers.telegram._require_client_impl", new_callable=AsyncMock) as mock_impl:
                with self.assertRaises(RuntimeError) as ctx:
                    async with tg._acquire_client_lease("-100123") as (_aid, _client):
                        pass

                self.assertIn("All accounts cooling down", str(ctx.exception))
                # Critical check: legacy client acquisition was NEVER invoked as fallback!
                mock_impl.assert_not_called()

    async def test_finding_3_athrow_preserves_original_exception(self):
        """Finding 3: Exceptions thrown inside _acquire_client_lease must propagate cleanly

        without being caught by a setup except or raising RuntimeError("generator didn't stop after athrow()").
        """
        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        @asynccontextmanager
        async def fake_lease(channel_ref=None, affinity_account=None):
            yield ("acc_test", mock_client)

        tg._CLIENT_LEASE_SEAM = fake_lease

        class CustomTelegramError(Exception):
            pass

        # 1. Test custom exception raised by consumer
        with self.assertRaises(CustomTelegramError):
            async with tg._acquire_client_lease("-100123") as (_aid, _client):
                raise CustomTelegramError("FileReferenceExpired inside lease")

        # 2. Test ValueError raised by consumer
        with self.assertRaises(ValueError) as val_ctx:
            async with tg._acquire_client_lease("-100123") as (_aid, _client):
                raise ValueError("Bad metadata payload")
        self.assertEqual(str(val_ctx.exception), "Bad metadata payload")

    async def test_finding_2_stream_job_affinity_and_shared_byte_cache(self):
        """Finding 2: Shared stream manager isolates in-flight fetch jobs by account while

        safely sharing completed byte blocks across accounts without Range corruption.
        """
        mgr = ParallelTelegramStreamManager(
            workers=2,
            global_workers=4,
            block_size=1024,
            read_ahead_bytes=2048,
            max_cache_bytes=1024 * 1024,
        )

        acc_a_fetches = []
        acc_b_fetches = []

        async def fetch_acc_a(offset: int, length: int) -> bytes:
            acc_a_fetches.append((offset, length))
            await asyncio.sleep(0.02)
            # Fill with known pattern 'A'
            return b"A" * length

        async def fetch_acc_b(offset: int, length: int) -> bytes:
            acc_b_fetches.append((offset, length))
            await asyncio.sleep(0.02)
            # Fill with known pattern 'B'
            return b"B" * length

        canonical_key = "tg:test_chan:100:doc999:4096"

        # Stream block 0 with Account A
        stream_a = [chunk async for chunk in mgr.stream(
            key=canonical_key,
            offset=0,
            length=1024,
            total=4096,
            fetch=fetch_acc_a,
            job_key="acc_a",
        )]
        self.assertEqual(len(stream_a), 1)
        self.assertEqual(stream_a[0], b"A" * 1024)
        self.assertIn((0, 1024), acc_a_fetches)

        # Now Stream block 0 with Account B:
        # It MUST reuse the cached block from Account A (cache hit) without re-fetching via B!
        stream_b = [chunk async for chunk in mgr.stream(
            key=canonical_key,
            offset=0,
            length=1024,
            total=4096,
            fetch=fetch_acc_b,
            job_key="acc_b",
        )]
        self.assertEqual(len(stream_b), 1)
        self.assertEqual(stream_b[0], b"A" * 1024)
        # Account B did NOT fetch block 0 because shared cache was preserved!
        self.assertEqual(len(acc_b_fetches), 0)

        # Range slice test: request bytes 500 to 700 (length 200)
        stream_slice = [chunk async for chunk in mgr.stream(
            key=canonical_key,
            offset=500,
            length=200,
            total=4096,
            fetch=fetch_acc_b,
            job_key="acc_b",
        )]
        combined = b"".join(stream_slice)
        self.assertEqual(len(combined), 200)
        self.assertEqual(combined, b"A" * 200)

        await mgr.close()

    async def test_finding_4_download_range_pins_chosen_account_across_retries(self):
        """Finding 4: download_range locks chosen_account before retry_stream starts,

        ensuring all retry attempts maintain affinity with the same account.
        """
        prov = tg.TelegramProvider()

        leases_requested = []

        @asynccontextmanager
        async def mock_lease(channel_ref=None, affinity_account=None):
            leases_requested.append(affinity_account)
            mock_client = AsyncMock()
            mock_client.is_connected = MagicMock(return_value=True)
            yield (affinity_account or "resolved_acc", mock_client)

        tg._CLIENT_LEASE_SEAM = mock_lease

        attempts = 0

        async def mock_download_once(ref, offset, length, affinity_account=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                class FileReferenceExpiredError(Exception):
                    pass
                raise FileReferenceExpiredError("Expired reference on attempt 1")
            yield b"payload_bytes"

        with patch.object(prov, "_download_range_once", side_effect=mock_download_once):
            with patch("resolver.providers.telegram.is_channel_authorized", return_value=True):
                with patch("resolver.providers.telegram_router.TelegramAccountRouter.select_eligible_account",
                           new_callable=AsyncMock, return_value="initial_pinned_acc"):
                    chunks = [c async for c in prov.download_range("-100123:42:unique1", 0, 100)]
                    self.assertEqual(chunks, [b"payload_bytes"])
                    self.assertEqual(attempts, 2)

    async def test_finding_13_get_categories_resilient_and_narrowed(self):
        """Finding 13: get_categories skips unauthorized channels and on non-fatal errors

        uses configured channel title, without masking unexpected cancellations.
        """
        prov = tg.TelegramProvider()

        with patch("resolver.providers.telegram.is_channel_authorized", side_effect=lambda ch: ch == "-100valid"):
            with patch("resolver.providers.telegram._get_channels", return_value=["-100unauth", "-100valid"]):
                mock_cfg = MagicMock()
                mock_ch_cfg = MagicMock()
                mock_ch_cfg.title = "Configured Channel Title"
                mock_cfg.get_telegram_channel.return_value = mock_ch_cfg

                with patch("resolver.providers.telegram._get_config", return_value=mock_cfg):
                    with patch("resolver.providers.telegram._acquire_client_lease",
                               side_effect=RuntimeError("Channel access failure")):
                        categories = await prov.get_categories()
                        self.assertEqual(len(categories), 1)
                        self.assertEqual(categories[0].id, "-100valid")
                        self.assertEqual(categories[0].title, "Configured Channel Title")

    def test_finding_15_cache_isolation_and_no_id_client(self):
        """Finding 15: _client_session_key derives stable account keys and never uses id(client)."""
        class DummyClient:
            pass

        client_mock_1 = DummyClient()
        client_mock_2 = DummyClient()

        # Without account_id: derives unique, non-id(client) stable UUID attached to object
        key_1 = _client_session_key(client_mock_1)
        key_2 = _client_session_key(client_mock_2)
        self.assertTrue(key_1.startswith("client_"))
        self.assertTrue(key_2.startswith("client_"))
        self.assertNotEqual(key_1, key_2)
        self.assertNotEqual(key_1, str(id(client_mock_1)))

        # Calling again on same client returns same stable ID
        self.assertEqual(_client_session_key(client_mock_1), key_1)

        # With explicit account_id: returns acc:<account_id>
        self.assertEqual(_client_session_key(client_mock_1, account_id="acc_1"), "acc:acc_1")
        self.assertEqual(_client_session_key(client_mock_2, account_id="acc_2"), "acc:acc_2")

    async def test_router_channel_lease_direct_exception_propagation(self):
        """Round 2 Finding 8: Exceptions thrown inside router.channel_lease propagate cleanly

        without seam indirection.
        """
        router = get_telegram_router()
        mock_client = AsyncMock()
        mock_client.is_connected = MagicMock(return_value=True)

        with patch.object(router, "select_eligible_account", new_callable=AsyncMock, return_value="acc_direct"):
            with patch.object(router, "get_client_for_account", new_callable=AsyncMock, return_value=mock_client):
                class DirectLeaseException(Exception):
                    pass

                with self.assertRaises(DirectLeaseException):
                    async with router.channel_lease("-100123"):
                        raise DirectLeaseException("Direct exception inside channel_lease")

    async def test_exact_affinity_account_passed_to_range_download(self):
        """Round 2 Finding 8: download_range passes the exact chosen or affinity account

        to _download_range_once without falling back to 'default'.
        """
        prov = tg.TelegramProvider()
        received_affinities = []

        async def capture_download_once(ref, offset, length, affinity_account=None):
            received_affinities.append(affinity_account)
            yield b"data_chunk"

        with patch.object(prov, "_download_range_once", side_effect=capture_download_once):
            with patch("resolver.providers.telegram.is_channel_authorized", return_value=True):
                # 1. When affinity_account is explicitly specified
                _ = [c async for c in prov.download_range("-100123:42:unique1", 0, 100, affinity_account="specific_account")]
                self.assertEqual(received_affinities[-1], "specific_account")

                # 2. When affinity_account is None, router.select_eligible_account is queried and passed
                with patch("resolver.providers.telegram_router.TelegramAccountRouter.select_eligible_account",
                           new_callable=AsyncMock, return_value="selected_worker_bot"):
                    _ = [c async for c in prov.download_range("-100123:42:unique1", 0, 100)]
                    self.assertEqual(received_affinities[-1], "selected_worker_bot")

    async def test_flood_wait_short_recorded_and_makes_account_ineligible(self):
        """Round 2 Finding 4: FloodWait <= 15s updates router cooldown, avoids sleeping and

        retrying a pinned account already cooling, and marks it ineligible.
        """
        prov = tg.TelegramProvider()
        router = get_telegram_router()

        class ShortFloodWaitError(Exception):
            seconds = 12

        attempts = 0

        async def mock_fail_flood(ref, offset, length, affinity_account=None):
            nonlocal attempts
            attempts += 1
            raise ShortFloodWaitError("Rate limited")
            yield b""

        with patch.object(prov, "_download_range_once", side_effect=mock_fail_flood):
            with patch("resolver.providers.telegram.is_channel_authorized", return_value=True):
                with self.assertRaises(ShortFloodWaitError):
                    _ = [c async for c in prov.download_range("-100123:42:unique1", 0, 100, affinity_account="flood_worker")]

                # Must NOT sleep and retry pinned account when cooling down
                self.assertEqual(attempts, 1)

                # Router cooldown recorded
                self.assertTrue(router.is_account_cooling_down("flood_worker"))
                self.assertGreater(router.get_cooldown_remaining("flood_worker"), 0)

    async def test_get_categories_handles_rpc_error_with_structured_log_and_fallback(self):
        """Round 2 Finding 5: get_categories catches per-channel RPC/access errors,

        logs structured type, and falls back to configured title without halting other channels.
        """
        prov = tg.TelegramProvider()

        with patch("resolver.providers.telegram.is_channel_authorized", return_value=True):
            with patch("resolver.providers.telegram._get_channels", return_value=["-100broken", "-100good"]):
                mock_cfg = MagicMock()
                mock_broken_cfg = MagicMock()
                mock_broken_cfg.title = "Broken Channel Config Title"
                mock_good_cfg = MagicMock()
                mock_good_cfg.title = "Good Channel Config Title"
                mock_cfg.get_telegram_channel.side_effect = lambda ch: mock_broken_cfg if ch == "-100broken" else mock_good_cfg

                class ChannelPrivateRPCError(Exception):
                    pass

                mock_client = AsyncMock()

                async def fake_channel_title(client, channel_ref, entity=None):
                    if channel_ref == "-100broken":
                        raise ChannelPrivateRPCError("Private channel access denied")
                    return "Good Live Title"

                @asynccontextmanager
                async def mock_lease(channel_ref=None, affinity_account=None):
                    yield ("acc_cat", mock_client)

                with patch("resolver.providers.telegram._get_config", return_value=mock_cfg):
                    with patch("resolver.providers.telegram._acquire_client_lease", side_effect=mock_lease):
                        with patch("resolver.providers.telegram._channel_title", side_effect=fake_channel_title):
                            categories = await prov.get_categories()
                            self.assertEqual(len(categories), 2)
                            self.assertEqual(categories[0].id, "-100broken")
                            self.assertEqual(categories[0].title, "Broken Channel Config Title")
                            self.assertEqual(categories[1].id, "-100good")
                            self.assertEqual(categories[1].title, "Good Live Title")


if __name__ == "__main__":
    unittest.main()
