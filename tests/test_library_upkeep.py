import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from common import db
from pg_helper import PgTestCase, require_url, reset_db

try:
    from test_support import load_sync_module
    sync_module = load_sync_module()
    SYNC_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    SYNC_AVAILABLE = False

try:
    PROVIDERS_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    PROVIDERS_AVAILABLE = False


_OPEN_CONNECTIONS = []


def _make_videos_db(path: Path | None = None):
    url = require_url()
    reset_db(url)
    conn = db.connect(url)
    _OPEN_CONNECTIONS.append(conn)
    return conn


def _close_all_test_dbs():
    while _OPEN_CONNECTIONS:
        c = _OPEN_CONNECTIONS.pop()
        try:
            c.close()
        except Exception:
            pass


class BaseUpkeepTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(_close_all_test_dbs)
        if not SYNC_AVAILABLE:
            self.skipTest("sync module not available")


class TestRewriteAllStreamFiles(BaseUpkeepTest):
    def _guard(self, *names):
        # Neutral "exampleweb" sources stand in for additional providers.
        from common.config_loader import AppConfig, SourceConfig, SyncConfig
        orig_config, orig_info = sync_module.CONFIG, dict(sync_module._PROVIDER_INFO)
        sync_module.CONFIG = AppConfig(
            sync=SyncConfig(retention_days=30),
            sources={n: SourceConfig(provider=n, enabled=True,
                                     address=f"https://{n}.example.com",
                                     stream_mode="proxy" if n == "exampleweb" else "redirect")
                     for n in names})
        for n in names:
            sync_module._PROVIDER_INFO[n] = {
                "name": n.capitalize(), "domain": f"{n}.example.com",
                "base_url": f"https://{n}.example.com"}
        self.addCleanup(setattr, sync_module, "CONFIG", orig_config)
        self.addCleanup(_restore_provider_info, orig_info)

    def test_rewrite_returns_changed_flag(self):
        old = sync_module.RESOLVER_PUBLIC_URL
        try:
            sync_module.RESOLVER_PUBLIC_URL = "http://h:8090"
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "x.strm"
                self.assertTrue(sync_module.write_stream_file(p, "telegram:1"))
                self.assertFalse(sync_module.write_stream_file(p, "telegram:1"))
        finally:
            sync_module.RESOLVER_PUBLIC_URL = old

    def test_rewrite_flips_stale_modes(self):
        self._guard("exampleweb", "exampleweb2")
        old = sync_module.RESOLVER_PUBLIC_URL
        try:
            sync_module.RESOLVER_PUBLIC_URL = "http://h:8090"
            with tempfile.TemporaryDirectory() as d:
                media = Path(d)
                stale = media / "exampleweb [abc-0123456789ab].strm"
                stale.write_text("http://h:8090/admin/play/exampleweb:abc/stream\n")
                fresh = media / "exampleweb2 [def-0123456789ab].strm"
                fresh.write_text("http://h:8090/admin/play/exampleweb2:def/stream\n")
                db_path = media / "t.db"
                conn = _make_videos_db(db_path)
                conn.execute("INSERT INTO synced_files VALUES (%s, %s, %s, %s)",
                             (stale.name, "exampleweb:abc", "g", 1))
                conn.execute("INSERT INTO synced_files VALUES (%s, %s, %s, %s)",
                             (fresh.name, "exampleweb2:def", "g", 1))
                conn.commit()
                checked, rewritten = sync_module.rewrite_all_stream_files(
                    conn, media, {"exampleweb", "exampleweb2"})
                conn.close()
                self.assertEqual(checked, 2)
                self.assertEqual(rewritten, 1)
                self.assertIn("?mode=proxy", stale.read_text())
                self.assertNotIn("?mode=proxy", fresh.read_text())
        finally:
            sync_module.RESOLVER_PUBLIC_URL = old

    def test_rewrite_uses_current_cleanup_and_writer_facades(self):
        with tempfile.TemporaryDirectory() as d:
            media = Path(d)
            stream = media / "exampleweb [abc-0123456789ab].strm"
            stream.write_text("old stream\n")
            conn = _make_videos_db(media / "t.db")
            conn.execute("INSERT INTO synced_files VALUES (%s, %s, %s, %s)",
                         (stream.name, "exampleweb:abc", "g", 1))
            conn.commit()
            with patch.object(sync_module, "is_cleanup_allowed", return_value=True) as allowed, \
                 patch.object(sync_module, "write_stream_file", return_value=True) as writer:
                checked, rewritten = sync_module.rewrite_all_stream_files(
                    conn, media, {"exampleweb"})
            conn.close()
        self.assertEqual((checked, rewritten), (1, 1))
        allowed.assert_called_once_with(
            "exampleweb:abc", providers_succeeded={"exampleweb"}
        )
        writer.assert_called_once_with(stream, "exampleweb:abc")


class TestDeadStreamDenylist(BaseUpkeepTest):
    def _db(self, path=None):
        return _make_videos_db(path)

    def test_denylist_prefilter_and_prune(self):
        with tempfile.TemporaryDirectory() as d:
            conn = self._db(Path(d) / "t.db")
            conn.execute("INSERT INTO dead_streams VALUES (%s, %s, %s)",
                         ("exampleweb:9", "x", 1))
            conn.execute("INSERT INTO dead_streams VALUES (%s, %s, %s)",
                         ("exampleweb:gone", "x", 1))
            conn.commit()
            collected = {"exampleweb:9": {"video": {}}, "exampleweb:10": {"video": {}}}
            sync_module.hide_dead_streams(collected, conn)
            self.assertNotIn("exampleweb:9", collected)
            self.assertIn("exampleweb:10", collected)
            left = {r[0] for r in conn.execute("SELECT video_id FROM dead_streams").fetchall()}
            self.assertIn("exampleweb:9", left)   # still listed upstream -> stays denied
            self.assertNotIn("exampleweb:gone", left)  # delisted -> pruned
            conn.close()


def _restore_provider_info(orig_info):
    """Restore the provider registry snapshot (LIFO-safe)."""
    sync_module._PROVIDER_INFO.clear()
    sync_module._PROVIDER_INFO.update(orig_info)


if __name__ == "__main__":
    unittest.main()
