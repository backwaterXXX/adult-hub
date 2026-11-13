"""A Telegram timeout must not look like a message that does not exist."""
import asyncio
import unittest
from unittest.mock import patch

from resolver.providers import telegram_message_cache as message_cache


class _HangingClient:
    async def get_entity(self, _ref):
        await asyncio.sleep(30)

    async def get_messages(self, _entity, ids=None):
        await asyncio.sleep(30)


class _MissingMessageClient:
    async def get_entity(self, _ref):
        return object()

    async def get_messages(self, _entity, ids=None):
        return None


class MessageLookupTimeoutTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(message_cache, "get_telegram_metadata_timeout", return_value=0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_entity_timeout_raises_instead_of_returning_none(self):
        with self.assertRaises(TimeoutError):
            asyncio.run(message_cache._get_cached_message(
                _HangingClient(), "-1001900000001", 1, account_id="timeout-entity"))

    def test_message_timeout_raises_instead_of_returning_none(self):
        class EntityOnlyClient(_HangingClient):
            async def get_entity(self, _ref):
                return object()

        with self.assertRaises(TimeoutError):
            asyncio.run(message_cache._get_cached_message(
                EntityOnlyClient(), "-1001900000002", 2, account_id="timeout-message"))

    def test_absent_message_still_returns_none(self):
        result = asyncio.run(message_cache._get_cached_message(
            _MissingMessageClient(), "-1001900000003", 3, account_id="absent"))
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
