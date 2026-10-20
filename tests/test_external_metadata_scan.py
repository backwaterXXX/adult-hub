"""Database-free tests for bounded external metadata provider passes."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sync"))

import pbc
import scene_scrapers
import stashdb
import sync
import theporndb


class TestExternalMetadataScan(unittest.TestCase):
    def setUp(self):
        self.connection = object()
        self.media_dir = ROOT / "data" / "test-media"

    def test_runs_each_bounded_provider_pass_and_reports_named_stages(self):
        progress = []
        with patch.object(pbc, "backfill_pbc", return_value=(10, 3)) as pbc_scan, \
             patch.object(theporndb, "backfill_tpdb", return_value=(10, 4)) as tpdb_scan, \
             patch.object(stashdb, "backfill_stashdb", return_value=(10, 6)) as stash_scan, \
             patch.object(scene_scrapers, "backfill_scenes", return_value=(20, 8)) as scene_scan:
            result = sync._run_external_metadata_backfill(
                self.connection, self.media_dir,
                lambda completed, total, stage: progress.append((completed, total, stage)),
            )

        self.assertEqual(progress, [
            (0, 4, "metadata_pbc"), (1, 4, "metadata_pbc"),
            (1, 4, "metadata_tpdb"), (2, 4, "metadata_tpdb"),
            (2, 4, "metadata_stashdb"), (3, 4, "metadata_stashdb"),
            (3, 4, "metadata_scenes"), (4, 4, "metadata_scenes"),
        ])
        pbc_scan.assert_called_once_with(self.connection, limit=sync.PBC_BACKFILL_PER_RUN)
        tpdb_scan.assert_called_once_with(self.connection, limit=sync.TPDB_BACKFILL_PER_RUN)
        stash_scan.assert_called_once_with(self.connection, limit=sync.TPDB_BACKFILL_PER_RUN)
        scene_scan.assert_called_once_with(self.connection, self.media_dir,
                                           limit=sync.SCENE_BACKFILL_PER_RUN)
        self.assertEqual(result, {
            "local_state": "metadata_scan_completed", "providers_completed": 4,
            "providers_failed": 0, "entities_checked": 50, "entities_enriched": 21,
        })

    def test_continues_other_passes_after_provider_timeout(self):
        progress = []
        with patch.object(pbc, "backfill_pbc", side_effect=TimeoutError()), \
             patch.object(theporndb, "backfill_tpdb", return_value=(10, 4)), \
             patch.object(stashdb, "backfill_stashdb", return_value=(10, 6)), \
             patch.object(scene_scrapers, "backfill_scenes", return_value=(20, 8)) as scene_scan:
            result = sync._run_external_metadata_backfill(
                self.connection, self.media_dir,
                lambda completed, total, stage: progress.append((completed, total, stage)),
            )

        scene_scan.assert_called_once_with(self.connection, self.media_dir,
                                           limit=sync.SCENE_BACKFILL_PER_RUN)
        self.assertEqual(progress[0], (0, 4, "metadata_pbc"))
        self.assertEqual(progress[-1], (4, 4, "metadata_scenes"))
        self.assertEqual(result, {
            "local_state": "metadata_scan_completed", "providers_completed": 3,
            "providers_failed": 1, "entities_checked": 40, "entities_enriched": 18,
        })


if __name__ == "__main__":
    unittest.main()
