"""Unit tests for resolver.providers.telegram_filesize."""

from __future__ import annotations

from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import resolver.providers.telegram_filesize as tfs


class TelegramFilesizeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tfs.clear_file_size_cache()

    def tearDown(self):
        tfs.clear_file_size_cache()

    def test_cache_basic_and_suffix(self):
        self.assertIsNone(tfs.get_cached_file_size(""))
        self.assertIsNone(tfs.get_cached_file_size("chan:1:100"))

        tfs.set_cached_file_size("chan:1:100", 5_000_000)
        self.assertEqual(tfs.get_cached_file_size("chan:1:100"), 5_000_000)
        # Suffix unique_id resolves the same cached size
        self.assertEqual(tfs.get_cached_file_size("100"), 5_000_000)

        # Invalid sizes are ignored
        tfs.set_cached_file_size("chan:1:100", -10)
        self.assertEqual(tfs.get_cached_file_size("chan:1:100"), 5_000_000)
        tfs.set_cached_file_size("chan:1:100", "not an int")  # type: ignore
        self.assertEqual(tfs.get_cached_file_size("chan:1:100"), 5_000_000)

    def test_cache_eviction(self):
        with patch.object(tfs, "_MAX_CACHE_ENTRIES", 3):
            tfs.set_cached_file_size("a:1:1", 100)
            tfs.set_cached_file_size("b:1:2", 200)
            tfs.set_cached_file_size("c:1:3", 300)
            tfs.set_cached_file_size("d:1:4", 400)

            self.assertIsNone(tfs.get_cached_file_size("a:1:1"))
            self.assertEqual(tfs.get_cached_file_size("d:1:4"), 400)

    def test_get_db_file_size_success(self):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = (12345678,)

        with patch("common.db.connection") as mock_ctx:
            mock_ctx.return_value.__enter__.return_value = mock_conn
            size = tfs.get_db_file_size("chan:42:999")

        self.assertEqual(size, 12345678)
        # Also cached in memory
        self.assertEqual(tfs.get_cached_file_size("chan:42:999"), 12345678)

    def test_get_db_file_size_empty_or_error(self):
        self.assertIsNone(tfs.get_db_file_size(""))

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("DB connection lost")

        with patch("common.db.connection") as mock_ctx:
            mock_ctx.return_value.__enter__.return_value = mock_conn
            size = tfs.get_db_file_size("chan:42:999")

        self.assertIsNone(size)

    def test_save_db_file_size_invalid_input(self):
        self.assertFalse(tfs.save_db_file_size("", 100))
        self.assertFalse(tfs.save_db_file_size("chan:1:100", 0))
        self.assertFalse(tfs.save_db_file_size("chan:1:100", -50))
        self.assertFalse(tfs.save_db_file_size("chan:1:100", "not an int"))  # type: ignore
        self.assertFalse(tfs.save_db_file_size("chan:1:100:variant:720p", 1000))

    def test_save_db_file_size_success(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.rowcount = 1
        mock_conn.execute.return_value = mock_cur

        with patch("common.db.connection") as mock_ctx:
            mock_ctx.return_value.__enter__.return_value = mock_conn
            mock_conn.__enter__.return_value = mock_conn
            ok = tfs.save_db_file_size("chan:42:999", 54_321_000)

        self.assertTrue(ok)
        self.assertEqual(tfs.get_cached_file_size("chan:42:999"), 54_321_000)
        mock_conn.execute.assert_called_once_with(
            "UPDATE videos SET file_size_bytes = %s WHERE id = %s AND file_size_bytes IS NULL",
            (54_321_000, "telegram:chan:42:999"),
        )

    def test_save_db_file_size_exception(self):
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = RuntimeError("DB error")

        with patch("common.db.connection") as mock_ctx:
            mock_ctx.return_value.__enter__.return_value = mock_conn
            mock_conn.__enter__.return_value = mock_conn
            ok = tfs.save_db_file_size("chan:42:999", 54_321_000)

        self.assertFalse(ok)

    async def test_resolve_file_size_in_memory_hit(self):
        tfs.set_cached_file_size("chan:1:100", 42_000)
        mock_upstream = AsyncMock()

        with patch("resolver.providers.telegram_filesize.get_db_file_size") as mock_db:
            size = await tfs.resolve_file_size("chan:1:100", fetch_upstream_fn=mock_upstream)

        self.assertEqual(size, 42_000)
        mock_db.assert_not_called()
        mock_upstream.assert_not_called()

    async def test_resolve_file_size_db_hit(self):
        mock_upstream = AsyncMock()

        with patch("resolver.providers.telegram_filesize.get_db_file_size", return_value=88_888) as mock_db:
            size = await tfs.resolve_file_size("chan:1:100", fetch_upstream_fn=mock_upstream)

        self.assertEqual(size, 88_888)
        mock_db.assert_called_once()
        mock_upstream.assert_not_called()

    async def test_resolve_file_size_upstream_hit(self):
        import asyncio

        mock_upstream = AsyncMock(return_value=999_999)

        with patch("resolver.providers.telegram_filesize.get_db_file_size", return_value=None), \
             patch("resolver.providers.telegram_filesize.save_db_file_size") as mock_save:
            size = await tfs.resolve_file_size("chan:1:100", fetch_upstream_fn=mock_upstream)
            await asyncio.sleep(0.01)

        self.assertEqual(size, 999_999)
        mock_upstream.assert_called_once_with("chan:1:100")
        self.assertEqual(tfs.get_cached_file_size("chan:1:100"), 999_999)
        mock_save.assert_called_once_with("chan:1:100", 999_999, None)

    async def test_file_size_impl_invalid_ref(self):
        size = await tfs.file_size_impl(
            provider=None,
            ref="invalid_ref_without_colons",
            acquire_lease_fn=None,
            is_channel_authorized_fn=lambda _: True,
            get_cached_message_fn=None,
        )
        self.assertIsNone(size)

    async def test_file_size_impl_unauthorized(self):
        size = await tfs.file_size_impl(
            provider=None,
            ref="chan:10:20",
            acquire_lease_fn=None,
            is_channel_authorized_fn=lambda _: False,
            get_cached_message_fn=None,
        )
        self.assertIsNone(size)

    async def test_file_size_impl_success(self):
        mock_client = MagicMock()
        mock_message = MagicMock()
        mock_doc = MagicMock()
        mock_doc.id = 555
        mock_doc.size = 123_456
        mock_message.media.document = mock_doc

        @asynccontextmanager
        async def fake_lease(channel_ref):
            yield ("account_1", mock_client)

        async def fake_get_msg(client, channel_ref, msg_id, account_id):
            return mock_message

        with patch("resolver.providers.telegram_filesize.get_db_file_size", return_value=None):
            size = await tfs.file_size_impl(
                provider=None,
                ref="chan:10:555",
                acquire_lease_fn=fake_lease,
                is_channel_authorized_fn=lambda _: True,
                get_cached_message_fn=fake_get_msg,
            )

        self.assertEqual(size, 123_456)
        self.assertEqual(tfs.get_cached_file_size("chan:10:555"), 123_456)


if __name__ == "__main__":
    unittest.main()
