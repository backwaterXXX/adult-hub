import asyncio
import sys
import time
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "resolver"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase, reset_db

try:
    import app as resolver_app
    from app import parse_composite_id, PersistentLRUCache
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    parse_composite_id = None
    PersistentLRUCache = None

class TestResolver(PgTestCase):

    def setUp(self):
        super().setUp()
        if parse_composite_id is None:
            self.skipTest("resolver module not available in this environment")

    def test_parse_composite_id(self):
        # ETAP C: public core is Telegram-only. Bare IDs map to telegram,
        # registered prefixes split on the first colon, unknown prefixes
        # raise ValueError (play endpoint maps it to HTTP 400).
        self.assertEqual(parse_composite_id("telegram:ch:42:uniq"), ("telegram", "ch:42:uniq"))
        self.assertEqual(parse_composite_id("plain_id"), ("telegram", "plain_id"))
        self.assertEqual(parse_composite_id(""), ("telegram", ""))
        with self.assertRaises(ValueError):
            parse_composite_id("exampleweb:z6JKVxfrVA7")
        with self.assertRaises(ValueError):
            parse_composite_id("unknownprovider:123")

    def test_parse_composite_id_registered_prefix(self):
        # With a provider registered (e.g. via the web extension), its
        # historical provider:id values split stably on the first colon.
        import providers as _providers_pkg
        from providers.base import BaseProvider
        snapshot = dict(_providers_pkg._PROVIDERS)
        try:
            class _Stub(BaseProvider):
                name = "secondweb"
                async def get_categories(self): return []
                async def get_browse_videos(self, section="trending", page=1): return []
                async def get_category_videos(self, category_id, page=1, sort="latest"): return []
                async def search(self, query, page=1): return []
                async def resolve_stream(self, provider_video_id): return ""
            _providers_pkg.register(_Stub())
            self.assertEqual(parse_composite_id("secondweb:50935/slug"), ("secondweb", "50935/slug"))
        finally:
            _providers_pkg._PROVIDERS.clear()
            _providers_pkg._PROVIDERS.update(snapshot)

    def test_persistent_lru_cache(self):
        reset_db(self.db_url)
        cache = PersistentLRUCache(db_url=self.db_url, max_entries=3, ttl_seconds=2)

        # Test set & get
        cache.set("item1", "https://cdn.example.com/1.m3u8")
        cache.set("item2", "https://cdn.example.com/2.m3u8")
        self.assertEqual(cache.get("item1"), "https://cdn.example.com/1.m3u8")
        self.assertEqual(cache.get("item2"), "https://cdn.example.com/2.m3u8")
        self.assertIsNone(cache.get("nonexistent"))

        # Test LRU eviction (max 3 entries)
        cache.set("item3", "https://cdn.example.com/3.m3u8")
        cache.set("item4", "https://cdn.example.com/4.m3u8")
        # item1 was accessed before item3, item2 was oldest
        # Capacity is 3, so item2 should have been evicted
        self.assertEqual(cache.get("item4"), "https://cdn.example.com/4.m3u8")

        # Test persistence by creating new cache instance with same db
        cache2 = PersistentLRUCache(db_url=self.db_url, max_entries=3, ttl_seconds=2)
        self.assertEqual(cache2.get("item4"), "https://cdn.example.com/4.m3u8")

    def test_cache_clear_removes_persistent_and_memory_entries_and_counts_uniquely(self):
        from common import db

        cache = PersistentLRUCache(db_url=self.db_url, max_entries=4, ttl_seconds=60)
        try:
            cache.set("shared", "https://cdn.example/shared")
            cache.set("persistent", "https://cdn.example/persistent")
            with db.connect(self.db_url) as conn:
                conn.execute(
                    "INSERT INTO stream_cache(cache_key,stream_url,expire_at,last_accessed) "
                    "VALUES ('expired','https://cdn.example/expired',%s,%s)",
                    (time.time() - 5, time.time() - 10),
                )
            self.assertEqual(cache.clear(), 2)
            self.assertEqual(len(cache), 0)
            self.assertIsNone(cache.get("shared"))
            with db.connect(self.db_url) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM stream_cache").fetchone()[0], 0)
        finally:
            cache.close()

    def test_cache_clear_preserves_hot_entries_when_persistent_delete_fails(self):
        from unittest.mock import patch

        cache = PersistentLRUCache(db_url=self.db_url, max_entries=4, ttl_seconds=60)
        try:
            cache.set("keep-on-error", "https://cdn.example/keep")
            with patch("stream_cache.db.connect", side_effect=RuntimeError("database unavailable")):
                with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                    cache.clear()
                self.assertEqual(cache.get("keep-on-error"), "https://cdn.example/keep")
        finally:
            cache.close()

    def test_search_query_encoding(self):
        import urllib.parse
        query = "muscle bear"
        encoded = urllib.parse.quote_plus(query.strip())
        self.assertEqual(encoded, "muscle+bear")
        self.assertNotIn("%2F", encoded)

    def test_telegram_range_is_reset_to_full_stream_when_size_is_unknown(self):
        class Provider:
            def __init__(self):
                self.download_args = None

            async def file_size(self, ref):
                return None

            async def download_range(self, ref, offset, length):
                self.download_args = (ref, offset, length)

                async def chunks():
                    yield b"complete"
                return chunks()

        class Request:
            method = "GET"
            headers = {"range": "bytes=-100"}

        provider = Provider()
        with patch.object(resolver_app, "get_provider", return_value=provider):
            asyncio.run(resolver_app._proxy_telegram_response(Request(), "tg://file-ref"))
        self.assertEqual(provider.download_args, ("file-ref", 0, None))

    def test_telegram_size_lookup_failure_returns_503(self):
        from fastapi import HTTPException

        class Provider:
            def __init__(self):
                self.download_args = None

            async def file_size(self, ref):
                raise OSError("size unavailable")

            async def download_range(self, ref, offset, length):
                self.download_args = (ref, offset, length)

                async def chunks():
                    yield b"complete"
                return chunks()

        class Request:
            method = "GET"
            headers = {"range": "bytes=100-199"}

        provider = Provider()
        with patch.object(resolver_app, "get_provider", return_value=provider):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(resolver_app._proxy_telegram_response(Request(), "tg://file-ref"))
        # Silently streaming the whole file would re-download it on every seek.
        self.assertEqual(raised.exception.status_code, 503)
        self.assertIsNone(provider.download_args)

    def test_get_stream_candidate_ids_failover(self):
        with self.conn:
            self.conn.execute("INSERT INTO video_sources (video_id, source_id, provider, priority, is_active, created_at) VALUES ('vid_keeper', 'vid_keeper', 'telegram', 1, 1, 1000)")
            self.conn.execute("INSERT INTO video_sources (video_id, source_id, provider, priority, is_active, created_at) VALUES ('vid_keeper', 'vid_loser1', 'telegram', 2, 1, 1000)")
            self.conn.execute("INSERT INTO video_sources (video_id, source_id, provider, priority, is_active, created_at) VALUES ('vid_keeper', 'vid_loser2_inactive', 'telegram', 3, 0, 1000)")
            self.conn.execute("INSERT INTO video_duplicate_decisions (loser_id, keeper_id, operation_id, state, created_at) VALUES ('vid_loser1', 'vid_keeper', 'op1', 'active', 1000)")

        with patch.dict("os.environ", {"DATABASE_URL": self.db_url}):
            candidates = resolver_app._get_stream_candidate_ids("vid_keeper")
            self.assertEqual(candidates, ["vid_keeper", "vid_loser1"])

            candidates_from_loser = resolver_app._get_stream_candidate_ids("vid_loser1")
            self.assertEqual(candidates_from_loser, ["vid_keeper", "vid_loser1"])

            fallback = resolver_app._get_stream_candidate_ids("vid_unknown")
            self.assertEqual(fallback, ["vid_unknown"])

    def test_play_stream_uses_backup_source_after_primary_timeout(self):
        """A transient Telegram timeout must fall through to the next active source."""
        import os
        from fastapi.testclient import TestClient

        with self.conn:
            self.conn.execute(
                """INSERT INTO video_sources
                   (video_id, source_id, provider, priority, is_active, created_at)
                   VALUES ('primary', 'primary', 'telegram', 1, 1, 1000)"""
            )
            self.conn.execute(
                """INSERT INTO video_sources
                   (video_id, source_id, provider, priority, is_active, created_at)
                   VALUES ('primary', 'backup', 'telegram', 2, 1, 1000)"""
            )

        attempts = []

        class Provider:
            def is_channel_authorized(self, _channel_ref):
                return True

            async def resolve_stream(self, provider_video_id):
                attempts.append(provider_video_id)
                if provider_video_id == "primary":
                    raise TimeoutError("simulated Telegram timeout")
                return "https://cdn.example.test/backup.m3u8"

        with patch.dict(os.environ, {"DATABASE_URL": self.db_url}), \
             patch.object(resolver_app, "get_provider", return_value=Provider()), \
             patch.object(resolver_app, "upstream_url_alive", return_value=True):
            with TestClient(resolver_app.app) as client:
                response = client.get("/admin/play/primary/stream", follow_redirects=False)

        self.assertEqual(attempts, ["primary", "backup"])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://cdn.example.test/backup.m3u8")
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM dead_streams WHERE video_id='primary'"
            ).fetchone()
        )


if __name__ == "__main__":
    unittest.main()
