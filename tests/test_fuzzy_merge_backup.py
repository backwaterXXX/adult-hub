"""Pre-merge dumps are a rollback point per window, not per batch."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from common import fuzzy_merge


def _tree(tmp: str):
    """Create a verified-looking pre-merge dump and return its path."""
    dest = Path(tmp)
    dump = dest / "adulthub-pre-fuzzy-merge-20260101-120000.dump.gz"
    dump.write_bytes(b"dump")
    Path(f"{dump}.sha256").write_text("digest")
    return dump


class MakeMergeBackupTest(unittest.TestCase):
    def _core(self, tmp: str):
        make_backup = mock.Mock(return_value=Path(tmp) / "adulthub-pre-fuzzy-merge-20260102-120000.dump.gz")
        return make_backup, {"make_backup": make_backup, "logger": mock.Mock()}

    def test_reuses_a_fresh_dump_instead_of_dumping_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump = _tree(tmp)
            make_backup, core = self._core(tmp)

            with mock.patch.dict(os.environ, {"BACKUP_DIR": tmp}, clear=False):
                path = fuzzy_merge._make_merge_backup("postgresql://unused", core)

        self.assertEqual(Path(path), dump)
        make_backup.assert_not_called()

    def test_creates_a_dump_when_the_window_holds_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_backup, core = self._core(tmp)

            with mock.patch.dict(os.environ, {"BACKUP_DIR": tmp}, clear=False):
                fuzzy_merge._make_merge_backup("postgresql://unused", core)

        make_backup.assert_called_once()

    def test_dumps_outside_the_window_are_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump = _tree(tmp)
            stale = time.time() - (fuzzy_merge.FUZZY_MERGE_BACKUP_REUSE_HOURS + 2) * 3600
            os.utime(dump, (stale, stale))
            make_backup, core = self._core(tmp)

            with mock.patch.dict(os.environ, {"BACKUP_DIR": tmp}, clear=False):
                fuzzy_merge._make_merge_backup("postgresql://unused", core)

        make_backup.assert_called_once()

    def test_dumps_without_a_checksum_sidecar_are_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump = _tree(tmp)
            Path(f"{dump}.sha256").unlink()
            make_backup, core = self._core(tmp)

            with mock.patch.dict(os.environ, {"BACKUP_DIR": tmp}, clear=False):
                fuzzy_merge._make_merge_backup("postgresql://unused", core)

        make_backup.assert_called_once()

    def test_zero_window_disables_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            _tree(tmp)
            make_backup, core = self._core(tmp)
            env = {"BACKUP_DIR": tmp, "FUZZY_MERGE_BACKUP_REUSE_HOURS": "0"}

            with mock.patch.dict(os.environ, env, clear=False):
                fuzzy_merge._make_merge_backup("postgresql://unused", core)

        make_backup.assert_called_once()

    def test_latest_dump_wins_when_several_are_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            older = _tree(tmp)
            newer = Path(tmp) / "adulthub-pre-fuzzy-merge-20260101-130000.dump.gz"
            newer.write_bytes(b"dump")
            Path(f"{newer}.sha256").write_text("digest")
            stale = time.time() - 3600
            os.utime(older, (stale, stale))
            make_backup, core = self._core(tmp)

            with mock.patch.dict(os.environ, {"BACKUP_DIR": tmp}, clear=False):
                path = fuzzy_merge._make_merge_backup("postgresql://unused", core)

        self.assertEqual(Path(path), newer)
        make_backup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
