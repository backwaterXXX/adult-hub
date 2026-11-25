"""Retention by archive family: a burst of dumps must not fill the volume."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from common import backup


class PruneByFamilyTest(unittest.TestCase):
    def _dump(self, directory: Path, name: str, age_seconds: float) -> Path:
        path = directory / name
        path.write_bytes(b"payload")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        Path(f"{path}.sha256").write_text("dummy")
        return path

    def test_only_the_newest_dumps_of_one_family_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            dumps = [
                self._dump(dest, f"adulthub-pre-fuzzy-merge-2026010{index}-120000.dump.gz",
                           age_seconds=(10 - index) * 60)
                for index in range(1, 6)
            ]

            pruned = backup.prune_old_backups(dest, keep_days=7, keep_count=2)

            survivors = sorted(path.name for path in dest.glob("*.dump.gz"))
            sidecars = sorted(path.name for path in dest.glob("*.sha256"))

        self.assertEqual(pruned, 3)
        self.assertEqual(survivors, sorted([dumps[-1].name, dumps[-2].name]))
        self.assertEqual(sidecars, sorted([f"{dumps[-1].name}.sha256", f"{dumps[-2].name}.sha256"]))

    def test_families_are_counted_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            merges = [self._dump(dest, f"adulthub-pre-fuzzy-merge-2026010{index}-120000.dump.gz", 30)
                      for index in range(1, 4)]
            nightly = self._dump(dest, "adulthub-20260101-020000.dump.gz", 60)

            backup.prune_old_backups(dest, keep_days=7, keep_count=1)

            survivors = {path.name for path in dest.glob("*.dump.gz")}

        self.assertEqual(survivors, {merges[-1].name, nightly.name})

    def test_age_pruning_still_applies(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            old = self._dump(dest, "adulthub-20200101-000000.dump.gz", 10 * 86400)
            fresh = self._dump(dest, "adulthub-20990101-000000.dump.gz", 60)

            backup.prune_old_backups(dest, keep_days=7, keep_count=5)

            survivors = {path.name for path in dest.glob("*.dump.gz")}

        self.assertEqual(survivors, {fresh.name})
        self.assertFalse(Path(f"{old}.sha256").exists())

    def test_current_dump_is_never_pruned(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            current = self._dump(dest, "adulthub-pre-fuzzy-merge-20260101-120000.dump.gz", 600)
            newest = self._dump(dest, "adulthub-pre-fuzzy-merge-20260102-120000.dump.gz", 10)

            backup.prune_old_backups(dest, current, keep_days=7, keep_count=1)

            survivors = {path.name for path in dest.glob("*.dump.gz")}

        self.assertEqual(survivors, {current.name, newest.name})

    def test_operator_requested_backups_are_never_count_pruned(self):
        """`backup_create` archives belong to a person and stay until they age out."""
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            operator = [
                self._dump(dest, f"adulthub-job-{index:032x}.dump.gz", age_seconds=60 * (index + 1))
                for index in range(4)
            ]
            automatic = [
                self._dump(dest, f"adulthub-pre-fuzzy-merge-2026010{index}-120000.dump.gz", 30)
                for index in range(1, 4)
            ]

            backup.prune_old_backups(dest, keep_days=7, keep_count=1)

            survivors = {path.name for path in dest.glob("*.dump.gz")}

        self.assertEqual(survivors, {path.name for path in operator} | {automatic[-1].name})

    def test_keep_count_reads_the_environment_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            dumps = [self._dump(dest, f"adulthub-pre-fuzzy-merge-2026010{index}-120000.dump.gz", 30)
                     for index in range(1, 4)]
            previous = os.environ.get("BACKUP_KEEP_COUNT")
            os.environ["BACKUP_KEEP_COUNT"] = "2"
            try:
                backup.prune_old_backups(dest, keep_days=7)
            finally:
                if previous is None:
                    os.environ.pop("BACKUP_KEEP_COUNT", None)
                else:
                    os.environ["BACKUP_KEEP_COUNT"] = previous
            survivors = {path.name for path in dest.glob("*.dump.gz")}

        self.assertEqual(survivors, {dumps[1].name, dumps[2].name})


if __name__ == "__main__":
    unittest.main()
