"""Database-free tests for bounded external metadata provider passes."""

import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

import sync.pbc as pbc
import sync.scene_scrapers as scene_scrapers
import sync.stashdb as stashdb
import sync.fansdb as fansdb
import sync.theporndb as theporndb
from test_support import load_sync_module

sync = load_sync_module()


class TestExternalMetadataScan(unittest.TestCase):
    def setUp(self):
        self.connection = object()
        self.media_dir = ROOT / "data" / "test-media"

    def test_runs_each_bounded_provider_pass_and_reports_named_stages(self):
        progress = []
        with patch.object(pbc, "backfill_pbc", return_value=(10, 3)) as pbc_scan, \
             patch.object(theporndb, "backfill_tpdb", return_value=(10, 4)) as tpdb_scan, \
             patch.object(stashdb, "backfill_stashdb", return_value=(10, 6)) as stash_scan, \
             patch.object(fansdb, "backfill_fansdb", return_value=(10, 5)) as fans_scan, \
             patch.object(scene_scrapers, "backfill_scenes", return_value=(20, 8)) as scene_scan:
            result = sync._run_external_metadata_backfill(
                self.connection, self.media_dir,
                lambda completed, total, stage: progress.append((completed, total, stage)),
            )

        self.assertEqual(progress, [
            (0, 5, "metadata_pbc"), (1, 5, "metadata_pbc"),
            (1, 5, "metadata_tpdb"), (2, 5, "metadata_tpdb"),
            (2, 5, "metadata_stashdb"), (3, 5, "metadata_stashdb"),
            (3, 5, "metadata_fansdb"), (4, 5, "metadata_fansdb"),
            (4, 5, "metadata_scenes"), (5, 5, "metadata_scenes"),
        ])
        pbc_scan.assert_called_once_with(self.connection, limit=sync.PBC_BACKFILL_PER_RUN)
        tpdb_scan.assert_called_once_with(self.connection, limit=sync.TPDB_BACKFILL_PER_RUN)
        stash_scan.assert_called_once_with(self.connection, limit=sync.TPDB_BACKFILL_PER_RUN)
        fans_scan.assert_called_once_with(self.connection, limit=sync.TPDB_BACKFILL_PER_RUN)
        scene_scan.assert_called_once_with(self.connection, self.media_dir,
                                           limit=sync.SCENE_BACKFILL_PER_RUN)
        self.assertEqual(result, {
            "local_state": "metadata_scan_completed", "providers_completed": 5,
            "providers_failed": 0, "entities_checked": 60, "entities_enriched": 26,
        })

    def test_continues_other_passes_after_provider_timeout(self):
        progress = []
        with patch.object(pbc, "backfill_pbc", side_effect=TimeoutError()), \
             patch.object(theporndb, "backfill_tpdb", return_value=(10, 4)), \
             patch.object(stashdb, "backfill_stashdb", return_value=(10, 6)), \
             patch.object(fansdb, "backfill_fansdb", return_value=(10, 5)), \
             patch.object(scene_scrapers, "backfill_scenes", return_value=(20, 8)) as scene_scan:
            result = sync._run_external_metadata_backfill(
                self.connection, self.media_dir,
                lambda completed, total, stage: progress.append((completed, total, stage)),
            )

        scene_scan.assert_called_once_with(self.connection, self.media_dir,
                                           limit=sync.SCENE_BACKFILL_PER_RUN)
        self.assertEqual(progress[0], (0, 5, "metadata_pbc"))
        self.assertEqual(progress[-1], (5, 5, "metadata_scenes"))
        self.assertEqual(result, {
            "local_state": "metadata_scan_completed", "providers_completed": 4,
            "providers_failed": 1, "entities_checked": 50, "entities_enriched": 23,
        })


if __name__ == "__main__":
    unittest.main()
