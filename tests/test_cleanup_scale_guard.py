"""Scale guard for the destructive library upkeep.

Cleanup reads "the stream file is gone" as "the catalog row may go with it". On
a missing, replaced or half-mounted media volume that is true for every entry at
once, so the guard refuses a pass that would remove more than a quarter of a
source's entries (never fewer than ten), keeps everything in that case and
leaves ordinary single losses deletable.
"""

import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase

sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))

try:
    import sync as sync_module
    from common.config_loader import AppConfig, SourceConfig, SyncConfig
    SYNC_AVAILABLE = True
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    SYNC_AVAILABLE = False

OLD = int(time.time()) - 40 * 86400  # older than the 30-day retention
NOW = int(time.time())


class CleanupScaleGuardTest(PgTestCase):
    def setUp(self):
        super().setUp()
        if not SYNC_AVAILABLE:
            self.skipTest("sync module not available")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media = Path(self.tmp.name) / "media"
        self.media.mkdir()
        self._orig_config = sync_module.CONFIG
        self.addCleanup(setattr, sync_module, "CONFIG", self._orig_config)
        self._orig_info = dict(sync_module._PROVIDER_INFO)
        self.addCleanup(_restore_provider_info, self._orig_info)
        self._orig_url = sync_module.RESOLVER_PUBLIC_URL
        sync_module.RESOLVER_PUBLIC_URL = "http://scale-test:8090"
        self.addCleanup(setattr, sync_module, "RESOLVER_PUBLIC_URL", self._orig_url)

    # -- fixtures -----------------------------------------------------------

    def _enable(self, *providers: str):
        """Register + enable the neutral sources, and credit them this run."""
        sync_module.CONFIG = AppConfig(
            sync=SyncConfig(retention_days=30),
            sources={p: SourceConfig(provider=p, enabled=True,
                                     address=f"https://{p}.example.com")
                     for p in providers})
        for p in providers:
            sync_module._PROVIDER_INFO[p] = {
                "name": p.capitalize(), "domain": f"{p}.example.com",
                "base_url": f"https://{p}.example.com"}
        return set(providers)

    def _seed(self, provider: str, count: int, present: int, *, expired: int = 0) -> list:
        """Create ``count`` catalog entries, ``present`` of them on disk."""
        ids = []
        with self.conn:
            for index in range(count):
                vid = f"{provider}:item{index:03d}"
                ids.append(vid)
                last_seen = OLD if index < expired else NOW
                name = f"{sync_module.stable_media_basename(vid)}.strm"
                self.conn.execute(
                    "INSERT INTO videos (id, provider, title, last_seen, first_seen)"
                    " VALUES (%s, %s, %s, %s, %s)", (vid, provider, vid, last_seen, last_seen))
                self.conn.execute(
                    "INSERT INTO synced_files (file_path, video_id, category, created_at)"
                    " VALUES (%s, %s, 'g', %s)", (name, vid, last_seen))
                if index < present:
                    (self.media / name).write_text(
                        f"http://scale-test:8090/admin/play/{vid}/stream\n")
        return ids

    def _stored_ids(self):
        return {row[0] for row in self.conn.execute("SELECT id FROM videos").fetchall()}

    def _warnings(self):
        """Context manager capturing the guard's refusal messages."""
        return self.assertLogs("sync", level="WARNING")

    # -- missing / emptied volume -------------------------------------------

    def test_a_missing_media_volume_stops_the_orphan_sweep(self):
        succeeded = self._enable("exampleweb")
        ids = self._seed("exampleweb", 20, 20)
        shutil.rmtree(self.media)

        with self._warnings() as captured:
            sync_module.cleanup_orphaned_records(self.media, self.conn, succeeded)

        self.assertEqual(self._stored_ids(), set(ids))
        self.assertTrue(any("media directory is unavailable" in line
                            for line in captured.output), captured.output)

    def test_an_emptied_media_volume_stops_the_orphan_sweep(self):
        succeeded = self._enable("exampleweb")
        ids = self._seed("exampleweb", 20, 20)
        for path in self.media.glob("*.strm"):
            path.unlink()

        with self._warnings() as captured:
            sync_module.cleanup_orphaned_records(self.media, self.conn, succeeded)

        self.assertEqual(self._stored_ids(), set(ids))
        self.assertTrue(any("holds no stream files" in line and "registers 20" in line
                            for line in captured.output), captured.output)

    def test_a_stray_file_does_not_make_an_emptied_volume_look_like_the_library(self):
        """A stray non-stream file must not vouch for the whole volume."""
        succeeded = self._enable("exampleweb")
        ids = self._seed("exampleweb", 20, 20)
        for path in self.media.glob("*.strm"):
            path.unlink()
        (self.media / "leftover.txt").write_text("not a stream\n")

        sync_module.cleanup_orphaned_records(self.media, self.conn, succeeded)

        self.assertEqual(self._stored_ids(), set(ids))

    # -- partial availability ------------------------------------------------

    def test_mass_file_loss_is_refused_and_reported_with_its_scale(self):
        succeeded = self._enable("exampleweb")
        ids = self._seed("exampleweb", 40, 12)

        with self._warnings() as captured:
            sync_module.cleanup_orphaned_records(self.media, self.conn, succeeded)

        self.assertEqual(self._stored_ids(), set(ids))
        self.assertTrue(any("would remove 28 of 40" in line
                            for line in captured.output), captured.output)

    def test_a_loss_below_the_guard_limit_is_still_pruned(self):
        succeeded = self._enable("exampleweb")
        ids = self._seed("exampleweb", 40, 30)

        sync_module.cleanup_orphaned_records(self.media, self.conn, succeeded)

        self.assertEqual(self._stored_ids(), set(ids[:30]))

    def test_a_single_lost_stream_is_still_pruned(self):
        succeeded = self._enable("exampleweb")
        ids = self._seed("exampleweb", 20, 19)

        sync_module.cleanup_orphaned_records(self.media, self.conn, succeeded)

        self.assertEqual(self._stored_ids(), set(ids[:19]))

    def test_a_refused_source_does_not_block_a_healthy_one(self):
        succeeded = self._enable("exampleweb", "exampleweb2")
        wiped = self._seed("exampleweb", 40, 0)
        healthy = self._seed("exampleweb2", 5, 3)

        with self._warnings() as captured:
            sync_module.cleanup_orphaned_records(self.media, self.conn, succeeded)

        self.assertEqual(self._stored_ids(), set(wiped) | set(healthy[:3]))
        self.assertTrue(any("source 'exampleweb'" in line for line in captured.output),
                        captured.output)

    # -- retention -----------------------------------------------------------

    def test_retention_refuses_a_mass_expiry(self):
        succeeded = self._enable("exampleweb")
        ids = self._seed("exampleweb", 40, 40, expired=40)

        with self._warnings() as captured:
            sync_module.cleanup_retention(self.conn, self.media, 30, succeeded)

        self.assertEqual(self._stored_ids(), set(ids))
        self.assertTrue(any("would remove 40 of 40" in line
                            for line in captured.output), captured.output)

    def test_retention_still_purges_an_ordinary_number_of_expired_videos(self):
        succeeded = self._enable("exampleweb")
        ids = self._seed("exampleweb", 40, 40, expired=3)

        sync_module.cleanup_retention(self.conn, self.media, 30, succeeded)

        self.assertEqual(self._stored_ids(), set(ids[3:]))


if __name__ == "__main__":
    unittest.main()


def _restore_provider_info(orig_info):
    """Restore the provider registry snapshot (LIFO-safe)."""
    sync_module._PROVIDER_INFO.clear()
    sync_module._PROVIDER_INFO.update(orig_info)
