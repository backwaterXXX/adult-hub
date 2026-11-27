"""Tests for post-scan enrichment stages, history reporting, and shutdown handling."""

import unittest

from common.config_loader import SourceConfig
from tests.test_sync_orchestration import CONN, MEDIA_DIR, SyncPassHarness


class TestEnrichmentStages(SyncPassHarness):
    def test_every_enrichment_stage_is_timed_and_limited(self):
        self.sync()

        self.assertEqual([stage for stage, _, _ in self.core.stages], [
            "metadata_scenes", "performer_title_backfill", "metadata_pbc", "metadata_tpdb",
            "metadata_stashdb", "metadata_fansdb", "telegram_title_cleanup",
        ])
        self.assertEqual([failed for _, _, failed in self.core.stages], [False] * 7)
        self.assertEqual(self.sink("backfill_scenes").args(), (CONN, MEDIA_DIR))
        self.assertEqual(self.sink("backfill_scenes").kwargs(), {"limit": self.core.SCENE_BACKFILL_PER_RUN})
        self.assertEqual(self.sink("backfill_performers_from_titles").args(), (CONN, MEDIA_DIR))
        self.assertEqual(self.sink("backfill_pbc").kwargs(), {"limit": self.core.PBC_BACKFILL_PER_RUN})
        self.assertEqual(self.sink("backfill_tpdb").kwargs(),
                         {"limit": self.core.TPDB_BACKFILL_PER_RUN})
        # The StashDB pass is limited by the PornDB knob, not its own.
        self.assertEqual(self.sink("backfill_stashdb").kwargs(),
                         {"limit": self.core.TPDB_BACKFILL_PER_RUN})
        self.assertEqual(self.sink("backfill_fansdb").kwargs(),
                         {"limit": self.core.TPDB_BACKFILL_PER_RUN})
        self.assertEqual(self.sink("cleanup_telegram_titles").args(), (CONN, MEDIA_DIR))

    def test_the_stage_totals_are_rounded_for_the_history(self):
        self.core.time.step = 0.1234

        self.sync(sync_mode="new")

        self.assertEqual(self.history()["stages"]["performer_title_backfill"],
                         {"calls": 1, "failures": 0, "total_seconds": 0.123, "max_seconds": 0.123})

    def test_a_failing_stage_is_recorded_as_a_failure_without_stopping_the_pass(self):
        self.explode("backfill_performers_from_titles", ValueError("bad title"))

        self.assertTrue(self.sync())

        self.assertIn(("performer_title_backfill", 1.0, True), self.core.stages)
        self.assertTrue(self.core.logger.has("Title performer backfill pass failed: bad title", "warning"))
        self.assertEqual(self.history()["stages"]["performer_title_backfill"]["failures"], 1)
        self.assertTrue(self.history()["success"])

    def test_an_uninstalled_optional_module_is_skipped_quietly(self):
        for module in ("sync.scene_scrapers", "sync.pbc", "sync.theporndb", "sync.stashdb", "sync.fansdb"):
            self.absent(module)

        self.assertTrue(self.sync())

        self.assertEqual([stage for stage, _, _ in self.core.stages],
                         ["performer_title_backfill", "telegram_title_cleanup"])
        self.assertEqual(self.core.logger.text("warning"), [])

    def test_a_failing_enrichment_pass_is_only_a_warning(self):
        expectations = {
            "backfill_scenes": "Scene fingerprint backfill pass failed: boom",
            "backfill_pbc": "PBC backfill pass failed: boom",
            "backfill_tpdb": "ThePornDB backfill pass failed: boom",
            "backfill_stashdb": "StashDB backfill pass failed: boom",
        }
        for name in expectations:
            self.sink(name).raises = RuntimeError("boom")

        self.assertTrue(self.sync())

        for message in expectations.values():
            self.assertTrue(self.core.logger.has(message, "warning"), message)

    def test_the_dead_stream_pass_can_be_disabled(self):
        self.use_core(config=self.config, hide_dead_streams=False)

        self.sync()

        self.assertFalse(self.sink("hide_dead_streams").called)

    def test_the_dead_stream_filter_keeps_every_video_when_it_fails(self):
        self.sink("sync_telegram_channel_pipeline").side_effect = self.core.collect_without_persisting
        self.explode("hide_dead_streams", RuntimeError("denylist unreadable"))

        self.assertTrue(self.sync())

        self.assertEqual(self.sink("hide_dead_streams").args(),
                         ({"telegram:chan-one:1": {}, "telegram:chan-two:1": {}}, CONN))
        self.assertTrue(self.core.logger.has("Dead-stream filtering failed (keeping all): denylist unreadable",
                                             "warning"))

    def test_a_failing_stream_rewrite_is_only_a_warning(self):
        self.explode("rewrite_all_stream_files", OSError("disk full"))

        self.assertTrue(self.sync())

        self.assertTrue(self.core.logger.has("Stream rewrite pass failed: disk full", "warning"))

    def test_a_failing_orphan_cleanup_is_only_a_warning(self):
        self.explode("cleanup_orphaned_records", OSError("permission denied"))

        self.assertTrue(self.sync())

        self.assertTrue(self.core.logger.has("Orphaned-record cleanup failed: permission denied", "warning"))
        self.assertEqual(self.sink("cleanup_retention").call_count, 1)


class TestHistorySourceRows(SyncPassHarness):
    def test_added_and_removed_counts_come_from_the_snapshots(self):
        self.sink("_video_id_snapshot").results = [
            {"telegram": {"telegram:chan-one:1", "telegram:chan-one:old", "telegram:chan-two:1"}},
            {"telegram": {"telegram:chan-one:1", "telegram:chan-two:1", "telegram:chan-two:new"}},
        ]

        self.sync()

        self.assertEqual({row["source"]: (row["added"], row["removed"], row["videos"])
                          for row in self.history()["sources"]},
                         {"telegram:chan-one": (0, 1, 1), "telegram:chan-two": (1, 0, 2)})

    def test_a_provider_row_counts_videos_from_the_whole_provider(self):
        self.config.sources["pctv"] = SourceConfig(provider="pctv")
        self.sink("_video_id_snapshot").results = [
            {"pctv": {"pctv:1", "pctv:2"}}, {"pctv": {"pctv:2", "pctv:3"}}]

        self.sync()

        self.assertEqual((self.rows()["pctv"]["added"], self.rows()["pctv"]["removed"],
                          self.rows()["pctv"]["videos"]), (1, 1, 2))

    def test_a_channel_row_only_counts_videos_with_its_own_prefix(self):
        self.sink("_video_id_snapshot").results = [
            {"telegram": {f"telegram:chan-one:{n}" for n in range(3)} | {"telegram:chan-two:1"}},
            {"telegram": {f"telegram:chan-one:{n}" for n in range(4)}},
        ]

        self.sync()

        self.assertEqual({row["source"]: (row["added"], row["removed"], row["videos"])
                          for row in self.history()["sources"]},
                         {"telegram:chan-one": (1, 0, 4), "telegram:chan-two": (0, 1, 0)})

    def test_a_snapshot_without_the_provider_counts_as_empty(self):
        self.sink("_video_id_snapshot").results = [{"other": {"x"}}, {}]

        self.sync()

        self.assertEqual({row["source"]: (row["added"], row["removed"], row["videos"])
                          for row in self.history()["sources"]},
                         {"telegram:chan-one": (0, 0, 0), "telegram:chan-two": (0, 0, 0)})

    def test_the_channel_label_uses_the_studio_name(self):
        self.sync()

        self.assertEqual(self.rows()["telegram:chan-one"]["label"], "tg:[One Studio]")
        self.assertEqual(self.rows()["telegram:chan-one"]["source"], "telegram:chan-one")

    def test_a_channel_without_a_studio_is_labelled_by_its_id(self):
        self.channels[0].studio = None

        self.sync()

        self.assertEqual(self.rows()["telegram:chan-one"]["label"], "tg:[chan-one]")

    def test_a_long_channel_name_is_cut_at_the_dashboard_limit(self):
        self.channels[0].studio = "A" * 26

        self.sync()

        self.assertEqual(self.rows()["telegram:chan-one"]["label"], f"tg:[{'A' * 24}…]")

    def test_a_channel_name_at_the_limit_keeps_its_full_label(self):
        self.channels[0].studio = "A" * 25

        self.sync()

        self.assertEqual(self.rows()["telegram:chan-one"]["label"], f"tg:[{'A' * 25}]")

    def test_trailing_spaces_are_dropped_from_a_truncated_label(self):
        self.channels[0].studio = ("A" * 23) + " the rest"

        self.sync()

        self.assertEqual(self.rows()["telegram:chan-one"]["label"], f"tg:[{'A' * 23}…]")

    def test_an_unconfigured_channel_keeps_its_raw_source_label(self):
        self.assertFalse(self.sync(source_filter="telegram:chan-gone"))

        self.assertEqual(self.rows()["telegram:chan-gone"]["label"], "telegram:chan-gone")
        self.assertEqual(self.rows()["telegram:chan-gone"]["success"], False)

    def test_the_channel_filter_narrows_the_history_to_one_row(self):
        self.assertTrue(self.sync(source_filter="chan-two"))

        self.assertEqual([row["source"] for row in self.history()["sources"]], ["telegram:chan-two"])

    def test_a_skipped_channel_pass_reports_the_remaining_providers(self):
        self.config.sources["pctv"] = SourceConfig(provider="pctv")

        self.assertFalse(self.sync(skip_telegram_channels=True))

        self.assertEqual([row["source"] for row in self.history()["sources"]], ["pctv"])



class TestShutdownBetweenChannels(SyncPassHarness):
    """A deploy's SIGTERM arrives mid-pass: stop at the next boundary, keep the pass consistent."""

    def test_the_pass_stops_before_starting_another_channel(self):
        self.stop_after_scans(1)

        self.assertFalse(self.sync())

        self.assertEqual(self.scanned_channels(), ["chan-one"])
        self.assertTrue(self.core.logger.has(
            "Shutdown requested before Telegram channel chan-two", "warning"))

    def test_an_interrupted_pass_reports_itself_as_interrupted(self):
        self.stop_after_scans(1)

        self.sync()

        history = self.history()
        self.assertIs(history["interrupted"], True)
        self.assertIs(history["success"], False)
        self.assertIs(self.rows()["telegram:chan-two"]["success"], False)

    def test_a_pass_that_never_saw_a_stop_is_not_marked_interrupted(self):
        self.assertTrue(self.sync())

        self.assertIs(self.history()["interrupted"], False)

    def test_the_material_already_synced_still_gets_its_publication_and_upkeep(self):
        self.stop_after_scans(1)

        self.sync()

        self.assertTrue(self.sink("flush_collected").called)
        self.assertTrue(self.sink("republish_pending_videos").called)
        self.assertEqual(self.sink("rewrite_all_stream_files").args()[2], {"telegram:chan-one"})

    def test_a_channel_never_scanned_stays_out_of_the_destructive_gate(self):
        self.stop_after_scans(1)

        self.sync()

        self.assertEqual(self.sink("rewrite_all_stream_files").args()[2], {"telegram:chan-one"})
        self.assertFalse(self.sink("cleanup_orphaned_records").called)
        self.assertFalse(self.sink("cleanup_retention").called)

    def test_no_enrichment_stage_starts_once_the_stop_is_requested(self):
        self.stop_after_scans(1)

        self.sync()

        for stage in ("metadata_scenes", "performer_title_backfill", "metadata_pbc",
                      "metadata_tpdb", "metadata_stashdb"):
            with self.subTest(stage=stage):
                self.assertTrue(self.core.logger.has(
                    f"skipping the {stage} stage until the next pass", "warning"))
        for sink in ("backfill_scenes", "backfill_performers_from_titles", "backfill_pbc",
                     "backfill_tpdb", "backfill_stashdb"):
            with self.subTest(operation=sink):
                self.assertFalse(self.sink(sink).called)

    def test_the_interrupted_pass_skips_library_maintenance_until_next_complete_pass(self):
        self.stop_after_scans(1)

        self.sync()

        self.assertFalse(self.sink("cleanup_orphaned_records").called)
        self.assertFalse(self.sink("cleanup_retention").called)
        self.assertIs(self.history()["interrupted"], True)



class TestShutdownInTheEnvironmentChannelLoop(SyncPassHarness):
    """The unmanaged-config loop walks TELEGRAM_CHANNELS and must honour the same stop."""

    def setUp(self):
        super().setUp()
        self.use_core(config=None, telegram_channels=["chan-one", "chan-two"])

    def test_the_environment_loop_stops_at_the_same_boundary(self):
        self.stop_after_scans(1)

        self.sync()

        self.assertEqual(self.scanned_channels(), ["chan-one"])
        self.assertTrue(self.core.logger.has(
            "Shutdown requested before Telegram channel chan-two", "warning"))
        self.assertIs(self.history()["interrupted"], True)



if __name__ == "__main__":
    unittest.main()
