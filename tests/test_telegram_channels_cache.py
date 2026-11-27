"""Unit tests for TelegramProvider channel & history caching, single-flight, and concurrency."""

import asyncio
from contextlib import asynccontextmanager
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from resolver.providers import telegram as tg
from resolver.providers.telegram_history import channel_title


class TestChannelTitleConfigLookup(unittest.IsolatedAsyncioTestCase):
    async def test_channel_title_uses_config_title_before_get_entity(self):
        client = MagicMock()
        client.get_entity = AsyncMock()
        cfg = MagicMock()
        cfg.get_telegram_channel.return_value = types.SimpleNamespace(
            studio=None,
            title="Configured Title",
        )

        title = await channel_title(
            client,
            "-100123",
            config_fn=lambda: cfg,
            entity_ref_fn=lambda ref: int(ref),
        )
        self.assertEqual(title, "Configured Title")
        client.get_entity.assert_not_awaited()

    async def test_channel_title_prefers_studio_over_title(self):
        client = MagicMock()
        client.get_entity = AsyncMock()
        cfg = MagicMock()
        cfg.get_telegram_channel.return_value = types.SimpleNamespace(
            studio="Studio Name",
            title="Configured Title",
        )

        title = await channel_title(
            client,
            "-100123",
            config_fn=lambda: cfg,
            entity_ref_fn=lambda ref: int(ref),
        )
        self.assertEqual(title, "Studio Name")
        client.get_entity.assert_not_awaited()


class TestTelegramCategoriesOptimization(unittest.IsolatedAsyncioTestCase):
    async def test_get_categories_caches_with_ttl_and_invalidates_on_config_change(self):
        prov = tg.TelegramProvider()
        calls = 0

        @asynccontextmanager
        async def mock_lease(channel_ref=None, affinity_account=None):
            yield ("acc1", AsyncMock())

        async def fake_channel_title(client, channel_ref, entity=None):
            nonlocal calls
            calls += 1
            return f"Title {channel_ref}"

        cfg = MagicMock()
        cfg.managed_revision = 1
        cfg.get_telegram_channel.return_value = types.SimpleNamespace(
            studio=None,
            title=None,
            min_duration_seconds=420,
            tags_whitelist=[],
            tags_blacklist=[],
        )

        now = 1000.0
        with patch("resolver.providers.telegram.is_channel_authorized", return_value=True), \
             patch("resolver.providers.telegram._get_channels", return_value=["-1001", "-1002"]), \
             patch("resolver.providers.telegram._get_config", return_value=cfg), \
             patch("resolver.providers.telegram._acquire_client_lease", side_effect=mock_lease), \
             patch("resolver.providers.telegram._channel_title", side_effect=fake_channel_title), \
             patch("resolver.providers.telegram._monotonic", side_effect=lambda: now):
            first = await prov.get_categories()
            self.assertEqual([c.id for c in first], ["-1001", "-1002"])
            self.assertEqual(calls, 2)

            # Within TTL -> cache hit, zero new Telethon calls
            now += 60.0
            second = await prov.get_categories()
            self.assertEqual([c.title for c in second], ["Title -1001", "Title -1002"])
            self.assertEqual(calls, 2)

            # Config revision change -> invalidates categories cache, but title cache still warm
            cfg.managed_revision = 2
            third = await prov.get_categories()
            self.assertEqual([c.id for c in third], ["-1001", "-1002"])
            self.assertEqual(calls, 2)

            # After both categories TTL and channel title TTL expire -> re-fetches
            now += tg.CHANNEL_TITLE_CACHE_TTL_SECONDS + 10.0
            fourth = await prov.get_categories()
            self.assertEqual([c.id for c in fourth], ["-1001", "-1002"])
            self.assertEqual(calls, 4)

    async def test_get_categories_single_flight_coalesces_concurrent_calls(self):
        prov = tg.TelegramProvider()
        calls = 0

        @asynccontextmanager
        async def mock_lease(channel_ref=None, affinity_account=None):
            yield ("acc1", AsyncMock())

        async def slow_channel_title(client, channel_ref, entity=None):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.03)
            return f"Live {channel_ref}"

        with patch("resolver.providers.telegram.is_channel_authorized", return_value=True), \
             patch("resolver.providers.telegram._get_channels", return_value=["-1001", "-1002"]), \
             patch("resolver.providers.telegram._get_config", return_value=None), \
             patch("resolver.providers.telegram._acquire_client_lease", side_effect=mock_lease), \
             patch("resolver.providers.telegram._channel_title", side_effect=slow_channel_title):
            results = await asyncio.gather(*(prov.get_categories() for _ in range(5)))

        self.assertEqual(calls, 2)
        for res in results:
            self.assertEqual([c.title for c in res], ["Live -1001", "Live -1002"])

    async def test_get_categories_parallel_semaphore_timeout_and_order(self):
        prov = tg.TelegramProvider()
        channels = [f"-100{i}" for i in range(8)]
        active = 0
        max_active = 0

        @asynccontextmanager
        async def mock_lease(channel_ref=None, affinity_account=None):
            yield ("acc1", AsyncMock())

        async def fake_channel_title(client, channel_ref, entity=None):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                if channel_ref == "-1003":
                    await asyncio.sleep(0.2)
                else:
                    await asyncio.sleep(0.02)
                return f"Title {channel_ref}"
            finally:
                active -= 1

        cfg = MagicMock()
        cfg.managed_revision = 1
        cfg.get_telegram_channel.side_effect = lambda ch: types.SimpleNamespace(
            studio=None,
            title=f"Fallback {ch}",
            min_duration_seconds=420,
            tags_whitelist=[],
            tags_blacklist=[],
        )

        with patch("resolver.providers.telegram.CATEGORY_TITLE_TIMEOUT_SECONDS", 0.05), \
             patch("resolver.providers.telegram.is_channel_authorized", return_value=True), \
             patch("resolver.providers.telegram._get_channels", return_value=channels), \
             patch("resolver.providers.telegram._get_config", return_value=cfg), \
             patch("resolver.providers.telegram._acquire_client_lease", side_effect=mock_lease), \
             patch("resolver.providers.telegram._channel_title", side_effect=fake_channel_title):
            categories = await prov.get_categories()

        self.assertLessEqual(max_active, tg.CATEGORIES_FETCH_CONCURRENCY)
        self.assertGreater(max_active, 1)
        self.assertEqual([c.id for c in categories], channels)
        # Timed out channel falls back to configured title without blocking others
        self.assertEqual(categories[3].title, "Fallback -1003")
        self.assertEqual(categories[0].title, "Title -1000")
        # Timed-out fallback must not be cached in long-lived title cache
        self.assertNotIn("-1003", prov._channel_title_cache)


class TestTelegramHistoryBatchCache(unittest.IsolatedAsyncioTestCase):
    def _make_msg(self, msg_id: int):
        return types.SimpleNamespace(id=msg_id)

    async def test_get_history_batch_lru_ttl_and_reuses_cached_title(self):
        prov = tg.TelegramProvider()
        fetch_calls = []
        title_calls = 0

        @asynccontextmanager
        async def mock_lease(channel_ref=None, affinity_account=None):
            client = MagicMock()
            client.get_entity = AsyncMock(return_value=types.SimpleNamespace(id=123, title="Live Channel"))
            yield ("acc1", client)

        async def fake_channel_title(client, channel_ref, entity=None):
            nonlocal title_calls
            title_calls += 1
            return "Live Channel"

        async def fake_fetch_history(client, entity, page=1, max_pages=None, offset_id=None, limit=None):
            fetch_calls.append((offset_id, limit))
            return [self._make_msg(100 if offset_id is None else offset_id - 1)]

        def fake_map(messages, category_id, title, kp, am, prepared_catalog, ch_cfg, min_dur):
            return [types.SimpleNamespace(id=f"{category_id}:{m.id}", title=title) for m in messages]

        now = 2000.0
        with patch("resolver.providers.telegram.HISTORY_CACHE_MAX_ENTRIES", 2), \
             patch("resolver.providers.telegram.is_channel_authorized", return_value=True), \
             patch("resolver.providers.telegram._get_config", return_value=None), \
             patch("resolver.providers.telegram._acquire_client_lease", side_effect=mock_lease), \
             patch("resolver.providers.telegram._channel_title", side_effect=fake_channel_title), \
             patch("resolver.providers.telegram._fetch_history", side_effect=fake_fetch_history), \
             patch("resolver.providers.telegram._load_performer_snapshot", return_value=([], {}, None)), \
             patch("resolver.providers.telegram._map_history_messages", side_effect=fake_map), \
             patch("resolver.providers.telegram._monotonic", side_effect=lambda: now):
            res1 = await prov.get_history_batch("-1001", offset_id=None, limit=50)
            self.assertEqual(len(fetch_calls), 1)
            self.assertEqual(title_calls, 1)
            self.assertEqual(res1["videos"][0]["title"], "Live Channel")

            # Same key within TTL -> cache hit
            now += 10.0
            res2 = await prov.get_history_batch("-1001", offset_id=None, limit=50)
            self.assertEqual(len(fetch_calls), 1)
            self.assertEqual(res2, res1)

            # Different offset_id -> cache miss, but reuses cached channel title!
            await prov.get_history_batch("-1001", offset_id=90, limit=50)
            self.assertEqual(len(fetch_calls), 2)
            self.assertEqual(title_calls, 1)

            # Third distinct key evicts LRU (which is (None, 50))
            await prov.get_history_batch("-1001", offset_id=80, limit=50)
            self.assertEqual(len(fetch_calls), 3)
            self.assertEqual(len(prov._history_cache), 2)

            # (None, 50) was evicted -> misses again
            await prov.get_history_batch("-1001", offset_id=None, limit=50)
            self.assertEqual(len(fetch_calls), 4)

            # Advance past TTL -> misses
            now += tg.HISTORY_CACHE_TTL_SECONDS + 5.0
            await prov.get_history_batch("-1001", offset_id=None, limit=50)
            self.assertEqual(len(fetch_calls), 5)

    async def test_get_history_batch_single_flight_and_timeout_propagation(self):
        prov = tg.TelegramProvider()
        fetch_calls = 0

        @asynccontextmanager
        async def mock_lease(channel_ref=None, affinity_account=None):
            client = MagicMock()
            client.get_entity = AsyncMock(return_value=types.SimpleNamespace(id=123, title="Live Channel"))
            yield ("acc1", client)

        async def slow_fetch_history(client, entity, page=1, max_pages=None, offset_id=None, limit=None):
            nonlocal fetch_calls
            fetch_calls += 1
            await asyncio.sleep(0.03)
            if offset_id == 999:
                raise TimeoutError("Telegram history request timed out")
            return [self._make_msg(55)]

        def fake_map(messages, category_id, title, kp, am, prepared_catalog, ch_cfg, min_dur):
            return [types.SimpleNamespace(id=f"{category_id}:{m.id}", title=title) for m in messages]

        with patch("resolver.providers.telegram.is_channel_authorized", return_value=True), \
             patch("resolver.providers.telegram._get_config", return_value=None), \
             patch("resolver.providers.telegram._acquire_client_lease", side_effect=mock_lease), \
             patch("resolver.providers.telegram._channel_title", AsyncMock(return_value="Live Channel")), \
             patch("resolver.providers.telegram._fetch_history", side_effect=slow_fetch_history), \
             patch("resolver.providers.telegram._load_performer_snapshot", return_value=([], {}, None)), \
             patch("resolver.providers.telegram._map_history_messages", side_effect=fake_map):
            results = await asyncio.gather(
                *(prov.get_history_batch("-1001", offset_id=None, limit=100) for _ in range(4))
            )
            self.assertEqual(fetch_calls, 1)
            for res in results:
                self.assertEqual(res["highest_id"], 55)

            # TimeoutError propagates and is not cached
            with self.assertRaises(TimeoutError):
                await prov.get_history_batch("-1001", offset_id=999, limit=100)
            self.assertNotIn(("-1001", 999, 100), prov._history_cache)


if __name__ == "__main__":
    unittest.main()
