"""Deterministic tests for multi-account Telegram synchronization, shared cursors, and access loss."""

import asyncio
import logging
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from resolver.providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    _MEMORY_CHANNEL_ACCESS,
    get_channel_access_status,
    set_memory_only_mode,
    upsert_account,
)
from resolver.providers.telegram_router import TelegramAccountRouter
from sync.telegram_pipeline import TelegramSyncServices, sync_telegram_channel_pipeline


class SqliteConnWrapper:
    """Wrapper adapting SQLite to PostgreSQL %s parameter style."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def execute(self, sql: str, params=None):
        adapted = sql.replace("%s", "?")
        if params is not None:
            return self._conn.execute(adapted, params)
        return self._conn.execute(adapted)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()


async def _all_accounts_authorized(account_ids):
    """Fixture session files are not real Telegram sessions; treat every listed account as authorized."""
    return list(account_ids)


class TestTelegramMultiAccountSync(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        set_memory_only_mode(True)
        _MEMORY_ACCOUNTS.clear()
        _MEMORY_CHANNEL_ACCESS.clear()
        # Fixture sessions are not real Telegram sessions; the authorization filter is tested separately.
        auth_patch = patch(
            "resolver.providers.telegram_router.authorized_account_ids", _all_accounts_authorized
        )
        auth_patch.start()
        self.addCleanup(auth_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.sqlite = sqlite3.connect(":memory:")
        self.sqlite.execute("""
            CREATE TABLE telegram_channel_state (
                channel_id TEXT PRIMARY KEY,
                last_min_duration INTEGER,
                last_tags_whitelist TEXT,
                last_tags_blacklist TEXT,
                backfill_in_progress INTEGER DEFAULT 0,
                backfill_cursor INTEGER,
                backfill_completed_at INTEGER,
                latest_watermark INTEGER,
                latest_cursor INTEGER,
                latest_head INTEGER,
                updated_at INTEGER
            )
        """)
        self.sqlite.execute("""
            CREATE TABLE videos (
                id TEXT PRIMARY KEY,
                provider TEXT,
                duration INTEGER,
                tags TEXT
            )
        """)
        self.sqlite.commit()
        self.conn = SqliteConnWrapper(self.sqlite)
        self.channel = "-100999"
        self.cfg = SimpleNamespace(
            max_pages=1,
            history_batch_size=50,
            min_duration_seconds=60,
            tags_whitelist=[],
            tags_blacklist=[],
            studio="MultiAccountStudio",
        )

    def tearDown(self):
        set_memory_only_mode(False)
        self.sqlite.close()
        self.tmp.cleanup()

    def _state(self):
        cur = self.conn.execute(
            "SELECT latest_watermark, latest_cursor, latest_head, backfill_cursor FROM telegram_channel_state WHERE channel_id=?",
            (self.channel,),
        )
        return cur.fetchone()

    async def test_access_loss_failover_between_accounts(self):
        """When Account 1 loses access with ChannelPrivateError, router isolates it and fails over to Account 2."""
        router = TelegramAccountRouter()

        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f1, \
             tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f2:
            f1.write(b"s1")
            f2.write(b"s2")
            p1, p2 = f1.name, f2.name

        try:
            upsert_account("acc1", label="Account 1", session_file=p1, enabled=True)
            upsert_account("acc2", label="Account 2", session_file=p2, enabled=True)

            mock_client1 = AsyncMock()
            mock_client1.is_connected = MagicMock(return_value=True)
            mock_client1.is_user_authorized = AsyncMock(return_value=True)

            mock_client2 = AsyncMock()
            mock_client2.is_connected = MagicMock(return_value=True)
            mock_client2.is_user_authorized = AsyncMock(return_value=True)

            def factory(path, aid, ahash):
                return mock_client1 if path == p1 else mock_client2

            router._client_factory = factory

            # Account 1 selected first
            router._active_leases["acc1"] = 0
            router._active_leases["acc2"] = 1
            selected = await router.select_eligible_account(self.channel)
            self.assertEqual(selected, "acc1")

            # Simulate ChannelPrivateError on Account 1
            class ChannelPrivateError(Exception):
                pass

            router.handle_request_exception("acc1", self.channel, ChannelPrivateError("Channel private"))

            # Account 1 is now marked inaccessible / no_access in store
            self.assertIn(get_channel_access_status("acc1", self.channel), {"inaccessible", "no_access"})

            # Next selection MUST pick Account 2
            next_selected = await router.select_eligible_account(self.channel)
            self.assertEqual(next_selected, "acc2")
        finally:
            import os
            os.remove(p1)
            os.remove(p2)

    def test_total_access_loss_preserves_shared_cursor_and_does_not_purge(self):
        """When no accounts have access, sync transaction rolls back and cursor/catalog remain intact."""
        # Prepopulate existing state and videos
        self.conn.execute("""
            INSERT INTO telegram_channel_state
            (channel_id, last_min_duration, last_tags_whitelist, last_tags_blacklist, backfill_in_progress, latest_watermark, latest_cursor)
            VALUES (%s, 60, '[]', '[]', 0, 500, 450)
        """, (self.channel,))
        self.conn.execute("""
            INSERT INTO videos (id, provider, duration, tags)
            VALUES (%s, 'telegram', 120, '[]')
        """, (f"telegram:{self.channel}:490:doc1",))
        self.conn.commit()

        purged_videos = []

        def mock_purge(conn, media_dir, to_purge, reason, providers_succeeded):
            purged_videos.extend(to_purge)
            return len(to_purge)

        def mock_fetch(url, **kwargs):
            # Simulate 503 Service Unavailable when all accounts are cooling down or inaccessible
            raise ConnectionError("No eligible accounts available with access to channel")

        services = TelegramSyncServices(
            resolver_public_url="http://resolver",
            resolver_api_url="http://resolver",
            map_category=lambda x: None,
            merge_video_fields=lambda *x: None,
            purge_videos=mock_purge,
            fetch_json_with_retry=mock_fetch,
            flush_collected=lambda *x, **k: None,
            logger=logging.getLogger("test_sync"),
        )

        # Run pipeline - fetch will fail
        with self.assertRaises(RuntimeError):
            sync_telegram_channel_pipeline(
                self.channel,
                self.cfg,
                self.conn,
                Path(self.tmp.name),
                {},
                services=services,
                sync_mode="new",
            )

        # Verify cursor is completely preserved (not reset or lost)
        state = self._state()
        self.assertEqual(state[0], 500)  # watermark
        self.assertEqual(state[1], 450)  # latest_cursor

        # Verify videos table was NOT purged
        cur = self.conn.execute("SELECT id FROM videos WHERE provider='telegram'")
        rows = cur.fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(purged_videos, [])

    def test_resuming_sync_with_new_authorized_account(self):
        """After access is regained, sync resumes safely from cursor and ingests without duplicates."""
        self.conn.execute("""
            INSERT INTO telegram_channel_state
            (channel_id, last_min_duration, last_tags_whitelist, last_tags_blacklist, backfill_in_progress, latest_watermark, latest_cursor)
            VALUES (%s, 60, '[]', '[]', 0, 500, 450)
        """, (self.channel,))
        self.conn.commit()

        collected_flushed = {}

        def mock_flush(collected, conn, media_dir, prefix, raise_on_error=True):
            collected_flushed.update(collected)

        # Batch returning messages from 450 downwards
        batch_response = {
            "message_count": 2,
            "highest_id": 449,
            "next_offset_id": 400,
            "has_more": False,
            "videos": [
                {
                    "id": f"{self.channel}:449:doc_a",
                    "provider_video_id": f"{self.channel}:449:doc_a",
                    "duration": 500,
                    "categories": ["#scene"],
                    "title": "Video A",
                },
                {
                    "id": f"{self.channel}:448:doc_b",
                    "provider_video_id": f"{self.channel}:448:doc_b",
                    "duration": 600,
                    "categories": ["#scene"],
                    "title": "Video B",
                },
            ],
        }

        services = TelegramSyncServices(
            resolver_public_url="http://resolver",
            resolver_api_url="http://resolver",
            map_category=lambda x: None,
            merge_video_fields=lambda *x: None,
            purge_videos=lambda *x, **k: 0,
            fetch_json_with_retry=lambda *a, **k: batch_response,
            flush_collected=mock_flush,
            logger=logging.getLogger("test_sync"),
        )

        result = sync_telegram_channel_pipeline(
            self.channel,
            self.cfg,
            self.conn,
            Path(self.tmp.name),
            {},
            services=services,
            sync_mode="new",
        )

        # Two videos synced
        self.assertEqual(len(result), 2)
        expected_id_a = f"telegram:{self.channel}:449:doc_a"
        expected_id_b = f"telegram:{self.channel}:448:doc_b"
        self.assertIn(expected_id_a, result)
        self.assertIn(expected_id_b, result)

        # Watermark updated to 500 (since 449 < 500 and scan complete)
        state = self._state()
        self.assertEqual(state[0], 500)
        self.assertIsNone(state[1])  # latest_cursor cleared after complete

    async def test_concurrent_fair_balancing_and_flood_wait_adherence(self):
        """Router balances leases across accounts and strictly respects FloodWait without evasive rerouting."""
        router = TelegramAccountRouter()

        with tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f1, \
             tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f2, \
             tempfile.NamedTemporaryFile(suffix=".session", delete=False) as f3:
            for f in (f1, f2, f3):
                f.write(b"session-data")
            p1, p2, p3 = f1.name, f2.name, f3.name

        try:
            upsert_account("acc1", label="Acc 1", session_file=p1, enabled=True)
            upsert_account("acc2", label="Acc 2", session_file=p2, enabled=True)
            upsert_account("acc3", label="Acc 3", session_file=p3, enabled=True)

            mock_client = AsyncMock()
            mock_client.is_connected = MagicMock(return_value=True)
            mock_client.is_user_authorized = AsyncMock(return_value=True)
            router._client_factory = lambda path, aid, ahash: mock_client

            # Acquire 3 leases concurrently
            l1 = await router.acquire_channel_lease("-1001")
            l2 = await router.acquire_channel_lease("-1002")
            l3 = await router.acquire_channel_lease("-1003")

            # Each of acc1, acc2, acc3 must have been assigned 1 lease
            assigned = {l1[0], l2[0], l3[0]}
            self.assertEqual(assigned, {"acc1", "acc2", "acc3"})

            # Simulate FloodWait on acc2
            class FloodWaitError(Exception):
                seconds = 60

            router.handle_request_exception("acc2", "-1002", FloodWaitError())
            self.assertTrue(router.is_account_cooling_down("acc2"))

            # Release l1 (acc1)
            l1[2]()
            await asyncio.sleep(0.01)

            # Next lease request MUST NOT pick acc2 (cooling down), even though acc2 lease count is 1
            # It should pick acc1 which now has 0 active leases
            l4 = await router.acquire_channel_lease("-1004")
            self.assertEqual(l4[0], l1[0])

            # Clean up leases
            l2[2]()
            l3[2]()
            l4[2]()
            await asyncio.sleep(0.01)
        finally:
            import os
            for p in (p1, p2, p3):
                os.remove(p)
