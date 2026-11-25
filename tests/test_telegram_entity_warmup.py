"""A cold Telegram client must warm its entity cache before a numeric channel resolves.

Telethon knows a channel's access hash only from the session entity cache, so right after a
resolver restart the first stream for a numeric ref failed with "Could not find the input
entity" (seen at 02:13, 03:13 and 07:00 UTC). These tests pin the one-off dialog warm-up.
"""

import unittest

from resolver.providers import telegram_message_cache as cache


class FakeClient:
    """Client double: get_entity fails like a cold Telethon client until dialogs are read."""

    def __init__(self, *, unknown_refs=(), message=None, dialogs_error=None):
        self.unknown_refs = set(unknown_refs)
        self.message = message
        self.dialogs_error = dialogs_error
        self.dialogs_calls = []
        self.entity_calls = []
        self._account_id = "default"

    async def get_dialogs(self, limit=None):
        self.dialogs_calls.append(limit)
        if self.dialogs_error is not None:
            raise self.dialogs_error
        self.unknown_refs.clear()
        return []

    async def get_entity(self, ref):
        # _entity_ref() turns numeric channel refs into ints before calling Telegram.
        self.entity_calls.append(str(ref))
        if str(ref) in self.unknown_refs:
            raise ValueError(f"Could not find the input entity for {ref} (PeerChannel)")
        return ("entity", str(ref))

    async def get_messages(self, entity, ids=None):
        return self.message


class WarmUpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cache._ENTITY_CACHE.clear()
        cache._MESSAGE_CACHE.clear()
        cache._DIALOG_WARMED.clear()

    async def test_unknown_entity_warms_dialogs_and_retries(self):
        client = FakeClient(unknown_refs={"-1001618765010"})

        entity = await cache._resolve_channel_entity(client, "-1001618765010", "acc:default")

        self.assertEqual(entity, ("entity", "-1001618765010"))
        self.assertEqual(client.dialogs_calls, [cache._DIALOG_WARMUP_LIMIT])
        self.assertEqual(client.entity_calls, ["-1001618765010", "-1001618765010"])

    async def test_warmup_runs_once_per_cooldown_window(self):
        client = FakeClient(unknown_refs={"-1001", "-1002"})

        await cache._resolve_channel_entity(client, "-1001", "acc:default")
        await cache._resolve_channel_entity(client, "-1002", "acc:default")

        self.assertEqual(len(client.dialogs_calls), 1)

    async def test_warmup_runs_again_after_the_cooldown(self):
        client = FakeClient(unknown_refs={"-1001"})
        cache._DIALOG_WARMED["acc:default"] = cache.time.monotonic() - cache._DIALOG_WARMUP_COOLDOWN - 1

        await cache._resolve_channel_entity(client, "-1001", "acc:default")

        self.assertEqual(len(client.dialogs_calls), 1)

    async def test_unrelated_value_error_is_not_warmed_and_propagates(self):
        class MissingChannelClient(FakeClient):
            async def get_entity(self, ref):
                raise ValueError("Channel not authorized: '-1001'")

        client = MissingChannelClient()

        with self.assertRaises(ValueError) as raised:
            await cache._resolve_channel_entity(client, "-1001", "acc:default")

        self.assertIn("not authorized", str(raised.exception))
        self.assertEqual(client.dialogs_calls, [])

    async def test_failed_warmup_still_reports_the_original_error(self):
        # The warm-up itself fails, so the channel stays unresolvable: the caller must see
        # the original entity error, not the warm-up failure.
        client = FakeClient(unknown_refs={"-1001"}, dialogs_error=RuntimeError("dialogs failed"))

        with self.assertRaises(ValueError) as raised:
            await cache._resolve_channel_entity(client, "-1001", "acc:default")

        self.assertIn("Could not find the input entity", str(raised.exception))
        self.assertEqual(len(client.dialogs_calls), 1)

    async def test_get_cached_message_recovers_from_a_cold_client(self):
        message = object()
        client = FakeClient(unknown_refs={"-1001"}, message=message)

        result = await cache._get_cached_message(client, "-1001", 42)

        self.assertIs(result, message)
        self.assertEqual(len(client.dialogs_calls), 1)
        self.assertIn("acc:default:-1001:42", cache._MESSAGE_CACHE)

    async def test_cached_message_skips_telegram_on_the_second_call(self):
        message = object()
        client = FakeClient(message=message)

        await cache._get_cached_message(client, "-1001", 42)
        await cache._get_cached_message(client, "-1001", 42)

        self.assertEqual(len(client.entity_calls), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
