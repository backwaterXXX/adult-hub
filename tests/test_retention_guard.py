"""Retention guard tests (5 scenarios on an isolated DB + tmp media).

Every scenario snapshots DB rows, media files and .strm bytes before/after
running the guarded upkeep (cleanup_retention, cleanup_orphaned_records,
purge_videos, rewrite_all_stream_files):

  (a) provider absent from configuration  -> PROTECT
  (b) provider disabled (enabled:false)   -> PROTECT
  (c) provider enabled but sync failed    -> PROTECT
  (d) provider active and succeeded       -> retention ALLOWED
  (e) disabled Telegram channel           -> PROTECT (per-channel)

Standalone calls without a success signal (providers_succeeded=None)
always protect.
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase

sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))

try:
    from test_support import load_flat_sync_module
    sync_module = load_flat_sync_module()
    from common.config_loader import AppConfig, SourceConfig, SyncConfig, TelegramChannelConfig
    SYNC_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    SYNC_AVAILABLE = False


OLD = int(time.time()) - 40 * 86400  # older than the 30-day retention
NOW = int(time.time())


def _mock_config(**sources) -> "AppConfig":
    return AppConfig(sync=SyncConfig(retention_days=30), sources=dict(sources))


def _extra_source(provider: str, enabled: bool = True) -> "SourceConfig":
    return SourceConfig(provider=provider, enabled=enabled,
                        address=f"https://{provider}.example.com")


def _register_exampleweb():
    sync_module._PROVIDER_INFO["exampleweb"] = {
        "name": "Exampleweb", "domain": "exampleweb.example.com",
        "base_url": "https://exampleweb.example.com"}


class RetentionGuardTest(PgTestCase):
    def setUp(self):
        super().setUp()
        if not SYNC_AVAILABLE:
            self.skipTest("sync module not available")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media = Path(self.tmp.name) / "media"
        self.media.mkdir(parents=True, exist_ok=True)
        self._orig_config = sync_module.CONFIG
        self.addCleanup(setattr, sync_module, "CONFIG", self._orig_config)
        self._orig_info = dict(sync_module._PROVIDER_INFO)
        self.addCleanup(_restore_provider_info, self._orig_info)
        # Neutral "exampleweb" stands in for a second registered provider.
        _register_exampleweb()
        self._orig_url = sync_module.RESOLVER_PUBLIC_URL
        sync_module.RESOLVER_PUBLIC_URL = "http://guard-test:8090"
        self.addCleanup(setattr, sync_module, "RESOLVER_PUBLIC_URL", self._orig_url)


    def _seed_expired(self, vid_id: str, provider: str):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, last_seen, first_seen)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
                "provider=EXCLUDED.provider,title=EXCLUDED.title,last_seen=EXCLUDED.last_seen,first_seen=EXCLUDED.first_seen",
                (vid_id, provider, vid_id, OLD, OLD))
        target = self.media / f"{sync_module.stable_media_basename(vid_id)}.strm"
        sync_module.write_stream_file(target, vid_id)
        with self.conn:
            self.conn.execute(
                "INSERT INTO synced_files (file_path, video_id, category, created_at)"
                " VALUES (%s, %s, 'g', %s) ON CONFLICT (file_path) DO UPDATE SET "
                "video_id=EXCLUDED.video_id,category=EXCLUDED.category,created_at=EXCLUDED.created_at",
                (target.name, vid_id, OLD))
        return target

    def _snapshot(self):
        rows = {r[0]: r[1] for r in
                self.conn.execute("SELECT id, provider FROM videos").fetchall()}
        files = sorted(p.name for p in self.media.iterdir())
        strm = {p.name: p.read_bytes() for p in self.media.glob("*.strm")}
        return rows, files, strm

    def _run_guarded_upkeep(self, succeeded):
        sync_module.cleanup_retention(self.conn, self.media, 30, succeeded)
        sync_module.cleanup_orphaned_records(self.media, self.conn, succeeded)
        ids = [r[0] for r in self.conn.execute("SELECT id FROM videos").fetchall()]
        sync_module.purge_videos(self.conn, self.media, ids, "guard-test", succeeded)
        sync_module.rewrite_all_stream_files(self.conn, self.media, succeeded)

    def test_a_absent_provider_is_protected(self):
        """Provider not present in configuration keeps rows/files/URLs."""
        sync_module.CONFIG = _mock_config(
            telegram=SourceConfig(provider="telegram", enabled=True, channels=[]))
        target = self._seed_expired("exampleweb:absent1", "exampleweb")
        before = self._snapshot()
        # Even a claimed success must not help: the source is not configured.
        self._run_guarded_upkeep({"exampleweb"})
        self.assertEqual(self._snapshot(), before)
        self.assertTrue(target.exists())

    def test_b_disabled_provider_is_protected(self):
        """enabled:false provider keeps rows/files/URLs."""
        sync_module.CONFIG = _mock_config(
            exampleweb=_extra_source("exampleweb", enabled=False))
        target = self._seed_expired("exampleweb:off1", "exampleweb")
        before = self._snapshot()
        self._run_guarded_upkeep({"exampleweb"})
        self.assertEqual(self._snapshot(), before)
        self.assertTrue(target.exists())

    def test_c_enabled_but_failed_provider_is_protected(self):
        """Enabled provider missing from providers_succeeded keeps everything;
        standalone calls (no signal) also protect."""
        sync_module.CONFIG = _mock_config(exampleweb=_extra_source("exampleweb", enabled=True))
        target = self._seed_expired("exampleweb:fail1", "exampleweb")
        before = self._snapshot()
        # Another provider succeeded, exampleweb did not.
        self._run_guarded_upkeep({"otherweb"})
        self.assertEqual(self._snapshot(), before)
        # Standalone default (no signal at all) protects as well.
        self._run_guarded_upkeep(None)
        self.assertEqual(self._snapshot(), before)
        self.assertTrue(target.exists())

    def test_a_row_awaiting_publication_is_not_an_orphan(self):
        """Publication, not the catalog row, is what a synced_files entry means.

        A video persisted in an earlier run whose files could not be written has
        no registration yet, so the orphan sweep must leave it for the retry
        pass - while an ordinary missing file is still removed.
        """
        sync_module.CONFIG = _mock_config(exampleweb=_extra_source("exampleweb", enabled=True))
        pending = "exampleweb:pending1"
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, last_seen, first_seen)"
                " VALUES (%s, 'exampleweb', %s, %s, %s)", (pending, pending, NOW, NOW))
        keeper = self._seed_expired("exampleweb:keeper11", "exampleweb")
        gone = self._seed_expired("exampleweb:gone12", "exampleweb")
        gone.unlink()

        sync_module.cleanup_orphaned_records(self.media, self.conn, {"exampleweb"})

        remaining = {r[0] for r in self.conn.execute("SELECT id FROM videos").fetchall()}
        self.assertIn(pending, remaining, "an unpublished entry is not a deletion candidate")
        self.assertIn("exampleweb:keeper11", remaining)
        self.assertNotIn("exampleweb:gone12", remaining, "a real orphan is still pruned")
        self.assertTrue(keeper.exists())

    def test_d_active_and_succeeded_provider_may_be_purged(self):
        """Registered + enabled + succeeded provider: retention applies."""
        sync_module.CONFIG = _mock_config(exampleweb=_extra_source("exampleweb", enabled=True))
        old_target = self._seed_expired("exampleweb:old9", "exampleweb")
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, last_seen, first_seen)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
                "provider=EXCLUDED.provider,title=EXCLUDED.title,last_seen=EXCLUDED.last_seen,first_seen=EXCLUDED.first_seen",
                ("exampleweb:fresh10", "exampleweb", "fresh", NOW, NOW))
        fresh_target = self.media / f"{sync_module.stable_media_basename('exampleweb:fresh10')}.strm"
        sync_module.write_stream_file(fresh_target, "exampleweb:fresh10")
        with self.conn:
            self.conn.execute(
                "INSERT INTO synced_files (file_path, video_id, category, created_at)"
                " VALUES (%s, %s, 'g', %s) ON CONFLICT (file_path) DO UPDATE SET "
                "video_id=EXCLUDED.video_id,category=EXCLUDED.category,created_at=EXCLUDED.created_at",
                (fresh_target.name, "exampleweb:fresh10", NOW))

        sync_module.cleanup_retention(self.conn, self.media, 30, {"exampleweb"})
        remaining = {r[0] for r in self.conn.execute("SELECT id FROM videos").fetchall()}
        self.assertNotIn("exampleweb:old9", remaining)
        self.assertIn("exampleweb:fresh10", remaining)
        self.assertFalse(old_target.exists())
        self.assertTrue(fresh_target.exists())

    def test_expired_telegram_item_survives_successful_channel_sync(self):
        channel = "-1001111111111"
        sync_module.CONFIG = _mock_config(telegram=SourceConfig(
            provider="telegram", enabled=True,
            channels=[TelegramChannelConfig(id=channel, enabled=True)]))
        video_id = f"telegram:{channel}:10:doc"
        target = self._seed_expired(video_id, "telegram")
        sync_module.cleanup_retention(self.conn, self.media, 30, {f"telegram:{channel}"})
        self.assertIsNotNone(self.conn.execute("SELECT id FROM videos WHERE id=%s", (video_id,)).fetchone())
        self.assertTrue(target.exists())

    def test_e_disabled_telegram_channel_is_protected(self):
        """Per-channel enabled flag: disabled channel keeps rows/files/URLs
        while the enabled channel of the same provider is collected."""
        chan_on = "-1001111111111"
        chan_off = "-1002222222222"
        sync_module.CONFIG = _mock_config(
            telegram=SourceConfig(
                provider="telegram", enabled=True,
                channels=[TelegramChannelConfig(id=chan_on, enabled=True),
                          TelegramChannelConfig(id=chan_off, enabled=False)]))
        on_vid = f"telegram:{chan_on}:10:u1"
        off_vid = f"telegram:{chan_off}:20:u2"
        on_target = self._seed_expired(on_vid, "telegram")
        off_target = self._seed_expired(off_vid, "telegram")
        off_bytes = off_target.read_bytes()
        succeeded = {"telegram", f"telegram:{chan_on}"}
        self._run_guarded_upkeep(succeeded)
        remaining = {r[0] for r in self.conn.execute("SELECT id FROM videos").fetchall()}
        self.assertNotIn(on_vid, remaining)
        self.assertIn(off_vid, remaining)
        self.assertFalse(on_target.exists())
        self.assertTrue(off_target.exists())
        self.assertEqual(off_target.read_bytes(), off_bytes)


if __name__ == "__main__":
    unittest.main()


def _restore_provider_info(orig_info):
    """Restore the provider registry snapshot (LIFO-safe)."""
    sync_module._PROVIDER_INFO.clear()
    sync_module._PROVIDER_INFO.update(orig_info)
