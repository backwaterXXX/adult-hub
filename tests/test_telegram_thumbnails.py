"""Preview failures must not hold Jellyfin's metadata queue indefinitely."""
import asyncio
from contextlib import asynccontextmanager
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import TEST_URL

# PostgreSQL-only: resolver cache/diagnostics read DATABASE_URL (no SQLite
# files). init_test_db creates the stream_cache table.
if TEST_URL:
    os.environ["DATABASE_URL"] = TEST_URL
else:
    os.environ.setdefault("DATABASE_URL", "")

sys.path.insert(0, str(ROOT / "resolver"))
sys.path.insert(0, str(ROOT))

from providers import telegram as telegram


class TestTelegramThumbnails(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self._orig_channels = os.environ.get("TELEGRAM_CHANNELS")
        os.environ["TELEGRAM_CHANNELS"] = "-1001,@example_channel_contract"

    def tearDown(self):
        if self._orig_channels is not None:
            os.environ["TELEGRAM_CHANNELS"] = self._orig_channels
        else:
            os.environ.pop("TELEGRAM_CHANNELS", None)
        super().tearDown()

    async def test_downloads_only_thumbnail_and_reuses_bytes_for_concurrent_callers(self):
        size = SimpleNamespace(type="m")
        document = SimpleNamespace(thumbs=[size])
        message = SimpleNamespace(media=SimpleNamespace(document=document), photo=None)
        client = SimpleNamespace(download_media=AsyncMock(return_value=b"\xff\xd8preview"))
        provider = telegram.TelegramProvider()

        @asynccontextmanager
        async def fake_lease(channel_ref=None, affinity_account=None):
            yield ("default", client)

        with patch.object(telegram, "_CLIENT_LEASE_SEAM", fake_lease), \
             patch.object(telegram, "_get_cached_message", AsyncMock(return_value=message)):
            first, second = await asyncio.gather(
                provider.download_thumb("-1001:42:unique"),
                provider.download_thumb("-1001:42:unique"))
            cached = await provider.download_thumb("-1001:42:unique")
        self.assertEqual((first, second, cached), (b"\xff\xd8preview",) * 3)
        client.download_media.assert_awaited_once_with(document, thumb=size, file=bytes)

    async def test_timeout_covers_client_connection_and_suppresses_immediate_retries(self):
        cancelled = asyncio.Event()
        async def connect():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        provider = telegram.TelegramProvider()

        @asynccontextmanager
        async def fake_lease(channel_ref=None, affinity_account=None):
            await connect()
            yield ("default", SimpleNamespace())

        with patch.object(telegram, "THUMB_TIMEOUT_SECONDS", 0.01), \
             patch.object(telegram, "_CLIENT_LEASE_SEAM", fake_lease):
            with self.assertRaises(asyncio.TimeoutError):
                await provider.download_thumb("-1001:42:unique")
            self.assertTrue(cancelled.is_set())
            self.assertIsNone(await provider.download_thumb("-1001:42:unique"))
            self.assertFalse(provider._thumb_tasks)

    async def test_disconnected_waiter_does_not_cancel_other_preview_callers(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def fetch(ref):
            started.set()
            await release.wait()
            return b"preview"
        provider = telegram.TelegramProvider()
        with patch.object(provider, "_download_thumb_uncached", side_effect=fetch) as download:
            first = asyncio.create_task(provider.download_thumb("-1001:42:unique"))
            await started.wait()
            second = asyncio.create_task(provider.download_thumb("-1001:42:unique"))
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            release.set()
            self.assertEqual(await second, b"preview")
            download.assert_awaited_once()
            self.assertFalse(provider._thumb_tasks)

    async def test_black_image_is_cached_as_none_with_negative_ttl(self):
        provider = telegram.TelegramProvider()
        with patch.object(provider, "_download_thumb_uncached", AsyncMock(return_value=b"fake-bytes")), \
             patch("common.image_utils.is_black_image", return_value=True):
            data = await provider.download_thumb("-1001:42:unique")
            self.assertIsNone(data)
            self.assertIn("-1001:42:unique", provider._thumb_cache)
            cached_exp, cached_data = provider._thumb_cache["-1001:42:unique"]
            self.assertIsNone(cached_data)

    async def test_cache_expires_and_evicts_by_bytes_and_entry_limit(self):
        provider = telegram.TelegramProvider()
        with patch.object(telegram, "THUMB_CACHE_MAX_BYTES", 6), \
             patch.object(telegram, "THUMB_CACHE_MAX_ENTRIES", 2):
            provider._cache_thumb("one", b"1234")
            provider._cache_thumb("two", b"5678")
            self.assertNotIn("one", provider._thumb_cache)
            self.assertEqual(provider._thumb_cache_bytes, 4)
            provider._cache_thumb("three", None)
            provider._cache_thumb("four", None)
            self.assertEqual(len(provider._thumb_cache), 2)
            self.assertEqual(provider._thumb_cache_bytes, 0)
        with patch.object(telegram, "THUMB_FAILURE_TTL_SECONDS", 0):
            provider._cache_thumb("-1001:42:unique", None)
        with patch.object(provider, "_download_thumb_uncached", AsyncMock(return_value=b"retry")) as fetch:
            self.assertEqual(await provider.download_thumb("-1001:42:unique"), b"retry")
            fetch.assert_awaited_once()


class TestThumbnailHttpApi(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_is_fast_gateway_response_and_missing_preview_stays_404(self):
        import httpx
        import catalog_api
        with tempfile.TemporaryDirectory(), patch.dict(os.environ, {
            "CONFIG_PATH": str(ROOT / "tests/fixtures/config_telegram_only.yaml"),
            "DATABASE_URL": TEST_URL or os.environ.get("DATABASE_URL", ""),
        }):
            import app
        async def blocked(ref):
            await asyncio.Event().wait()
        fake = SimpleNamespace(download_thumb=AsyncMock(side_effect=blocked))
        transport = httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            with patch.object(catalog_api, "get_provider", return_value=fake), \
                 patch.object(catalog_api, "THUMB_API_TIMEOUT_SECONDS", 0.01):
                result = await client.get("/admin/api/thumb", params={"provider": "telegram", "ref": "-1001:42:unique"})
                self.assertEqual(result.status_code, 504)
                fake.download_thumb = AsyncMock(return_value=None)
                missing = await client.get("/admin/api/thumb", params={"provider": "telegram", "ref": "-1001:42:unique"})
                self.assertEqual(missing.status_code, 404)
