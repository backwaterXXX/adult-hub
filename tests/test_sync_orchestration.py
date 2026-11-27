"""The scheduled sync pass: which sync.* calls sync_orchestration makes, in what order, and what it reports."""

import contextlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


from test_support import load_sync_module

import sync.sync_orchestration as sync_orchestration
sync_orchestration._core = load_sync_module()
from common.config_loader import AppConfig, SourceConfig, TelegramChannelConfig
from sync.sync_metrics import observe_sync_stage

MEDIA_DIR = Path("/srv/media")
DATA_DIR = Path("/srv/data")


class _Connection:
    """Stand-in for the hub connection; records whether the pass closed it."""

    closed = False

    def close(self):
        self.closed = True


CONN = _Connection()

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
    "republish_pending_videos",
    "safe_process_duplicate_jobs_checkpoint",
    "sync_telegram_channel_pipeline",
    "reconcile_telegram_channel",
    "fetch_json_with_retry",
    "hide_dead_streams",
    "expire_dead_streams",
    "flush_collected",
    "rewrite_all_stream_files",
    "backfill_performers_from_titles",
    "cleanup_orphaned_records",
    "cleanup_retention",
)

# Imported lazily inside the pass, so they are reached through sys.modules.
LAZY_FUNCTIONS = {
    "sync.performer_catalog_sync": "sync_catalog_if_due",
    "sync.fansdb_catalog_sync": "sync_fansdb_catalog_if_due",
    "sync.scene_scrapers": "backfill_scenes",
    "sync.pbc": "backfill_pbc",
    "sync.theporndb": "backfill_tpdb",
    "sync.stashdb": "backfill_stashdb",
    "sync.fansdb": "backfill_fansdb",
    "sync.telegram_title_cleanup": "cleanup_telegram_titles",
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
        outcome = self.side_effect(*args, **kwargs) if self.side_effect is not None else None
        error = self._take(self.raises)
        if error is not None:
            raise error if isinstance(error, BaseException) else error()
        if self.results is None and self.result is None:
            # No scripted value: report what the scenario actually produced, the
            # way the real dependency would.
            return outcome
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
        self.is_telegram_authorized = lambda: True
        self.HIDE_DEAD_STREAMS = hide_dead_streams
        self.TELEGRAM_CHANNELS = list(telegram_channels)
        self.TELEGRAM_MAX_PAGES = telegram_max_pages
        self.SCENE_BACKFILL_PER_RUN = 11
        self.PBC_BACKFILL_PER_RUN = 22
        self.TPDB_BACKFILL_PER_RUN = 33
        self.CONFIG = config
        # State the pass reads at each channel boundary, not a dependency it
        # calls for a result: a scenario flips it through shutdown_during().
        self.stop_requested = lambda: False
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
        self.sinks["sync_fansdb_catalog_if_due"].result = (8, 2)

    def _register(self, recorder):
        self.sinks[recorder.name] = recorder
        setattr(self, recorder.name, recorder)
        recorder.trace = self.order.append
        return recorder

    def collect_videos(self, channel_id, _channel, _conn, _media, collected, **_kwargs):
        video_id = f"telegram:{channel_id}:1"
        collected[video_id] = {}
        # The real pipeline persists and clears `collected` before returning, so a
        # double that leaves it populated would pass for the wrong reason.
        collected.clear()
        return {video_id}

    def collect_without_persisting(self, channel_id, _channel, _conn, _media, collected, **_kwargs):
        """Records a pipeline never flushed, left for the end-of-pass flush."""
        collected[f"telegram:{channel_id}:1"] = {}
        return set()

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
        # The catalog cache refresh talks to the database; a pass harness has none.
        self.stack.enter_context(
            patch.object(sync_orchestration, "_refresh_catalog_caches", lambda conn: None))
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

    def stop_after_scans(self, count: int) -> None:
        """Ask the process for a shutdown once ``count`` channels have been scanned."""
        state = {"scanned": 0}
        pipeline = self.sink("sync_telegram_channel_pipeline")
        collect = pipeline.side_effect

        def scan(*args, **kwargs):
            result = collect(*args, **kwargs)
            state["scanned"] += 1
            if state["scanned"] >= count:
                self.core.stop_requested = lambda: True
            return result

        pipeline.side_effect = scan



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

    def test_resumable_history_warning_is_recorded_and_excluded_from_cleanup(self):
        from sync.telegram_pipeline import TelegramSyncResult
        original = self.sink("sync_telegram_channel_pipeline").side_effect
        def scan(channel, *args, **kwargs):
            result = original(channel, *args, **kwargs)
            return TelegramSyncResult(result, ["history batch: TimeoutError: timed out"]) if channel == "chan-one" else result
        self.sink("sync_telegram_channel_pipeline").side_effect = scan
        self.assertTrue(self.sync())
        self.assertIn("history batch:", self.rows()["telegram:chan-one"]["warnings"][0])
        retention_sources = self.sink("cleanup_retention").args()[3]
        self.assertNotIn("telegram:chan-one", retention_sources)
        self.assertIn("telegram:chan-two", retention_sources)

    def test_the_history_records_the_window_the_pass_ran_in(self):
        self.core.time.wall_values = [1_000, 1_060]

        self.sync()

        report = self.history()
        self.assertEqual((report["started_at"], report["ended_at"]), (1000, 1060))
        self.assertEqual(report["duration_seconds"], 60)
        self.assertEqual(report["stages"]["metadata_scenes"]["calls"], 1)

    def test_the_connection_is_closed_when_the_pass_ends(self):
        CONN.closed = False

        self.sync()

        self.assertTrue(CONN.closed)

    def test_the_connection_is_closed_when_a_stage_raises(self):
        CONN.closed = False
        self.explode("_video_id_snapshot")

        with self.assertRaises(RuntimeError):
            self.sync()

        self.assertTrue(CONN.closed)

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
            "init_db", "sync_catalog_if_due", "sync_fansdb_catalog_if_due", "_video_id_snapshot", "migrate_synced_files",
            "migrate_categories", "migrate_redundant_performers",
            "cleanup_legacy_folders", "cleanup_old_transcodes", "republish_pending_videos",
            "safe_process_duplicate_jobs_checkpoint",
        ])
        self.assertEqual(self.sink("init_db").args(), ())
        self.assertEqual(self.sink("_video_id_snapshot").args(), (CONN,))
        for name in ("migrate_synced_files", "migrate_categories",
                     "migrate_redundant_performers", "republish_pending_videos"):
            self.assertEqual(self.sink(name).args(), (CONN, MEDIA_DIR), name)
        self.assertEqual(self.sink("cleanup_legacy_folders").args(), (MEDIA_DIR,))
        self.assertEqual(self.sink("cleanup_old_transcodes").args(), ())

    def test_the_catalogue_refresh_reports_a_successful_update(self):
        self.sync()

        self.assertEqual(self.sink("sync_catalog_if_due").args(), (CONN,))
        self.assertEqual(self.sink("sync_fansdb_catalog_if_due").args(), (CONN,))
        self.assertTrue(self.core.logger.has("Performer catalogue refreshed: 12 people, 3 aliases", "info"))
        self.assertTrue(self.core.logger.has("FansDB performer catalogue refreshed: 8 people, 2 aliases", "info"))

    def test_the_catalogue_refresh_is_silent_when_it_reports_nothing(self):
        self.sink("sync_catalog_if_due").result = ()
        self.sink("sync_fansdb_catalog_if_due").result = ()

        self.sync()

        self.assertFalse(self.core.logger.has("Performer catalogue refreshed", "info"))
        self.assertFalse(self.core.logger.has("FansDB performer catalogue refreshed", "info"))
        self.assertFalse(self.core.logger.has("catalogue refresh failed", "warning"))

    def test_a_failing_catalogue_refresh_keeps_the_last_local_copy(self):
        self.explode("sync_catalog_if_due", ValueError("stashdb unreachable"))
        self.explode("sync_fansdb_catalog_if_due", ValueError("fansdb unreachable"))

        self.assertTrue(self.sync())

        self.assertTrue(self.core.logger.has(
            "Performer catalogue refresh failed; using last local copy: stashdb unreachable", "warning"))
        self.assertTrue(self.core.logger.has(
            "FansDB performer catalogue refresh failed; using last local copy: fansdb unreachable", "warning"))

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
        self.assertTrue(self.sink("cleanup_retention").called, "the closing stages still run")

    def test_a_checkpoint_that_fails_every_time_still_finishes_the_pass(self):
        self.explode("safe_process_duplicate_jobs_checkpoint", OSError("ledger busy"))

        self.assertTrue(self.sync())

        self.assertEqual(self.sink("safe_process_duplicate_jobs_checkpoint").call_count, 7)
        self.assertEqual(
            self.core.logger.text("warning").count(
                "Could not process queued duplicate decisions: ledger busy"), 7)
        for name in ("rewrite_all_stream_files", "backfill_performers_from_titles",
                     "cleanup_orphaned_records", "cleanup_retention"):
            self.assertTrue(self.sink(name).called, name)

    def test_only_the_checkpoint_before_the_scans_has_no_in_memory_records(self):
        self.sync()

        checkpoint = self.sink("safe_process_duplicate_jobs_checkpoint")
        self.assertEqual(checkpoint.args(), (CONN, MEDIA_DIR, None))
        collected = self.sink("flush_collected").args()[0]
        for args in checkpoint.every_args()[1:]:
            self.assertIs(args[2], collected)

    def test_the_remaining_videos_are_flushed_once_after_the_scans(self):
        self.sink("sync_telegram_channel_pipeline").side_effect = self.core.collect_without_persisting
        self.sync()

        flush = self.sink("flush_collected")
        self.assertEqual(flush.call_count, 1)
        self.assertEqual(flush.args(), ({"telegram:chan-one:1": {}, "telegram:chan-two:1": {}},
                                        CONN, MEDIA_DIR, "remaining"))
        self.assertEqual(flush.kwargs(), {"raise_on_error": True},
                         "an unpublished entry must not count as a synced source")

    def test_the_end_of_pass_flush_failure_is_reported_without_aborting_upkeep(self):
        self.sink("sync_telegram_channel_pipeline").side_effect = self.core.collect_without_persisting
        self.explode("flush_collected", RuntimeError("library volume full"))

        self.assertTrue(self.sync())

        self.assertTrue(self.core.logger.has(
            "Final catalog flush failed: safe detail for library volume full", "error"))
        for name in ("cleanup_orphaned_records", "cleanup_retention"):
            self.assertTrue(self.sink(name).called, f"{name} still runs")

    def test_a_failed_publication_retry_keeps_the_pass_going(self):
        self.explode("republish_pending_videos", OSError("library volume busy"))

        self.assertTrue(self.sync())

        self.assertTrue(self.core.logger.has(
            "Could not retry pending library publications: library volume busy",
            "warning"))
        self.assertTrue(self.sink("sync_telegram_channel_pipeline").called, "the scans still run")


class TestTheCoreFacadeImport(unittest.TestCase):
    """The orchestration binds the sync facade from the sync package.

    ``sync_orchestration`` imports ``sync.sync`` as ``_core``. Loading its source
    against a stub ``sync`` package must hand back the stub's ``sync`` attribute.
    """

    SOURCE = Path(sync_orchestration.__file__)

    def load(self, name):
        spec = importlib.util.spec_from_file_location(name, self.SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_the_package_layout_reaches_the_facade_as_a_sibling_module(self):
        facade = types.ModuleType("sync.sync")
        package = types.ModuleType("sync")
        package.sync = facade

        with patch.dict(sys.modules, {"sync": package}):
            module = self.load("sync.sync_orchestration")

        self.assertIs(module._core, facade)



if __name__ == "__main__":
    unittest.main()
