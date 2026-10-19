import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "resolver"))
sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase

try:
    from app import rewrite_m3u8_playlist, looks_like_playlist, cache_ttl_for_provider, validate_proxy_url
    APP_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    APP_AVAILABLE = False

try:
    import sync as sync_module
    SYNC_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    SYNC_AVAILABLE = False


class TestProxyRewrite(unittest.TestCase):
    def setUp(self):
        if not APP_AVAILABLE:
            self.skipTest("resolver module not available")

    def test_segment_lines_rewritten(self):
        text = "#EXTM3U\n#EXT-X-VERSION:3\nseg-1.ts?token=abc\nseg-2.ts\n"
        out = rewrite_m3u8_playlist(text, "https://cdn.example.com/h/master.m3u8", "http://r:8090/px?u=")
        self.assertIn("#EXTM3U", out)
        self.assertIn("http://r:8090/px/seg.ts?u=https%3A%2F%2Fcdn.example.com%2Fh%2Fseg-1.ts%3Ftoken%3Dabc", out)
        self.assertIn("http://r:8090/px/seg.ts?u=https%3A%2F%2Fcdn.example.com%2Fh%2Fseg-2.ts", out)
        self.assertNotIn("\nseg-1.ts", out)

    def test_uri_attributes_rewritten(self):
        text = '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.key"\n#EXT-X-MEDIA:TYPE=AUDIO,URI="audio.m3u8"\nchunk.ts\n'
        out = rewrite_m3u8_playlist(text, "https://h.example.com/v/master.m3u8", "http://r:8090/px?u=")
        self.assertIn('URI="http://r:8090/px/seg.key?u=https%3A%2F%2Fh.example.com%2Fv%2Fkey.key"', out)
        self.assertIn('URI="http://r:8090/px/seg.m3u8?u=https%3A%2F%2Fh.example.com%2Fv%2Faudio.m3u8"', out)

    def test_absolute_urls_proxied(self):
        text = "#EXTM3U\nhttps://other.example.com/s/seg.ts?t=1\n"
        out = rewrite_m3u8_playlist(text, "https://h.example.com/master.m3u8", "http://r:8090/px?u=")
        self.assertIn("http://r:8090/px/seg.ts?u=https%3A%2F%2Fother.example.com%2Fs%2Fseg.ts%3Ft%3D1", out)

    def test_looks_like_playlist(self):
        self.assertTrue(looks_like_playlist("https://x/master.m3u8?t=1"))
        self.assertTrue(looks_like_playlist("https://x/f", "application/vnd.apple.mpegurl"))
        self.assertFalse(looks_like_playlist("https://x/video.mp4", "video/mp4"))

    def test_per_provider_ttl(self):
        self.assertGreaterEqual(cache_ttl_for_provider("exampleweb"), 1500)
        self.assertGreaterEqual(cache_ttl_for_provider("exampleweb"), 1500)
        self.assertGreaterEqual(cache_ttl_for_provider("unknown_xyz"), 1)

    def test_proxy_rejects_private_resolved_addresses(self):
        local = ("", [], 0, "", ("10.0.0.4", 443))
        with patch("app.socket.getaddrinfo", return_value=[local]):
            with self.assertRaises(Exception):
                validate_proxy_url("https://stream.example/video.mp4")

    def test_proxy_rejects_localhost_and_non_http_urls(self):
        with self.assertRaises(Exception):
            validate_proxy_url("http://127.0.0.1/private")
        with self.assertRaises(Exception):
            validate_proxy_url("file:///etc/passwd")


class TestStreamModes(unittest.TestCase):
    def setUp(self):
        if not SYNC_AVAILABLE:
            self.skipTest("sync module not available")
        # Hermetic: bypass the local config.yaml (CONFIG lookup first,
        # then static modes, then default).
        self._orig_config = sync_module.CONFIG
        sync_module.CONFIG = None
        self.addCleanup(self._restore)

    def _restore(self):
        sync_module.CONFIG = self._orig_config

    def test_registered_and_default_modes(self):
        self.assertEqual(sync_module.stream_mode_for_provider("telegram:123"), "proxy")
        self.assertEqual(sync_module.stream_mode_for_provider("unknownxyz:123"), "redirect")

    def test_write_stream_file_modes(self):
        import tempfile
        old = sync_module.RESOLVER_PUBLIC_URL
        try:
            sync_module.RESOLVER_PUBLIC_URL = "http://h:8090"
            with tempfile.TemporaryDirectory() as d:
                p1 = Path(d) / "a.strm"
                sync_module.write_stream_file(p1, "telegram:1")
                self.assertEqual(p1.read_text(), "http://h:8090/admin/play/telegram:1/stream?mode=proxy\n")
                p2 = Path(d) / "b.strm"
                sync_module.write_stream_file(p2, "unknownxyz:1")
                self.assertEqual(p2.read_text(), "http://h:8090/admin/play/unknownxyz:1/stream\n")
        finally:
            sync_module.RESOLVER_PUBLIC_URL = old


class TestUnsupportedEmbedHiding(PgTestCase):
    def setUp(self):
        super().setUp()
        if not SYNC_AVAILABLE:
            self.skipTest("sync module not available")
        # Guard setup: the retention guard needs a registered,
        # enabled source plus a success signal.
        from common.config_loader import AppConfig, SourceConfig, SyncConfig
        self._orig_config = sync_module.CONFIG
        self._orig_info = dict(sync_module._PROVIDER_INFO)
        sync_module.CONFIG = AppConfig(
            sync=SyncConfig(retention_days=30),
            sources={"exampleweb": SourceConfig(
                provider="exampleweb", enabled=True,
                address="https://exampleweb.example.com")})
        sync_module._PROVIDER_INFO["exampleweb"] = {
            "name": "Exampleweb", "domain": "exampleweb.example.com",
            "base_url": "https://exampleweb.example.com"}
        self.addCleanup(self._restore)

    def _restore(self):
        sync_module.CONFIG = self._orig_config
        sync_module._PROVIDER_INFO.clear()
        sync_module._PROVIDER_INFO.update(self._orig_info)

    def test_extract_embed_host(self):
        html_iframe = '<div><iframe src="https://player.example.org/e/abc123" allowfullscreen></iframe></div>'
        self.assertEqual(sync_module.extract_embed_host(html_iframe), "player.example.org")
        html_prop = '<div itemprop="embedUrl" content="https://embed.example.net/e/xyz"></div>'
        self.assertEqual(sync_module.extract_embed_host(html_prop), "embed.example.net")
        self.assertEqual(sync_module.extract_embed_host("<div>no player</div>"), "")
        self.assertEqual(sync_module.extract_embed_host(""), "")

    def test_is_unsupported_embed_host(self):
        deny = ("player.example.org", "dood", "embed.example.net")
        self.assertTrue(sync_module.is_unsupported_embed_host("player.example.org", deny))
        self.assertTrue(sync_module.is_unsupported_embed_host("embed.example.net", deny))
        self.assertFalse(sync_module.is_unsupported_embed_host("embed.example.com", deny))
        self.assertFalse(sync_module.is_unsupported_embed_host("", deny))

    def test_purge_videos_removes_files_and_rows(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            media = root / "media"
            media.mkdir()
            v = {"provider": "exampleweb", "provider_video_id": "999",
                 "title": "T", "duration": 600, "performers": []}
            sync_module.sync_unique_video(v, set(), set(), media, self.conn)
            self.assertEqual(len(list(media.glob("*.strm"))), 1)
            n = sync_module.purge_videos(self.conn, media, ["exampleweb:999"], "test",
                                         {"exampleweb"})
            self.assertEqual(n, 1)
            self.assertEqual(list(media.glob("*.strm")), [])
            self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], 0)
            self.assertEqual(self.conn.execute("SELECT count(*) FROM synced_files").fetchone()[0], 0)

    def test_embed_host_persisted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            media = root / "media"
            media.mkdir()
            v = {"provider": "exampleweb", "provider_video_id": "1000",
                 "title": "T", "duration": 600, "performers": [],
                 "embed_host": "embed.example.com"}
            sync_module.sync_unique_video(v, set(), set(), media, self.conn)
            row = self.conn.execute(
                "SELECT embed_host FROM videos WHERE id = %s",
                ("exampleweb:1000",)).fetchone()
            self.assertEqual(row[0], "embed.example.com")


class TestRateLimitPoliteness(unittest.TestCase):
    def setUp(self):
        if not SYNC_AVAILABLE:
            self.skipTest("sync module not available")

    def test_parse_retry_after(self):
        self.assertEqual(sync_module.parse_retry_after("5", 3.0), 5.0)
        self.assertEqual(sync_module.parse_retry_after("120", 3.0), 60.0)
        self.assertEqual(sync_module.parse_retry_after("junk", 3.0), 3.0)
        self.assertEqual(sync_module.parse_retry_after(None, 3.0), 3.0)
        self.assertEqual(sync_module.parse_retry_after("-2", 3.0), 3.0)


if __name__ == "__main__":
    unittest.main()
