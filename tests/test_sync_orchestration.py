"""The scheduled sync pass: which sync.* calls sync_orchestration makes, in what order, and what it reports."""

import contextlib
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sync_orchestration
from common.config_loader import AppConfig, SourceConfig, TelegramChannelConfig
from sync_metrics import observe_sync_stage

MEDIA_DIR = Path("/srv/media")
DATA_DIR = Path("/srv/data")
CONN = object()

# Everything the pass reaches through the sync module facade.
CORE_CALLS = (
    "init_db",
    "_video_id_snapshot",
    "migrate_synced_files",
    "migrate_categories",
    "migrate_display_titles",
    "migrate_redundant_performers",
    "cleanup_legacy_folders",
    "cleanup_old_transcodes",
    "safe_process_duplicate_jobs_checkpoint",
    "sync_telegram_channel_pipeline",
    "reconcile_telegram_channel",
    "fetch_json_with_retry",
    "hide_dead_streams",
    "flush_collected",
    "rewrite_all_stream_files",
    "backfill_performers_from_titles",
    "cleanup_orphaned_records",
    "cleanup_retention",
    "sync_jellyfin_telegram_metadata",
    "trigger_jellyfin_refresh",
    "sync_jellyfin_titles",
)

# Imported lazily inside the pass, so they are reached through sys.modules.
LAZY_FUNCTIONS = {
    "performer_catalog_sync": "sync_catalog_if_due",
    "scene_scrapers": "backfill_scenes",
    "pbc": "backfill_pbc",
    "theporndb": "backfill_tpdb",
    "stashdb": "backfill_stashdb",
    "jellyfin_people": "sync_jellyfin_people",
}


class Call:
    """One recorded invocation."""

    def __init__(self, args, kwargs):
        self.args = args
        self.kwargs = kwargs

    def __repr__(self):
        return f"Call(args={self.args!r}, kwargs={self.kwargs!r})"


class Recorder:
    """One recorded stand-in for a sync.* dependency.

    ``results``/``raises`` are consumed one entry per call and the last entry
    keeps repeating, so a scenario can fail only the second channel, say.
    """

    def __init__(self, name, result=None, results=None, raises=None, side_effect=None):
        self.name = name
        self.result = result
        self.results = list(results) if results is not None else None
        self.raises = None if raises is None else (list(raises) if isinstance(raises, list) else [raises])
        self.side_effect = side_effect
        self.trace = None
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(Call(args, kwargs))
        if self.trace is not None:
            self.trace(self.name)
        if self.side_effect is not None:
            self.side_effect(*args, **kwargs)
        error = self._take(self.raises)
        if error is not None:
            raise error if isinstance(error, BaseException) else error()
        return self._take(self.results, default=self.result)

    @staticmethod
    def _take(values, default=None):
        if values is None:
            return default
        if not isinstance(values, list):
            return values
        return values.pop(0) if len(values) > 1 else values[0]

    @property
    def called(self):
        return bool(self.calls)

    @property
    def call_count(self):
        return len(self.calls)

    def args(self, index=0):
        return self.calls[index].args

    def kwargs(self, index=0):
        return self.calls[index].kwargs

    def every_kwargs(self):
        return [call.kwargs for call in self.calls]

    def every_args(self):
        return [call.args for call in self.calls]


class FakeTime:
    """Clock where every monotonic read advances by one fixed step, so durations are exact."""

    def __init__(self, wall=(1_000, 1_060), step=1.0):
        self.wall_values = list(wall)
        self.step = step
        self._monotonic = 0.0

    def monotonic(self):
        value = self._monotonic
        self._monotonic += self.step
        return value

    def time(self):
        if len(self.wall_values) > 1:
            return self.wall_values.pop(0)
        return self.wall_values[0]


class LogCapture:
    def __init__(self):
        self.entries = []

    def _record(self, level, message, *args):
        self.entries.append((level, message % args if args else str(message)))

    def debug(self, message, *args):
        self._record("debug", message, *args)

    def info(self, message, *args):
        self._record("info", message, *args)

    def warning(self, message, *args):
        self._record("warning", message, *args)

    def error(self, message, *args):
        self._record("error", message, *args)

    def critical(self, message, *args):
        self._record("critical", message, *args)

    def text(self, level=None):
        return [msg for entry_level, msg in self.entries if level is None or entry_level == level]

    def has(self, needle, level=None):
        return any(needle in message for message in self.text(level))


class CoreDouble:
    """The whole sync.* surface the orchestration reaches, recorded call by call."""

    def __init__(self, *, config=None, sync_telegram=True, hide_dead_streams=True,
                 telegram_channels=(), telegram_max_pages=3):
        self.time = FakeTime()
        self.logger = LogCapture()
        self.MEDIA_DIR = MEDIA_DIR
        self.DATA_DIR = DATA_DIR
        self.RESOLVER_API_URL = "http://resolver:8090"
        self.RESOLVER_PUBLIC_URL = "http://public.example:8090"
        self.RETENTION_DAYS = 30
        self.SYNC_TELEGRAM = sync_telegram
        self.HIDE_DEAD_STREAMS = hide_dead_streams
        self.TELEGRAM_CHANNELS = list(telegram_channels)
        self.TELEGRAM_MAX_PAGES = telegram_max_pages
        self.SCENE_BACKFILL_PER_RUN = 11
        self.PBC_BACKFILL_PER_RUN = 22
        self.TPDB_BACKFILL_PER_RUN = 33
        self.JELLYFIN_PEOPLE_PER_RUN = 44
        self.JELLYFIN_URL = "http://jellyfin:8096"
        self.JELLYFIN_API_KEY = "jf-key"
        self.CONFIG = config
        self._ACTIVE_SYNC_METRICS = None
        self.order = []
        self.stages = []
        self.history_reports = []
        self.sinks = {}

        for name in CORE_CALLS:
            self._register(Recorder(name))
        self.sinks["init_db"].result = CONN
        self.sinks["_video_id_snapshot"].result = {}
        self.sinks["sync_telegram_channel_pipeline"].side_effect = self.collect_videos
        self.sinks["reconcile_telegram_channel"].result = {"checked": 1}

        self.lazy_modules = {}
        for module_name, function_name in LAZY_FUNCTIONS.items():
            recorder = self._register(Recorder(function_name))
            module = types.ModuleType(module_name)
            setattr(module, function_name, recorder)
            self.lazy_modules[module_name] = module
        self.sinks["sync_catalog_if_due"].result = (12, 3)

    def _register(self, recorder):
        self.sinks[recorder.name] = recorder
        setattr(self, recorder.name, recorder)
        recorder.trace = self.order.append
        return recorder

    def collect_videos(self, channel_id, _channel, _conn, _media, collected, **_kwargs):
        collected[f"telegram:{channel_id}:1"] = {}

    def record_http_retry(self, *_args, **_kwargs):
        metrics = self._ACTIVE_SYNC_METRICS
        metrics["http"]["retries"] += 1
        metrics["http_hosts"].setdefault("example.org", {"failures": 1, "retries": 1, "rate_limited": 0})

    def _observe_sync_stage(self, name, elapsed, failed=False):
        self.stages.append((name, elapsed, failed))
        observe_sync_stage(self._ACTIVE_SYNC_METRICS, name, elapsed, failed)

    def _write_sync_history(self, report):
        self.history_reports.append(report)

    def _safe_error_detail(self, error):
        return f"safe detail for {error}"


class SyncPassHarness(unittest.TestCase):
    """Run one pass against the recorded surface and inspect what it did."""

    def setUp(self):
        self.channels = [
            TelegramChannelConfig(id="chan-one", studio="One Studio"),
            TelegramChannelConfig(id="chan-two", studio="Two Studio"),
            TelegramChannelConfig(id="chan-off", studio="Off Studio", enabled=False),
        ]
        self.config = AppConfig(sources={
            "telegram": SourceConfig(provider="telegram", channels=self.channels)
        })
        self.limiter = Recorder("snapshot_host_rate_limit_stats", results=[{}, {"requests": 3}])
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(sync_orchestration, "snapshot_host_rate_limit_stats", self.limiter))
        self.use_core(config=self.config)

    def use_core(self, **overrides):
        self.core = CoreDouble(**overrides)
        self.stack.enter_context(patch.object(sync_orchestration, "_core", self.core))
        self.stack.enter_context(patch.dict(sys.modules, dict(self.core.lazy_modules)))
        return self.core

    def sink(self, name):
        return self.core.sinks[name]

    def explode(self, name, error=None):
        """Make one dependency raise, on every call."""
        self.sink(name).raises = error or RuntimeError(f"{name} exploded")

    def absent(self, module_name):
        """Make a lazily imported optional module look uninstalled."""
        self.stack.enter_context(patch.dict(sys.modules, {module_name: None}))

    def sync(self, **kwargs):
        return sync_orchestration._run_sync_locked(**kwargs)

    def history(self):
        self.assertEqual(len(self.core.history_reports), 1, "the pass must write exactly one history entry")
        return self.core.history_reports[0]

    def rows(self):
        return {row["source"]: row for row in self.history()["sources"]}

    def scanned_channels(self):
        return [args[0] for args in self.sink("sync_telegram_channel_pipeline").every_args()]


class TestPassOpeningAndClosing(SyncPassHarness):
    def test_the_rate_limiter_counters_are_cleared_at_both_ends(self):
        self.sync()

        self.assertEqual(self.limiter.every_kwargs(), [{"reset": True}, {"reset": True}])
        self.assertEqual(self.history()["http"]["rate_limit"], {"requests": 3})

    def test_a_fresh_metrics_bucket_replaces_the_previous_run(self):
        self.core._ACTIVE_SYNC_METRICS = {"stages": {"stale": {}}, "http": {}, "http_hosts": {},
                                         "left_over": True}

        self.sync()

        self.assertNotIn("stale", self.history()["stages"])
        self.assertNotIn("left_over", self.core._ACTIVE_SYNC_METRICS)
        self.assertEqual(self.rows()["telegram:chan-one"]["success"], True)

    def test_the_opening_log_names_the_paths_and_knobs_in_use(self):
        self.sync()

        self.assertTrue(self.core.logger.has("Starting Adult-Hub sync (deduplicated)...", "info"))
        for needle in (f"Media Dir: {MEDIA_DIR}", f"Resolver API: {self.core.RESOLVER_API_URL}",
                       f"Resolver Public URL: {self.core.RESOLVER_PUBLIC_URL}",
                       f"Retention Days: {self.core.RETENTION_DAYS}"):
            self.assertTrue(self.core.logger.has(needle, "info"), needle)

    def test_the_history_records_the_window_the_pass_ran_in(self):
        self.core.time.wall_values = [1_000, 1_060]

        self.sync()

        report = self.history()
        self.assertEqual((report["started_at"], report["ended_at"]), (1000, 1060))
        self.assertEqual(report["duration_seconds"], 60)
        self.assertEqual(report["stages"]["metadata_scenes"]["calls"], 1)

    def test_a_clock_that_moves_backwards_still_reports_a_non_negative_duration(self):
        self.core.time.wall_values = [1_060, 1_000]

        self.sync()

        self.assertEqual(self.history()["duration_seconds"], 0)

    def test_the_history_records_the_mode_and_the_filters(self):
        self.sync(source_filter="chan-two", sync_mode="backfill", skip_telegram_channels=True)

        report = self.history()
        self.assertEqual(report["sync_mode"], "backfill")
        self.assertEqual(report["source_filter"], "chan-two")
        self.assertEqual(report["skip_telegram_channels"], True)

    def test_a_missing_config_counts_as_revision_zero(self):
        self.use_core(config=None)

        self.assertTrue(self.sync())

        self.assertEqual(self.history()["managed_config_revision"], 0)

    def test_a_clean_pass_reports_success(self):
        self.assertTrue(self.sync())

        report = self.history()
        self.assertTrue(report["success"])
        self.assertEqual(sorted(self.rows()), ["telegram:chan-one", "telegram:chan-two"])
        self.assertTrue(self.core.logger.has("Sync completed successfully.", "info"))
        self.assertEqual(report["jellyfin_refresh_poll"], {})

    def test_an_expected_source_that_never_synced_marks_the_run_failed(self):
        self.config.sources["pctv"] = SourceConfig(provider="pctv")

        self.assertFalse(self.sync())

        report = self.history()
        self.assertFalse(report["success"])
        self.assertEqual(self.rows()["pctv"]["success"], False)
        self.assertIsNone(self.rows()["pctv"]["error"])
        self.assertTrue(self.core.logger.has("Sync completed with source failures: pctv", "error"))

    def test_the_http_counters_are_copied_from_the_live_metrics(self):
        self.sink("sync_telegram_channel_pipeline").side_effect = self.core.record_http_retry

        self.sync()

        http = self.history()["http"]
        self.assertEqual(http["retries"], 2)
        self.assertEqual(http["failed_attempts"], 0)
        self.assertEqual(http["hosts"], {"example.org": {"failures": 1, "retries": 1, "rate_limited": 0}})


class TestMaintenanceAheadOfTheScans(SyncPassHarness):
    def test_the_file_migrations_run_before_any_source_is_scanned(self):
        self.sync()

        order = self.core.order
        scanned = order.index("sync_telegram_channel_pipeline")
        self.assertEqual(order[:scanned], [
            "init_db", "sync_catalog_if_due", "_video_id_snapshot", "migrate_synced_files",
            "migrate_categories", "migrate_display_titles", "migrate_redundant_performers",
            "cleanup_legacy_folders", "cleanup_old_transcodes",
            "safe_process_duplicate_jobs_checkpoint",
        ])
        self.assertEqual(self.sink("init_db").args(), ())
        self.assertEqual(self.sink("_video_id_snapshot").args(), (CONN,))
        for name in ("migrate_synced_files", "migrate_categories", "migrate_display_titles",
                     "migrate_redundant_performers"):
            self.assertEqual(self.sink(name).args(), (CONN, MEDIA_DIR), name)
        self.assertEqual(self.sink("cleanup_legacy_folders").args(), (MEDIA_DIR,))
        self.assertEqual(self.sink("cleanup_old_transcodes").args(), ())

    def test_the_catalogue_refresh_reports_a_successful_update(self):
        self.sync()

        self.assertEqual(self.sink("sync_catalog_if_due").args(), (CONN,))
        self.assertTrue(self.core.logger.has("Performer catalogue refreshed: 12 people, 3 aliases", "info"))

    def test_the_catalogue_refresh_is_silent_when_it_reports_nothing(self):
        self.sink("sync_catalog_if_due").result = ()

        self.sync()

        self.assertFalse(self.core.logger.has("Performer catalogue refreshed", "info"))
        self.assertFalse(self.core.logger.has("catalogue refresh failed", "warning"))

    def test_a_failing_catalogue_refresh_keeps_the_last_local_copy(self):
        self.explode("sync_catalog_if_due", ValueError("stashdb unreachable"))

        self.assertTrue(self.sync())

        self.assertTrue(self.core.logger.has(
            "Performer catalogue refresh failed; using last local copy: stashdb unreachable", "warning"))

    def test_the_first_duplicate_checkpoint_failure_is_only_a_warning(self):
        checkpoint = self.sink("safe_process_duplicate_jobs_checkpoint")

        def raise_once(*_args, **_kwargs):
            if checkpoint.call_count == 1:
                raise OSError("ledger busy")

        checkpoint.side_effect = raise_once

        self.assertTrue(self.sync())

        self.assertGreater(checkpoint.call_count, 1, "later checkpoints still run")
        self.assertTrue(self.core.logger.has(
            "Could not process queued duplicate decisions: ledger busy", "warning"))

    def test_a_duplicate_checkpoint_failure_after_the_flush_does_not_abort_the_pass(self):
        def abort(*_args, **_kwargs):
            if self.sink("flush_collected").called:
                raise RuntimeError("ledger unreadable")

        self.sink("safe_process_duplicate_jobs_checkpoint").side_effect = abort

        self.assertTrue(self.sync())

        report = self.history()
        self.assertTrue(report["success"])
        self.assertTrue(self.core.logger.has(
            "Could not process queued duplicate decisions: ledger unreadable", "warning"))
        self.assertTrue(self.sink("sync_jellyfin_titles").called, "the closing stages still run")

    def test_a_checkpoint_that_fails_every_time_still_finishes_the_pass(self):
        self.explode("safe_process_duplicate_jobs_checkpoint", OSError("ledger busy"))

        self.assertTrue(self.sync())

        self.assertEqual(self.sink("safe_process_duplicate_jobs_checkpoint").call_count, 7)
        self.assertEqual(
            self.core.logger.text("warning").count(
                "Could not process queued duplicate decisions: ledger busy"), 7)
        for name in ("rewrite_all_stream_files", "backfill_performers_from_titles",
                     "cleanup_orphaned_records", "cleanup_retention", "sync_jellyfin_titles"):
            self.assertTrue(self.sink(name).called, name)

    def test_only_the_checkpoint_before_the_scans_has_no_in_memory_records(self):
        self.sync()

        checkpoint = self.sink("safe_process_duplicate_jobs_checkpoint")
        self.assertEqual(checkpoint.args(), (CONN, MEDIA_DIR, None))
        collected = self.sink("flush_collected").args()[0]
        for args in checkpoint.every_args()[1:]:
            self.assertIs(args[2], collected)

    def test_the_remaining_videos_are_flushed_once_after_the_scans(self):
        self.sync()

        flush = self.sink("flush_collected")
        self.assertEqual(flush.call_count, 1)
        self.assertEqual(flush.args(), ({"telegram:chan-one:1": {}, "telegram:chan-two:1": {}},
                                        CONN, MEDIA_DIR, "remaining"))


class TestConfiguredTelegramChannels(SyncPassHarness):
    def test_only_enabled_channels_are_scanned_and_reported(self):
        self.sync()

        self.assertEqual(self.scanned_channels(), ["chan-one", "chan-two"])
        self.assertEqual(sorted(self.rows()), ["telegram:chan-one", "telegram:chan-two"])

    def test_each_channel_is_scanned_in_the_requested_mode(self):
        self.sync(sync_mode="new")

        self.assertEqual(self.sink("sync_telegram_channel_pipeline").every_kwargs(),
                         [{"sync_mode": "new"}] * 2)
        self.assertEqual(self.sink("sync_telegram_channel_pipeline").args(1)[0], "chan-two")
        self.assertIs(self.sink("sync_telegram_channel_pipeline").args(1)[1], self.channels[1])
        self.assertEqual(self.sink("sync_telegram_channel_pipeline").args()[2:4], (CONN, MEDIA_DIR))
        self.assertIs(self.sink("sync_telegram_channel_pipeline").args()[4],
                      self.sink("flush_collected").args()[0])

    def test_the_scanned_channels_share_one_success_gate_with_the_destructive_passes(self):
        self.sync()

        gate = self.sink("rewrite_all_stream_files").args()[2]
        self.assertEqual(gate, {"telegram:chan-one", "telegram:chan-two"})
        self.assertEqual(self.sink("rewrite_all_stream_files").args()[:2], (CONN, MEDIA_DIR))
        self.assertIs(self.sink("cleanup_orphaned_records").args()[2], gate)
        self.assertEqual(self.sink("cleanup_orphaned_records").args()[:2], (MEDIA_DIR, CONN))
        self.assertIs(self.sink("cleanup_retention").args()[3], gate)
        self.assertEqual(self.sink("cleanup_retention").args()[:3], (CONN, MEDIA_DIR, 30))

    def test_a_failing_channel_keeps_its_historical_records_out_of_the_gate(self):
        self.sink("sync_telegram_channel_pipeline").raises = [None, TimeoutError("page fetch timed out")]

        self.assertFalse(self.sync())

        self.assertEqual(self.sink("cleanup_retention").args()[3], {"telegram:chan-one"})
        row = self.rows()["telegram:chan-two"]
        self.assertEqual(row["success"], False)
        self.assertEqual(row["error"], "Sync failed (TimeoutError).")
        self.assertEqual(row["detail"], "safe detail for page fetch timed out")
        self.assertTrue(self.core.logger.has(
            "Telegram sync failed for chan-two: safe detail for page fetch timed out", "error"))
        self.assertTrue(self.core.logger.has(
            "Sync completed with source failures: telegram:chan-two", "error"))

    def test_each_channel_reports_its_own_duration(self):
        self.core.time.step = 0.1234

        self.sync()

        self.assertEqual(self.rows()["telegram:chan-one"]["duration_seconds"], 0.123)
        self.assertEqual(self.rows()["telegram:chan-two"]["duration_seconds"], 0.123)

    def test_a_channel_filter_reaches_only_that_channel(self):
        self.sync(source_filter="telegram:chan-two")

        self.assertEqual(self.scanned_channels(), ["chan-two"])
        self.assertEqual(sorted(self.rows()), ["telegram:chan-two"])

    def test_a_bare_channel_id_is_understood_as_a_channel_filter(self):
        self.sync(source_filter="chan-two")

        self.assertEqual(self.scanned_channels(), ["chan-two"])
        self.assertEqual(sorted(self.rows()), ["telegram:chan-two"])

    def test_the_provider_filter_keeps_every_channel_and_only_telegram_rows(self):
        self.config.sources["pctv"] = SourceConfig(provider="pctv")

        self.assertTrue(self.sync(source_filter="telegram"))

        self.assertEqual(self.scanned_channels(), ["chan-one", "chan-two"])
        self.assertEqual(sorted(self.rows()), ["telegram:chan-one", "telegram:chan-two"])

    def test_an_unrelated_filter_skips_telegram_without_touching_the_gate(self):
        self.assertFalse(self.sync(source_filter="chan-missing"))

        self.assertEqual(self.scanned_channels(), [])
        self.assertEqual(self.sink("cleanup_retention").args()[3], set())
        self.assertEqual(self.history()["sources"], [
            {"source": "chan-missing", "label": "chan-missing", "success": False, "error": None,
             "added": 0, "detail": None, "removed": 0, "videos": 0, "duration_seconds": None}])

    def test_the_channel_scans_can_be_skipped_for_a_maintenance_only_pass(self):
        self.assertTrue(self.sync(skip_telegram_channels=True))

        self.assertEqual(self.scanned_channels(), [])
        self.assertTrue(self.core.logger.has(
            "Skipping Telegram channel scans by request; continuing full maintenance pass.", "info"))
        self.assertEqual(self.history()["sources"], [])

    def test_a_disabled_telegram_source_scans_and_reports_nothing(self):
        self.config.sources["telegram"].enabled = False
        self.use_core(config=self.config, sync_telegram=False)

        self.assertTrue(self.sync())

        self.assertEqual(self.scanned_channels(), [])
        self.assertEqual(self.history()["sources"], [])

    def test_the_global_switch_stops_the_scans_but_the_channels_stay_expected(self):
        self.use_core(config=self.config, sync_telegram=False)

        self.assertFalse(self.sync())

        self.assertEqual(self.scanned_channels(), [])
        self.assertEqual([row["source"] for row in self.history()["sources"]],
                         ["telegram:chan-one", "telegram:chan-two"])

    def test_a_source_without_channels_expects_no_telegram_row(self):
        self.config.sources["telegram"].channels = []

        self.assertTrue(self.sync())

        self.assertEqual(self.history()["sources"], [])


class TestTelegramAvailabilityReconciliation(SyncPassHarness):
    def test_the_reconciler_runs_only_for_a_scheduled_sync(self):
        self.sync(sync_mode="backfill")

        self.assertFalse(self.sink("reconcile_telegram_channel").called)

    def test_the_reconciler_is_handed_only_its_own_channels_videos(self):
        self.sync()

        reconciler = self.sink("reconcile_telegram_channel")
        self.assertEqual(reconciler.call_count, 2)
        first = reconciler.args()
        self.assertEqual(first[:5], (CONN, "chan-one", MEDIA_DIR,
                                     DATA_DIR / "telegram-unavailable-quarantine",
                                     self.core.RESOLVER_API_URL))
        self.assertIs(first[5], self.sink("fetch_json_with_retry"))
        self.assertIs(first[6], self.core.logger)
        self.assertEqual(reconciler.kwargs(0)["current_video_ids"], {"telegram:chan-one:1"})
        self.assertEqual(reconciler.kwargs(1)["current_video_ids"], {"telegram:chan-two:1"})
        self.assertEqual(reconciler.kwargs(0)["now"], 1_060)

    def test_the_availability_limit_comes_from_the_environment(self):
        with patch.dict(os.environ, {"TELEGRAM_AVAILABILITY_CHECKS_PER_RUN": "7"}):
            self.sync()

        self.assertEqual(self.sink("reconcile_telegram_channel").kwargs()["limit"], 7)

    def test_the_availability_limit_defaults_to_twenty_checks_per_run(self):
        with patch.dict(os.environ, {}):
            os.environ.pop("TELEGRAM_AVAILABILITY_CHECKS_PER_RUN", None)
            self.sync()

        self.assertEqual(self.sink("reconcile_telegram_channel").kwargs()["limit"], 20)

    def test_a_failing_reconciler_does_not_sink_the_channel(self):
        self.explode("reconcile_telegram_channel", OSError("jellyfin refused"))

        self.assertTrue(self.sync())

        self.assertEqual(self.sink("cleanup_retention").args()[3],
                         {"telegram:chan-one", "telegram:chan-two"})
        for channel in ("chan-one", "chan-two"):
            self.assertTrue(self.core.logger.has(
                f"Telegram availability reconciliation failed for {channel}: jellyfin refused", "warning"))


class TestEnvironmentTelegramChannels(SyncPassHarness):
    """Without a managed config the pass walks TELEGRAM_CHANNELS with a minimal channel shape."""

    def setUp(self):
        super().setUp()
        self.use_core(config=None, telegram_channels=["chan-one", " ", "chan-two"],
                      telegram_max_pages=2)

    def test_the_environment_channels_are_scanned_with_a_minimal_shape(self):
        self.sync()

        scanned = self.sink("sync_telegram_channel_pipeline")
        self.assertEqual(self.scanned_channels(), ["chan-one", "chan-two"])
        channel = scanned.args(1)[1]
        self.assertEqual((channel.studio, channel.min_duration_seconds), ("chan-two", 420))
        self.assertEqual(channel.max_pages, self.core.TELEGRAM_MAX_PAGES)
        self.assertEqual(channel.history_batch_size, 100)
        self.assertTrue(self.core.logger.has(
            f"Syncing Telegram ({len(self.core.TELEGRAM_CHANNELS)} channel(s), "
            f"{self.core.TELEGRAM_MAX_PAGES} page(s))...", "info"))

    def test_the_environment_channels_report_their_own_outcome(self):
        self.sink("sync_telegram_channel_pipeline").raises = [ConnectionError("no session"), None]

        # Nothing is "expected" without a managed config, so the pass reports
        # success and the failed channel only shows up in the log: it never
        # reaches the destructive passes, but it is not counted as a failure.
        self.assertTrue(self.sync())

        self.assertEqual([row["source"] for row in self.history()["sources"]], ["telegram:chan-two"])
        self.assertEqual(self.sink("cleanup_orphaned_records").args()[2], {"telegram:chan-two"})
        self.assertTrue(self.core.logger.has(
            "Telegram sync failed for chan-one: safe detail for no session", "error"))

    def test_a_channel_filter_needs_a_managed_config_to_be_understood(self):
        # Without config.yaml a channel id cannot be resolved, so the filter
        # silently stops the Telegram scans - yet "telegram:<id>" still counts
        # as an expected source, and the pass reports it as a failure.
        self.assertFalse(self.sync(source_filter="telegram:chan-two"))

        self.assertEqual(self.scanned_channels(), [])
        self.assertEqual(self.rows()["telegram:chan-two"]["success"], False)

    def test_the_provider_filter_scans_every_environment_channel(self):
        self.assertTrue(self.sync(source_filter="telegram"))

        self.assertEqual(self.scanned_channels(), ["chan-one", "chan-two"])
        self.assertEqual([row["source"] for row in self.history()["sources"]],
                         ["telegram:chan-one", "telegram:chan-two"])
        self.assertEqual([row["success"] for row in self.history()["sources"]], [True, True])

    def test_the_availability_reconciler_is_not_used_without_a_managed_config(self):
        self.sync()

        self.assertFalse(self.sink("reconcile_telegram_channel").called)


class TestEnrichmentStages(SyncPassHarness):
    def test_every_enrichment_stage_is_timed_and_limited(self):
        self.sync()

        self.assertEqual([stage for stage, _, _ in self.core.stages], [
            "metadata_scenes", "performer_title_backfill", "metadata_pbc", "metadata_tpdb",
            "metadata_stashdb", "jellyfin_metadata_reconciliation",
        ])
        self.assertEqual([failed for _, _, failed in self.core.stages], [False] * 6)
        self.assertEqual(self.sink("backfill_scenes").args(), (CONN, MEDIA_DIR))
        self.assertEqual(self.sink("backfill_scenes").kwargs(), {"limit": self.core.SCENE_BACKFILL_PER_RUN})
        self.assertEqual(self.sink("backfill_performers_from_titles").args(), (CONN, MEDIA_DIR))
        self.assertEqual(self.sink("backfill_pbc").kwargs(), {"limit": self.core.PBC_BACKFILL_PER_RUN})
        self.assertEqual(self.sink("backfill_tpdb").kwargs(),
                         {"limit": self.core.TPDB_BACKFILL_PER_RUN})
        # The StashDB pass is limited by the PornDB knob, not its own.
        self.assertEqual(self.sink("backfill_stashdb").kwargs(),
                         {"limit": self.core.TPDB_BACKFILL_PER_RUN})
        self.assertEqual(self.sink("sync_jellyfin_people").args(),
                         (CONN, self.core.JELLYFIN_URL, self.core.JELLYFIN_API_KEY))
        self.assertEqual(self.sink("sync_jellyfin_people").kwargs(),
                         {"limit": self.core.JELLYFIN_PEOPLE_PER_RUN})

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
        for module in ("scene_scrapers", "pbc", "theporndb", "stashdb"):
            self.absent(module)

        self.assertTrue(self.sync())

        self.assertEqual([stage for stage, _, _ in self.core.stages],
                         ["performer_title_backfill", "jellyfin_metadata_reconciliation"])
        self.assertEqual(self.core.logger.text("warning"), [])

    def test_a_failing_enrichment_pass_is_only_a_warning(self):
        expectations = {
            "backfill_scenes": "Scene fingerprint backfill pass failed: boom",
            "backfill_pbc": "PBC backfill pass failed: boom",
            "backfill_tpdb": "ThePornDB backfill pass failed: boom",
            "backfill_stashdb": "StashDB backfill pass failed: boom",
            "sync_jellyfin_people": "Jellyfin person metadata pass failed: boom",
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

    def test_the_jellyfin_library_refresh_and_title_pass_run_once_each(self):
        self.sync()

        self.assertEqual(self.sink("trigger_jellyfin_refresh").call_count, 1)
        self.assertEqual(self.sink("sync_jellyfin_titles").call_count, 1)
        order = self.core.order
        self.assertLess(order.index("sync_jellyfin_telegram_metadata"), order.index("trigger_jellyfin_refresh"))
        self.assertLess(order.index("trigger_jellyfin_refresh"), order.index("sync_jellyfin_titles"))


class TestJellyfinRefreshPoll(SyncPassHarness):
    def test_the_refresh_poll_is_published_from_the_reconciliation_result(self):
        self.sink("sync_jellyfin_telegram_metadata").result = {
            "refresh_poll": {"queued": 4, "reason": "nfo-changed"}}

        self.sync()

        self.assertEqual(self.history()["jellyfin_refresh_poll"], {"queued": 4, "reason": "nfo-changed"})
        self.assertEqual(self.core._ACTIVE_SYNC_METRICS["jellyfin_refresh_poll"],
                         {"queued": 4, "reason": "nfo-changed"})

    def test_a_reconciliation_result_without_a_poll_publishes_an_empty_one(self):
        self.sink("sync_jellyfin_telegram_metadata").result = {"refresh_poll": None}

        self.sync()

        self.assertEqual(self.history()["jellyfin_refresh_poll"], {})

    def test_a_non_dict_reconciliation_result_publishes_no_poll(self):
        self.sink("sync_jellyfin_telegram_metadata").result = ["legacy", "summary"]

        self.sync()

        self.assertEqual(self.history()["jellyfin_refresh_poll"], {})
        self.assertNotIn("jellyfin_refresh_poll", self.core._ACTIVE_SYNC_METRICS)

    def test_a_failing_reconciliation_publishes_no_poll(self):
        self.explode("sync_jellyfin_telegram_metadata", OSError("jellyfin refused"))

        self.assertTrue(self.sync())

        self.assertEqual(self.history()["jellyfin_refresh_poll"], {})
        self.assertTrue(self.core.logger.has(
            "Telegram metadata reconciliation failed: jellyfin refused", "warning"))


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


if __name__ == "__main__":
    unittest.main()
