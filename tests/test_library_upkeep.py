import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "resolver"))
sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import db
from pg_helper import PgTestCase, require_url, reset_db

try:
    import sync as sync_module
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


class TestBackfillPerformers(BaseUpkeepTest):
    def _seed(self, media: Path):
        db_path = media / "t.db"
        conn = _make_videos_db(db_path)
        conn.execute(
            "INSERT INTO videos (id, provider, title, slug, duration, thumbnail, performers, categories, first_seen, last_seen, tags, embed_host, playable) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("exampleweb:1", "exampleweb", "T1", "s1", 600, "", "[]", '["G"]', 1, 200, "[]", "", None))
        conn.execute(
            "INSERT INTO videos (id, provider, title, slug, duration, thumbnail, performers, categories, first_seen, last_seen, tags, embed_host, playable) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("exampleweb:2", "exampleweb", "T2", "s2", 600, "", None, '["G"]', 1, 100, "[]", "", None))
        conn.execute(
            "INSERT INTO videos (id, provider, title, slug, duration, thumbnail, performers, categories, first_seen, last_seen, tags, embed_host, playable) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("otherweb:9", "otherweb", "T9", "s9", 600, "", "[]", '["G"]', 1, 300, "[]", "", None))
        conn.commit()
        return conn

    def test_backfill_updates_db_and_nfo(self):
        orig_fetch = sync_module.fetch_json_with_retry
        orig_sleep = sync_module.time.sleep
        orig_limit = sync_module.PERFORMER_BACKFILL_PER_RUN
        calls = []
        try:
            sync_module.PERFORMER_BACKFILL_PER_RUN = 10
            sync_module.time.sleep = lambda *a: None
            sync_module.fetch_json_with_retry = lambda url, **kw: (
                calls.append(url) or {"performers": [" Jane Doe ", "Jane Doe", ""]})
            with tempfile.TemporaryDirectory() as d:
                media = Path(d)
                conn = self._seed(media)
                checked, updated = sync_module.backfill_performers(
                    conn, media, providers=("exampleweb",))
                # Explicit providers prove the generic mechanism.
                self.assertEqual(checked, 2)
                self.assertEqual(updated, 2)
                self.assertTrue(all("provider=exampleweb" in u for u in calls))
                row = conn.execute(
                    "SELECT performers FROM videos WHERE id='exampleweb:1'").fetchone()
                self.assertEqual(json.loads(row[0]), ["Jane Doe"])
                nfo = media / f"{sync_module.stable_media_basename('exampleweb:1')}.nfo"
                self.assertTrue(nfo.exists())
                self.assertIn("<actor>", nfo.read_text(encoding="utf-8"))
                self.assertIn("Jane Doe", nfo.read_text(encoding="utf-8"))
                conn.close()
        finally:
            sync_module.fetch_json_with_retry = orig_fetch
            sync_module.time.sleep = orig_sleep
            sync_module.PERFORMER_BACKFILL_PER_RUN = orig_limit

    def test_backfill_respects_cap(self):
        orig_fetch = sync_module.fetch_json_with_retry
        orig_sleep = sync_module.time.sleep
        orig_limit = sync_module.PERFORMER_BACKFILL_PER_RUN
        try:
            sync_module.PERFORMER_BACKFILL_PER_RUN = 1
            sync_module.time.sleep = lambda *a: None
            sync_module.fetch_json_with_retry = lambda url, **kw: {"performers": ["A B"]}
            with tempfile.TemporaryDirectory() as d:
                media = Path(d)
                conn = self._seed(media)
                checked, updated = sync_module.backfill_performers(
                    conn, media, providers=("exampleweb",))
                self.assertEqual(checked, 1)
                self.assertEqual(updated, 1)
                conn.close()
        finally:
            sync_module.fetch_json_with_retry = orig_fetch
            sync_module.time.sleep = orig_sleep
            sync_module.PERFORMER_BACKFILL_PER_RUN = orig_limit


class TestDeadStreamSweep(BaseUpkeepTest):

    def setUp(self):
        super().setUp()
        # Purge assertions need the retention guard to allow the
        # neutral "exampleweb" (registered + enabled + succeeded).
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
        self.addCleanup(setattr, sync_module, "CONFIG", self._orig_config)
        self.addCleanup(_restore_provider_info, self._orig_info)

    def _db(self, path=None):
        return _make_videos_db(path)

    def _row(self, conn, vid, playable=1, fails=None, checked=None):
        conn.execute(
            "INSERT INTO videos (id, provider, title, slug, duration, thumbnail,"
            " performers, categories, first_seen, last_seen, tags, embed_host,"
            " playable, stream_checked, stream_fails)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (vid, vid.split(":")[0], "T", "s", 600, "", "[]",
             '["G"]', 1, 100, "[]", "", playable, checked, fails))
        conn.commit()

    def test_first_strike_keeps(self):
        # Reliability-first: DEAD_STREAM_STRIKES_TO_PURGE=1 (default) => pierwszy
        # potwierdzony strzal od razu purgluje (1 strike = purge).
        orig_probe, orig_sleep = sync_module.probe_stream_via_resolver, sync_module.time.sleep
        orig_strikes = sync_module.DEAD_STREAM_STRIKES_TO_PURGE
        try:
            sync_module.probe_stream_via_resolver = lambda cid: False
            sync_module.time.sleep = lambda *a: None
            sync_module.DEAD_STREAM_STRIKES_TO_PURGE = 1
            with tempfile.TemporaryDirectory() as d:
                media = Path(d)
                stem = "exampleweb [x-0123456789ab]"
                for ext in (".strm", ".nfo", ".jpg"):
                    (media / f"{stem}{ext}").write_text("x")
                conn = self._db(Path(d) / "t.db")
                self._row(conn, "exampleweb:1")
                conn.execute("INSERT INTO synced_files VALUES (%s, %s, %s, %s)",
                             (f"{stem}.strm", "exampleweb:1", "g", 1))
                conn.commit()
                purged = sync_module.hide_dead_streams({}, conn, media, {"exampleweb"}, providers=("exampleweb",))
                self.assertEqual(purged, 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM videos").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT count(*) FROM dead_streams").fetchone()[0], 1)
                conn.close()
            # Tryb lenient (2 strzaly) nadal dziala gdy skonfigurowany.
            sync_module.DEAD_STREAM_STRIKES_TO_PURGE = 2
            with tempfile.TemporaryDirectory() as d:
                conn = self._db(Path(d) / "t.db")
                self._row(conn, "exampleweb:2")
                purged = sync_module.hide_dead_streams({}, conn, Path(d), {"exampleweb"}, providers=("exampleweb",))
                self.assertEqual(purged, 0)
                r = conn.execute(
                    "SELECT playable, stream_fails, stream_checked FROM videos"
                    " WHERE id='exampleweb:2'").fetchone()
                self.assertEqual(r[0], 1)
                self.assertEqual(r[1], 1)
                self.assertIsNotNone(r[2])
                conn.close()
        finally:
            sync_module.probe_stream_via_resolver = orig_probe
            sync_module.time.sleep = orig_sleep
            sync_module.DEAD_STREAM_STRIKES_TO_PURGE = orig_strikes

    def test_second_strike_purges_and_denylists(self):
        orig_probe, orig_sleep = sync_module.probe_stream_via_resolver, sync_module.time.sleep
        try:
            sync_module.probe_stream_via_resolver = lambda cid: False
            sync_module.time.sleep = lambda *a: None
            with tempfile.TemporaryDirectory() as d:
                media = Path(d)
                stem = "exampleweb [x-0123456789ab]"
                for ext in (".strm", ".nfo", ".jpg"):
                    (media / f"{stem}{ext}").write_text("x")
                conn = self._db(media / "t.db")
                self._row(conn, "exampleweb:1", playable=1, fails=1, checked=1)
                conn.execute("INSERT INTO synced_files VALUES (%s, %s, %s, %s)",
                             (f"{stem}.strm", "exampleweb:1", "g", 1))
                conn.commit()
                purged = sync_module.hide_dead_streams({}, conn, media, {"exampleweb"}, providers=("exampleweb",))
                self.assertEqual(purged, 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM videos").fetchone()[0], 0)
                self.assertEqual(list(media.glob("exampleweb*")), [])
                self.assertEqual(
                    conn.execute("SELECT reason FROM dead_streams WHERE video_id='exampleweb:1'").fetchone()[0],
                    "dead CDN stream (probe)")
                conn.close()
        finally:
            sync_module.probe_stream_via_resolver = orig_probe
            sync_module.time.sleep = orig_sleep

    def test_alive_resets_strikes(self):
        orig_probe, orig_sleep = sync_module.probe_stream_via_resolver, sync_module.time.sleep
        try:
            sync_module.probe_stream_via_resolver = lambda cid: True
            sync_module.time.sleep = lambda *a: None
            with tempfile.TemporaryDirectory() as d:
                conn = self._db(Path(d) / "t.db")
                self._row(conn, "exampleweb:1", playable=1, fails=1, checked=1)
                purged = sync_module.hide_dead_streams({}, conn, Path(d), providers=("exampleweb",))
                self.assertEqual(purged, 0)
                r = conn.execute(
                    "SELECT stream_fails, stream_checked FROM videos WHERE id='exampleweb:1'").fetchone()
                self.assertEqual(r[0], 0)
                self.assertIsNotNone(r[1])
                conn.close()
        finally:
            sync_module.probe_stream_via_resolver = orig_probe
            sync_module.time.sleep = orig_sleep

    def test_unknown_changes_nothing(self):
        orig_probe, orig_sleep = sync_module.probe_stream_via_resolver, sync_module.time.sleep
        try:
            sync_module.probe_stream_via_resolver = lambda cid: None
            sync_module.time.sleep = lambda *a: None
            with tempfile.TemporaryDirectory() as d:
                conn = self._db(Path(d) / "t.db")
                self._row(conn, "exampleweb:1")
                purged = sync_module.hide_dead_streams({}, conn, Path(d), providers=("exampleweb",))
                self.assertEqual(purged, 0)
                r = conn.execute(
                    "SELECT stream_fails, stream_checked FROM videos WHERE id='exampleweb:1'").fetchone()
                self.assertIsNone(r[0])
                self.assertIsNone(r[1])
                conn.close()
        finally:
            sync_module.probe_stream_via_resolver = orig_probe
            sync_module.time.sleep = orig_sleep

    def test_purge_cap(self):
        orig_probe, orig_sleep = sync_module.probe_stream_via_resolver, sync_module.time.sleep
        orig_cap = sync_module.DEAD_STREAM_MAX_PURGE_PER_RUN
        try:
            sync_module.probe_stream_via_resolver = lambda cid: False
            sync_module.time.sleep = lambda *a: None
            sync_module.DEAD_STREAM_MAX_PURGE_PER_RUN = 2
            with tempfile.TemporaryDirectory() as d:
                conn = self._db(Path(d) / "t.db")
                for i in range(3):
                    self._row(conn, f"exampleweb:{i}", playable=1, fails=1, checked=1)
                purged = sync_module.hide_dead_streams({}, conn, Path(d), {"exampleweb"}, providers=("exampleweb",))
                self.assertEqual(purged, 2)
                self.assertEqual(conn.execute("SELECT count(*) FROM videos").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM dead_streams").fetchone()[0], 2)
                conn.close()
        finally:
            sync_module.probe_stream_via_resolver = orig_probe
            sync_module.time.sleep = orig_sleep
            sync_module.DEAD_STREAM_MAX_PURGE_PER_RUN = orig_cap

    def test_denylist_prefilter_and_prune(self):
        orig_probe, orig_sleep = sync_module.probe_stream_via_resolver, sync_module.time.sleep
        try:
            calls = []
            sync_module.probe_stream_via_resolver = lambda cid: calls.append(cid) or True
            sync_module.time.sleep = lambda *a: None
            with tempfile.TemporaryDirectory() as d:
                conn = self._db(Path(d) / "t.db")
                conn.execute("INSERT INTO dead_streams VALUES (%s, %s, %s)",
                             ("exampleweb:9", "x", 1))
                conn.execute("INSERT INTO dead_streams VALUES (%s, %s, %s)",
                             ("exampleweb:gone", "x", 1))
                conn.commit()
                collected = {"exampleweb:9": {"video": {}}, "exampleweb:10": {"video": {}}}
                sync_module.hide_dead_streams(collected, conn, Path(d), providers=("exampleweb",))
                self.assertNotIn("exampleweb:9", collected)
                self.assertIn("exampleweb:10", collected)
                self.assertNotIn("exampleweb:9", calls)  # no probe spent on corpse
                left = {r[0] for r in conn.execute("SELECT video_id FROM dead_streams").fetchall()}
                self.assertNotIn("exampleweb:gone", left)  # delisted -> pruned
                conn.close()
        finally:
            sync_module.probe_stream_via_resolver = orig_probe
            sync_module.time.sleep = orig_sleep


def _restore_provider_info(orig_info):
    """Restore the provider registry snapshot (LIFO-safe)."""
    sync_module._PROVIDER_INFO.clear()
    sync_module._PROVIDER_INFO.update(orig_info)


if __name__ == "__main__":
    unittest.main()
