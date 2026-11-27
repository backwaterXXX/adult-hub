"""Expiry rules for the byte-range cache the fingerprint workers share."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import phash_range_cache
from resolver import phash_worker, video_visual_worker


class RangeCacheExpiryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _cache_dir(self, name: str, *, updated_at: float | None = None,
                   mtime: float | None = None) -> Path:
        directory = self.root / name
        directory.mkdir()
        if updated_at is not None:
            (directory / "usage.json").write_text(json.dumps({"updated_at": updated_at}))
        if mtime is not None:
            os.utime(directory, (mtime, mtime))
        return directory

    def test_the_ledger_expires_on_the_short_ttl(self) -> None:
        now = 1_000_000.0
        expired = self._cache_dir("a" * 64, updated_at=now - phash_range_cache.DEFAULT_TTL_SECONDS)
        fresh = self._cache_dir("b" * 64, updated_at=now - 60)

        self.assertEqual(phash_range_cache.purge_expired(str(self.root), now=now), 1)
        self.assertFalse(expired.exists())
        self.assertTrue(fresh.exists())

    def test_a_directory_without_a_ledger_expires_after_the_grace(self) -> None:
        now = 1_000_000.0
        abandoned = self._cache_dir("c" * 64,
                                    mtime=now - phash_range_cache.LEGACY_GRACE_SECONDS)
        running = self._cache_dir("d" * 64, mtime=now - 60)

        self.assertEqual(phash_range_cache.purge_expired(str(self.root), now=now), 1)
        self.assertFalse(abandoned.exists())
        self.assertTrue(running.exists())

    def test_a_broken_ledger_falls_back_to_the_directory_age(self) -> None:
        now = 1_000_000.0
        stale = self._cache_dir("e" * 64)
        (stale / "usage.json").write_text("not json")
        # Writing the ledger touched the directory; the age is what the fallback reads.
        os.utime(stale, (now - phash_range_cache.LEGACY_GRACE_SECONDS - 1,) * 2)

        self.assertEqual(phash_range_cache.purge_expired(str(self.root), now=now), 1)
        self.assertFalse(stale.exists())

    def test_other_names_and_symlinks_are_left_alone(self) -> None:
        now = 1_000_000.0
        old = now - phash_range_cache.LEGACY_GRACE_SECONDS - 1
        short = self._cache_dir("short", mtime=old)
        long = self._cache_dir("f" * 65, mtime=old)
        target = self._cache_dir("g" * 64, mtime=now - 60)
        link = self.root / ("h" * 64)
        link.symlink_to(target, target_is_directory=True)
        os.utime(link, (old, old), follow_symlinks=False)

        self.assertEqual(phash_range_cache.purge_expired(str(self.root), now=now), 0)
        for survivor in (short, long, target, link):
            self.assertTrue(survivor.exists())
        self.assertTrue(link.is_symlink())

    def test_a_missing_cache_directory_is_not_an_error(self) -> None:
        self.assertEqual(phash_range_cache.purge_expired(str(self.root / "absent")), 0)


class WorkerPurgeWiringTests(unittest.TestCase):
    def test_the_phash_worker_delegates_to_the_shared_expiry(self) -> None:
        with mock.patch.object(phash_worker, "PHASH_RANGE_CACHE_DIR", "/tmp/does-not-exist"):
            self.assertEqual(phash_worker._purge_expired_range_caches(now=1_000_000.0), 0)

    def test_the_visual_worker_purges_at_most_once_an_hour(self) -> None:
        calls: list[float] = []
        with mock.patch.object(video_visual_worker, "_purge_range_caches",
                               side_effect=lambda now=None: calls.append(now) or 0):
            video_visual_worker._last_range_purge = -1_000_000.0
            self.addCleanup(setattr, video_visual_worker, "_last_range_purge", 0.0)
            video_visual_worker._purge_range_caches_due(clock=lambda: 100.0)
            video_visual_worker._purge_range_caches_due(clock=lambda: 200.0)
            video_visual_worker._purge_range_caches_due(clock=lambda: 100.0 + 3601)

        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
