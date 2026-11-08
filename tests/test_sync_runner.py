"""The sync daemon's own unit surface: what one runner instance does with its config, files and children."""

import contextlib
import datetime
import json
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNNER_PATH = ROOT / "sync" / "runner.py"

from common.config_loader import ConfigError

ORIGINAL_HANDLERS = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}


class Call:
    """One recorded invocation."""

    def __init__(self, args, kwargs):
        self.args = args
        self.kwargs = kwargs

    def __repr__(self):
        return f"Call(args={self.args!r}, kwargs={self.kwargs!r})"


class Recorder:
    """A recorded stand-in for one collaborator.

    ``results``/``raises`` are consumed one entry per call and the last entry
    keeps repeating, so a scenario can fail only the second claim, say.
    """

    def __init__(self, name, result=None, results=None, raises=None, side_effect=None):
        self.name = name
        self.result = result
        self.results = list(results) if results is not None else None
        self.raises = None if raises is None else (list(raises) if isinstance(raises, list) else [raises])
        self.side_effect = side_effect
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(Call(args, kwargs))
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

    def every_args(self):
        return [call.args for call in self.calls]

    def every_kwargs(self):
        return [call.kwargs for call in self.calls]


class LogCapture:
    """What the daemon logged, collected before its first line is written."""

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

    def count(self, needle, level=None):
        return sum(needle in message for message in self.text(level))


class FakeClock:
    """The ``time`` the runner reads: a counter a test can advance explicitly."""

    def __init__(self, start=1_000.0, step=1.0):
        self.now = start
        self.step = step
        self.slept = []

    def advance(self, seconds):
        self.now += seconds

    def monotonic(self):
        value = self.now
        self.now += self.step
        return value

    def time(self):
        return int(self.now)

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def strftime(self, fmt):
        return "frozen-moment"


class FakeProcess:
    """A sync child the runner polls, terminates and reaps."""

    def __init__(self, returncode=0, alive_polls=0, terminate_error=None, wait_raises=()):
        self.final_returncode = returncode
        self.returncode = None
        self._alive_polls = alive_polls
        self.terminate_error = terminate_error
        self.wait_raises = list(wait_raises)
        self.terminated = 0
        self.killed = 0
        self.wait_calls = []

    def poll(self):
        if self._alive_polls > 0:
            self._alive_polls -= 1
            return None
        self.returncode = self.final_returncode
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if self.terminate_error is not None:
            raise self.terminate_error

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_raises:
            raise self.wait_raises.pop(0)
        self.returncode = self.final_returncode
        return self.final_returncode

    def kill(self):
        self.killed += 1


class PopenDouble:
    """What ``subprocess.Popen`` returns: children pass through, exceptions are raised."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(Call(argv, kwargs))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def argv(self, index=0):
        return list(self.calls[index].args)


class FakeStop:
    """A stop flag whose ``wait`` advances the recorded clock the way a real sleep would."""

    def __init__(self, clock):
        self.clock = clock
        self._set = False
        self.waits = []

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self._set:
            return True
        if timeout:
            self.clock.advance(timeout)
        return False


class RunnerHarness(unittest.TestCase):
    """One freshly executed copy of ``sync/runner.py`` per test, with its files in a temp dir."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

    def load(self, *, daily_at=None, interval_minutes=15, run_on_startup=True,
             backoff_base=30, backoff_max=900, shutdown_grace=180, config_error=None, temp_files=True):
        """Execute the daemon with the given config and hand it a recorded logger.

        ``temp_files=False`` leaves the file paths the environment asked for, so
        a test can read the ones the module body picked up.
        """
        sync = types.SimpleNamespace(
            interval_minutes=interval_minutes, daily_at=daily_at, run_on_startup=run_on_startup,
            retry_backoff_base_seconds=backoff_base, retry_backoff_max_seconds=backoff_max,
            shutdown_grace_seconds=shutdown_grace)
        self.logs = LogCapture()
        loader = Recorder("load_config", raises=[config_error]) if config_error else lambda: self.config(sync)
        self.stack.enter_context(patch("common.config_loader.load_config", loader))
        self.stack.enter_context(patch("common.logging_setup.configure_logging", lambda name: self.logs))
        spec = importlib.util.spec_from_file_location("sync_runner_under_test", RUNNER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.stack.enter_context(self.restore_signals())
        self.module = module
        if temp_files:
            self.use_temp_files()
        return module

    def use_temp_files(self):
        """Point every file the daemon writes at a directory of its own."""
        temp = tempfile.TemporaryDirectory(prefix="adult_hub_runner_unit_")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        module = self.module
        module.TRIGGER_FILE = str(root / "sync.trigger")
        module.STATUS_FILE = str(root / "sync_status.json")
        module.SYNC_HISTORY_FILE = str(root / "sync_history.json")
        module.WORKER_CAPABILITIES_FILE = str(root / "worker-capabilities.json")
        # Nothing in this suite talks to a database: every queue call is recorded.
        module.DB_URL = "postgresql://runner.invalid/adulthub"
        module.ensure_system_schedules = lambda *_args, **_kwargs: None
        module.claim_due_system_schedule = lambda *_args, **_kwargs: False
        module.finish_system_schedule = lambda *_args, **_kwargs: None
        module.list_system_schedules = lambda *_args: [{"id": "library-sync", "enabled": True, "next_run_at": "2026-01-01T03:30:00+00:00", "timezone": "Europe/Warsaw"}]
        module.dispatch_due_schedules = lambda *_args, **_kwargs: 0
        module.dispatch_due_system_schedules = lambda *_args, **_kwargs: 0
        module.time = FakeClock()
        module.stop_event.clear()
        return module

    @staticmethod
    def config(sync):
        return types.SimpleNamespace(sync=sync)

    @contextlib.contextmanager
    def restore_signals(self):
        try:
            yield
        finally:
            for sig, handler in ORIGINAL_HANDLERS.items():
                signal.signal(sig, handler)

    def record(self, name, **kwargs):
        """Swap one module-level collaborator for a recorder."""
        recorder = Recorder(name, **kwargs)
        setattr(self.module, name, recorder)
        return recorder

    def idle_queue(self, actions=None):
        """The durable queue answers with a fixed action set and nothing else."""
        self.module.pending_actions = None if actions is None else Recorder(
            "pending_actions", result=set(actions))

    def fake_stop(self):
        """Replace the stop flag so the wait advances the recorded clock instead of sleeping."""
        stop = FakeStop(self.module.time)
        self.module.stop_event = stop
        return stop

    def children(self, *outcomes):
        """Hand this runner copy its own ``subprocess`` shim.

        The stdlib module is shared with every other test in the process, so
        patching ``Popen`` on it would outlive the test that asked for it.
        """
        popen = PopenDouble(*outcomes)
        self.module.subprocess = types.SimpleNamespace(
            Popen=popen, TimeoutExpired=subprocess.TimeoutExpired)
        return popen

    def trigger(self, payload):
        """Write a wake file: a dict becomes JSON, anything else goes in raw."""
        path = Path(self.module.TRIGGER_FILE)
        text = json.dumps(payload) if isinstance(payload, (dict, list)) else payload
        path.write_text(text, encoding="utf-8")
        return path

    def history(self, *runs):
        Path(self.module.SYNC_HISTORY_FILE).write_text(
            json.dumps({"runs": list(runs)}), encoding="utf-8")

    def read_status(self):
        return json.loads(Path(self.module.STATUS_FILE).read_text(encoding="utf-8"))


class TestStartupConfiguration(RunnerHarness):
    def test_the_configured_interval_becomes_the_sleep_window(self):
        module = self.load(interval_minutes=25, backoff_base=45, backoff_max=1_200)

        self.assertEqual(module.interval, 1_500)
        self.assertIsNone(module.daily_at)
        self.assertTrue(module.run_on_startup)
        self.assertEqual((module.BASE_BACKOFF_SECONDS, module.MAX_BACKOFF_SECONDS), (45, 1_200))

    def test_the_configured_drain_window_is_what_a_stopped_child_gets(self):
        module = self.load(shutdown_grace=45)

        self.assertEqual(module.SHUTDOWN_GRACE_SECONDS, 45)

    def test_the_startup_log_announces_the_interval(self):
        self.load(interval_minutes=25, backoff_base=45, backoff_max=1_200)

        self.assertTrue(self.logs.has("starting periodic sync runner (every 25 min", "info"))
        self.assertTrue(self.logs.has("retry base 45s, max 1200s", "info"))
        self.assertFalse(self.logs.has("scheduled daily sync", "info"))

    def test_the_startup_log_announces_the_daily_slot_instead(self):
        self.load(daily_at="03:30", backoff_base=5, backoff_max=60)

        self.assertTrue(self.logs.has("scheduled daily sync at 03:30 (retry base 5s, max 60s)", "info"))
        self.assertFalse(self.logs.has("periodic sync runner", "info"))

    def test_a_fatal_config_error_stops_the_daemon_before_it_logs_anything_else(self):
        with self.assertRaises(SystemExit) as stop:
            self.load(config_error=ConfigError("sources: at least one is required"))

        self.assertEqual(stop.exception.code, 1)
        self.assertEqual(self.logs.text("critical"),
                         ["[sync-daemon] FATAL CONFIG ERROR: sources: at least one is required"])

    def test_an_unexpected_config_failure_stops_the_daemon_with_its_own_message(self):
        with self.assertRaises(SystemExit) as stop:
            self.load(config_error=OSError("config file vanished"))

        self.assertEqual(stop.exception.code, 1)
        self.assertTrue(self.logs.has("FATAL: config load failed unexpectedly: config file vanished", "critical"))

    def test_the_trigger_and_state_files_follow_the_environment(self):
        with contextlib.ExitStack() as stack:
            for name, value in (("SYNC_TRIGGER_FILE", "/tmp/run/trigger"),
                                ("SYNC_STATUS_FILE", "/tmp/run/status.json"),
                                ("ADMIN_WORKER_CAPABILITIES_PATH", "/tmp/run/capabilities.json"),
                                ("SYNC_HISTORY_FILE", "/tmp/run/history.json")):
                stack.enter_context(patch.dict(os.environ, {name: value}, clear=False))
            module = self.load(temp_files=False)

        self.assertEqual(module.TRIGGER_FILE, "/tmp/run/trigger")
        self.assertEqual(module.STATUS_FILE, "/tmp/run/status.json")
        self.assertEqual(module.WORKER_CAPABILITIES_FILE, "/tmp/run/capabilities.json")
        self.assertEqual(module.SYNC_HISTORY_FILE, "/tmp/run/history.json")


class TestShutdownSignal(RunnerHarness):
    def test_both_shutdown_signals_are_wired_to_the_handler(self):
        module = self.load()

        self.assertIs(signal.getsignal(signal.SIGTERM), module.signal_handler)
        self.assertIs(signal.getsignal(signal.SIGINT), module.signal_handler)

    def test_a_signal_stops_the_wait_and_the_loop(self):
        module = self.load()

        module.signal_handler(signal.SIGTERM, None)

        self.assertTrue(module.stop_event.is_set())
        self.assertTrue(self.logs.has("[sync-daemon] Received shutdown signal, stopping...", "info"))

    def test_a_signal_terminates_the_child_that_is_still_running(self):
        module = self.load()
        child = FakeProcess(alive_polls=1)
        module.current_process = child

        module.signal_handler(signal.SIGTERM, None)

        self.assertEqual(child.terminated, 1)

    def test_a_child_that_already_exited_is_not_signalled_twice(self):
        module = self.load()
        child = FakeProcess()
        child.poll()
        module.current_process = child

        module.signal_handler(signal.SIGTERM, None)

        self.assertEqual(child.terminated, 0)

    def test_a_child_that_vanishes_before_the_signal_is_forgiven(self):
        module = self.load()
        module.current_process = FakeProcess(alive_polls=1, terminate_error=ProcessLookupError())

        module.signal_handler(signal.SIGTERM, None)

        self.assertTrue(module.stop_event.is_set())


class TestSleepMaths(RunnerHarness):
    def freeze_now(self, moment):
        """Pin the wall clock the sleep maths read, without touching timedelta."""

        class FrozenDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return moment

        return patch("datetime.datetime", FrozenDatetime)

    def test_no_daily_slot_sleeps_the_plain_interval(self):
        self.load()

        self.assertEqual(self.module.calculate_next_sleep_seconds(None, 900), 900.0)

    def test_the_next_sleep_is_measured_to_the_slot_that_has_not_happened_today(self):
        self.load()
        moment = datetime.datetime(2026, 3, 14, 9, 0, 0)

        with self.freeze_now(moment):
            slept = self.module.calculate_next_sleep_seconds("11:30", 900)

        self.assertEqual(slept, 2.5 * 3600)

    def test_a_slot_that_already_happened_rolls_over_to_tomorrow(self):
        self.load()
        moment = datetime.datetime(2026, 3, 14, 23, 59, 0)

        with self.freeze_now(moment):
            slept = self.module.calculate_next_sleep_seconds("03:30", 900)

        self.assertEqual(slept, 3.5 * 3600 + 60)

    def test_a_slot_that_passed_this_minute_rolls_over_exactly(self):
        self.load()
        moment = datetime.datetime(2026, 3, 14, 3, 30, 30)

        with self.freeze_now(moment):
            slept = self.module.calculate_next_sleep_seconds("03:30", 900)

        self.assertEqual(slept, 24 * 3600 - 30)

    def test_a_slot_reached_inside_the_current_minute_still_sleeps_a_second(self):
        self.load()
        moment = datetime.datetime(2026, 3, 14, 3, 29, 59, 500_000)

        with self.freeze_now(moment):
            slept = self.module.calculate_next_sleep_seconds("03:30", 900)

        self.assertEqual(slept, 1.0)

    def test_the_duration_is_spelled_in_the_units_it_needs(self):
        self.load()
        format_duration = self.module.format_duration

        self.assertEqual(format_duration(45), "45s")
        self.assertEqual(format_duration(65), "1m 5s")
        self.assertEqual(format_duration(3_665), "1h 1m 5s")
        self.assertEqual(format_duration(7_200), "2h 0m 0s")
        self.assertEqual(format_duration(0), "0s")

    def test_the_duration_is_rounded_before_it_is_split(self):
        self.load()

        self.assertEqual(self.module.format_duration(59.6), "1m 0s")
        self.assertEqual(self.module.format_duration(0.4), "0s")


class TestFailedSyncSources(RunnerHarness):
    def failing_run(self, **overrides):
        run = {"started_at": 1_000, "source_filter": None, "sources": []}
        run.update(overrides)
        return run

    def failing_source(self, **overrides):
        source = {"source": "pctv", "label": "pctv", "success": False,
                  "error": "Sync failed (OSError)."}
        source.update(overrides)
        return source

    def test_the_failures_of_the_run_that_just_finished_are_relayed(self):
        self.load()
        self.history(self.failing_run(sources=[self.failing_source(),
                                                self.failing_source(source="tg", label="tg:[One]")]))

        self.assertEqual(self.module._failed_sync_sources(1_000, 1_060, None),
                         [{"label": "pctv", "error": "Sync failed (OSError)."},
                          {"label": "tg:[One]", "error": "Sync failed (OSError)."}])

    def test_only_the_runs_inside_the_window_and_behind_the_same_filter_count(self):
        self.load()
        self.history(self.failing_run(started_at=997, source_filter=None, sources=[self.failing_source()]),
                     self.failing_run(started_at=1_061, sources=[self.failing_source(label="too late")]),
                     self.failing_run(started_at=1_010, source_filter="Chan",
                                       sources=[self.failing_source(label="other filter")]),
                     self.failing_run(started_at="1_020", sources=[self.failing_source(label="not a clock")]),
                     self.failing_run(started_at=True, sources=[self.failing_source(label="not even a number")]),
                     self.failing_run(started_at=1_020, sources=[self.failing_source(label="the newest")]))

        failures = self.module._failed_sync_sources(1_000, 1_060, None)

        self.assertEqual([item["label"] for item in failures], ["the newest"])

    def test_a_run_without_failures_reports_an_empty_list(self):
        self.load()
        self.history(self.failing_run(sources=[]))

        self.assertEqual(self.module._failed_sync_sources(1_000, 1_060, None), [])

    def test_a_history_file_that_is_missing_or_nonsense_is_not_the_runners_problem(self):
        self.load()

        self.assertEqual(self.module._failed_sync_sources(1_000, 1_060, None), [])

        Path(self.module.SYNC_HISTORY_FILE).write_text("{not json", encoding="utf-8")
        self.assertEqual(self.module._failed_sync_sources(1_000, 1_060, None), [])

        Path(self.module.SYNC_HISTORY_FILE).write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(self.module._failed_sync_sources(1_000, 1_060, None), [])

        Path(self.module.SYNC_HISTORY_FILE).write_text(
            json.dumps({"runs": ["not a run", None]}), encoding="utf-8")
        self.assertEqual(self.module._failed_sync_sources(1_000, 1_060, None), [])

    def test_a_source_that_succeeded_or_stayed_silent_is_not_reported(self):
        self.load()
        self.history(self.failing_run(sources=[
            self.failing_source(label="fine", success=True),
            self.failing_source(label="unknown", success=None),
            {"label": "no signal at all"},
            self.failing_source(label="reported"),
        ]))

        failures = self.module._failed_sync_sources(1_000, 1_060, None)

        self.assertEqual([item["label"] for item in failures], ["reported"])

    def test_a_row_without_a_usable_label_is_dropped(self):
        self.load()
        self.history(self.failing_run(sources=[
            self.failing_source(label=None),
            self.failing_source(label=7),
            self.failing_source(label="x" * 257),
            "not a row at all",
            self.failing_source(label="kept"),
        ]))

        failures = self.module._failed_sync_sources(1_000, 1_060, None)

        self.assertEqual([item["label"] for item in failures], ["kept"])

    def test_only_the_four_known_failure_messages_are_passed_on(self):
        self.load()
        self.history(self.failing_run(sources=[
            self.failing_source(error="Sync failed (RuntimeError).", label="a"),
            self.failing_source(error="Sync failed (TimeoutError).", label="b"),
            self.failing_source(error="Sync failed (ValueError).", label="c"),
            self.failing_source(error="Sync failed (OSError).", label="d"),
            self.failing_source(error="Sync failed (KeyError).", label="e"),
            self.failing_source(error=None, label="f"),
            self.failing_source(error=42, label="g"),
        ]))

        self.assertEqual(self.module._failed_sync_sources(1_000, 1_060, None), [
            {"label": "a", "error": "Sync failed (RuntimeError)."},
            {"label": "b", "error": "Sync failed (TimeoutError)."},
            {"label": "c", "error": "Sync failed (ValueError)."},
            {"label": "d", "error": "Sync failed (OSError)."},
            {"label": "e"},
            {"label": "f"},
            {"label": "g"},
        ])

    def test_a_telegram_runtime_failure_carries_the_detail_the_pipeline_recorded(self):
        self.load()
        self.history(self.failing_run(sources=[
            self.failing_source(source="telegram:chan-one", label="tg:[One]",
                                error="Sync failed (RuntimeError).",
                                detail="Flood wait 30s on get_messages"),
        ]))

        self.assertEqual(self.module._failed_sync_sources(1_000, 1_060, None), [{
            "label": "tg:[One]", "error": "Sync failed (RuntimeError).",
            "detail": "Flood wait 30s on get_messages"}])

    def test_a_telegram_failure_without_a_usable_detail_gets_a_generic_one(self):
        self.load()
        runtime = self.failing_source(error="Sync failed (RuntimeError).")
        self.history(self.failing_run(sources=[
            dict(runtime, source="telegram:a", label="empty", detail=""),
            dict(runtime, source="telegram:b", label="too long", detail="y" * 701),
            dict(runtime, source="telegram:c", label="not text", detail=7),
            dict(runtime, source="telegram:d", label="absent"),
        ]))

        failures = self.module._failed_sync_sources(1_000, 1_060, None)

        self.assertEqual([item["detail"] for item in failures],
                         ["Telegram fetch/history operation failed."] * 4)

    def test_a_non_telegram_failure_never_carries_a_detail(self):
        self.load()
        self.history(self.failing_run(sources=[
            self.failing_source(source="tg-pctv", error="Sync failed (RuntimeError).", detail="secret trace"),
        ]))

        self.assertEqual(self.module._failed_sync_sources(1_000, 1_060, None),
                         [{"label": "pctv", "error": "Sync failed (RuntimeError)."}])

    def test_at_most_a_hundred_failing_sources_are_relayed(self):
        self.load()
        self.history(self.failing_run(sources=[
            self.failing_source(label=f"source-{index}") for index in range(105)]))

        failures = self.module._failed_sync_sources(1_000, 1_060, None)

        self.assertEqual(len(failures), 100)
        self.assertEqual(failures[-1]["label"], "source-99")


class TestWorkerCapabilities(RunnerHarness):
    def test_a_deployment_without_capability_publishing_is_left_alone(self):
        module = self.load()
        module.publish_worker_capabilities = None

        module.publish_worker_heartbeat()
        module.stop_event.set()
        module.worker_heartbeat_loop()

        self.assertEqual(self.logs.text("warning"), [])

    def test_the_capabilities_are_published_with_the_whole_action_list(self):
        module = self.load()
        module.ALL_ACTIONS = ("sync", "availability_check")
        published = self.record("publish_worker_capabilities")

        module.publish_worker_heartbeat(running=False)

        self.assertEqual(published.args(), (module.WORKER_CAPABILITIES_FILE,))
        self.assertEqual(published.kwargs(), {"version": module.APP_VERSION,
                                              "actions": ("sync", "availability_check"),
                                              "running": False})

    def test_a_capability_publish_that_throws_is_only_a_warning(self):
        module = self.load()
        self.record("publish_worker_capabilities", raises=[OSError("cache is full")])

        module.publish_worker_heartbeat()

        self.assertTrue(self.logs.has("Could not publish worker capabilities: cache is full", "warning"))

    def test_the_heartbeat_thread_publishes_until_the_daemon_stops(self):
        module = self.load()
        published = self.record("publish_worker_capabilities",
                                side_effect=lambda *_args, **_kwargs: module.stop_event.set())

        module.worker_heartbeat_loop()

        self.assertEqual(published.call_count, 1)


class TestStatusFile(RunnerHarness):
    def test_a_running_sync_is_marked_with_its_source_and_start(self):
        module = self.load()
        module.time.now = 2_000.0

        module.update_sync_status(running=True, source="telegram:chan-one")

        status = self.read_status()
        self.assertEqual(status["running"], True)
        self.assertEqual(status["started_at"], 2_000)
        self.assertEqual(status["active_source"], "telegram:chan-one")
        self.assertEqual(status["updated_at"], 2_000)
        self.assertEqual(status["updated_human"], "frozen-moment")
        self.assertNotIn("last_exit_code", status)

    def test_a_finished_sync_keeps_only_its_result(self):
        module = self.load()

        module.update_sync_status(running=False, exit_code=3, success=False)

        status = self.read_status()
        self.assertEqual((status["running"], status["last_exit_code"], status["last_success"]),
                         (False, 3, False))
        self.assertNotIn("started_at", status)
        self.assertNotIn("active_source", status)

    def test_a_publish_that_cannot_clean_up_after_itself_still_reports_the_failure(self):
        module = self.load()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("os.replace", side_effect=OSError("disk vanished")))
            stack.enter_context(patch("os.unlink", side_effect=OSError("the temporary went too")))
            module.update_sync_status(running=True)

        self.assertEqual(self.logs.count("Could not update status file"), 1)
        self.assertFalse(Path(module.STATUS_FILE).exists())

    def test_the_status_directory_is_created_when_it_is_missing(self):
        module = self.load()
        module.STATUS_FILE = str(Path(module.STATUS_FILE).parent / "nested" / "deeper" / "status.json")

        module.update_sync_status(running=False, exit_code=0, success=True)

        self.assertTrue(Path(module.STATUS_FILE).is_file())

    def test_the_write_reaches_the_world_even_if_it_cannot_be_loosened(self):
        module = self.load()
        with patch("os.chmod", side_effect=OSError("read-only filesystem")):
            module.update_sync_status(running=True)

        self.assertTrue(self.read_status()["running"])
        self.assertEqual(self.logs.text("warning"), [])

    def test_a_publish_that_fails_midway_leaves_no_temporary_file_behind(self):
        module = self.load()
        with patch("os.replace", side_effect=OSError("disk vanished")):
            module.update_sync_status(running=True)

        self.assertFalse(Path(module.STATUS_FILE).exists())
        self.assertEqual(list(Path(module.STATUS_FILE).parent.glob(".*.tmp")), [])
        self.assertTrue(self.logs.has("Could not update status file: disk vanished", "warning"))

    def test_a_status_path_that_cannot_be_created_is_only_a_warning(self):
        module = self.load()
        blocker = Path(module.STATUS_FILE).parent / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        module.STATUS_FILE = str(blocker / "status.json")

        module.update_sync_status(running=False, exit_code=0, success=True)

        self.assertTrue(self.logs.has("Could not update status file", "warning"))
        self.assertFalse(Path(module.STATUS_FILE).exists())


class TestChildShutdown(RunnerHarness):
    def test_a_child_that_is_already_gone_is_only_reaped(self):
        module = self.load()
        child = FakeProcess(returncode=7)

        self.assertEqual(module._stop_child(child), 7)

        self.assertEqual(child.terminated, 0)
        self.assertEqual(child.wait_calls, [None])

    def test_a_child_that_vanished_during_the_terminate_is_reaped(self):
        module = self.load()
        child = FakeProcess(alive_polls=1, terminate_error=ProcessLookupError())

        self.assertEqual(module._stop_child(child), 0)

        self.assertEqual(child.killed, 0)
        self.assertEqual(child.wait_calls, [None])

    def test_a_child_that_ignores_the_terminate_is_killed_after_the_grace_period(self):
        module = self.load()
        timeout = subprocess.TimeoutExpired(cmd="sync.py", timeout=1.5)
        child = FakeProcess(alive_polls=1, wait_raises=[timeout])

        self.assertEqual(module._stop_child(child, grace_seconds=1.5), 0)

        self.assertEqual(child.terminated, 1)
        self.assertEqual(child.killed, 1)
        self.assertEqual(child.wait_calls, [1.5, None])

    def test_a_draining_child_is_given_the_configured_window_before_being_forced(self):
        module = self.load(shutdown_grace=45)
        timeout = subprocess.TimeoutExpired(cmd="sync.py", timeout=45)
        child = FakeProcess(alive_polls=1, wait_raises=[timeout])

        module._stop_child(child)

        self.assertEqual(child.wait_calls, [45, None],
                         "the child needs the whole drain window to reach its next channel boundary")

    def test_a_child_that_stops_after_the_terminate_needs_no_kill(self):
        module = self.load()
        child = FakeProcess(returncode=2, alive_polls=1)

        self.assertEqual(module._stop_child(child, grace_seconds=1.5), 2)

        self.assertEqual(child.killed, 0)
        self.assertEqual(child.wait_calls, [1.5])


class TestJobsWorker(RunnerHarness):
    def test_the_jobs_only_child_is_started_and_its_exit_reaches_the_caller(self):
        module = self.load()
        child = FakeProcess(returncode=0, alive_polls=2)
        popen = self.children(child)

        self.assertEqual(module.run_jobs_worker(), 0)

        self.assertEqual(popen.argv()[1:], ["sync.py", "--jobs-only"])
        self.assertEqual(module.time.slept, [0.2, 0.2])
        self.assertIsNone(module.current_process)
        self.assertEqual(self.logs.text("warning"), [])

    def test_a_jobs_worker_that_fails_loudly_is_reported(self):
        module = self.load()
        self.children(FakeProcess(returncode=2))

        self.assertEqual(module.run_jobs_worker(), 2)

        self.assertTrue(self.logs.has("Jobs worker exited with code 2", "warning"))

    def test_a_jobs_worker_that_cannot_be_started_is_reported_as_minus_one(self):
        module = self.load()
        self.children(OSError("no python here"))

        self.assertEqual(module.run_jobs_worker(), -1)

        self.assertTrue(self.logs.has("Jobs worker execution error: no python here", "warning"))
        self.assertIsNone(module.current_process)

    def test_a_shutdown_asked_mid_flight_stops_the_child_and_counts_as_success(self):
        module = self.load()
        child = FakeProcess(returncode=0, alive_polls=3)
        self.children(child)
        stopped = self.record("_stop_child", result=0)
        module.stop_event.set()

        self.assertEqual(module.run_jobs_worker(), 0)

        self.assertEqual(stopped.args(), (child,))
        self.assertEqual(child.terminated, 0, "the shutdown helper, not the runner, stops the child")
        self.assertIsNone(module.current_process)


class TestFullSync(RunnerHarness):
    def test_a_clean_child_exit_is_a_successful_sync(self):
        module = self.load()
        self.children(FakeProcess(returncode=0, alive_polls=1))
        status = self.record("update_sync_status")

        self.assertEqual(module.run_full_sync(), (True, 0))

        self.assertEqual(status.every_kwargs(), [{"running": True, "source": None},
                                                 {"running": False, "exit_code": 0, "success": True}])
        self.assertIsNone(module.current_process)
        self.assertEqual(self.logs.text("warning"), [])

    def test_the_source_and_mode_reach_the_child_as_arguments(self):
        module = self.load()
        popen = self.children(FakeProcess())
        self.record("update_sync_status")

        module.run_full_sync("Chan", "backfill")

        self.assertEqual(popen.argv()[1:], ["sync.py", "--source", "Chan", "--sync-mode", "backfill"])
        self.assertTrue(self.logs.has("Executing sync for Chan...", "info"))

    def test_a_scheduled_sync_needs_no_mode_argument(self):
        module = self.load()
        popen = self.children(FakeProcess())
        self.record("update_sync_status")

        module.run_full_sync("Chan", "scheduled")

        self.assertEqual(popen.argv()[1:], ["sync.py", "--source", "Chan"])

    def test_a_failing_child_is_warned_about_while_the_failures_are_still_new(self):
        module = self.load()
        self.children(FakeProcess(returncode=4, alive_polls=1))
        self.record("update_sync_status")

        self.assertEqual(module.run_full_sync(), (False, 4))

        self.assertTrue(self.logs.has("Sync exited with code 4", "warning"))

    def test_a_child_that_keeps_failing_is_no_longer_warned_about(self):
        module = self.load()
        module.consecutive_errors = 3
        self.children(FakeProcess(returncode=4, alive_polls=1))
        self.record("update_sync_status")

        self.assertEqual(module.run_full_sync(), (False, 4))

        self.assertEqual(self.logs.text("warning"), [])

    def test_a_child_that_cannot_be_spawned_fails_with_the_reserved_exit_code(self):
        module = self.load()
        self.children(OSError("sync.py is gone"))
        self.record("update_sync_status")

        self.assertEqual(module.run_full_sync(), (False, -1))

        self.assertTrue(self.logs.has("Sync execution error: sync.py is gone", "warning"))

    def test_a_persistent_spawn_failure_is_no_longer_warned_about(self):
        module = self.load()
        module.consecutive_errors = 5
        self.children(OSError("sync.py is gone"))
        self.record("update_sync_status")

        self.assertEqual(module.run_full_sync(), (False, -1))

        self.assertEqual(self.logs.text("warning"), [])

    def test_a_shutdown_during_the_sync_stops_the_child_and_reports_no_success(self):
        module = self.load(shutdown_grace=10)
        child = FakeProcess(returncode=1, alive_polls=2)
        self.children(child)
        status = self.record("update_sync_status")
        module.stop_event.set()

        self.assertEqual(module.run_full_sync(), (False, 1))

        self.assertEqual(child.terminated, 1)
        self.assertEqual(child.wait_calls, [10, None])
        self.assertEqual(status.kwargs(1), {"running": False, "exit_code": 1, "success": False})
        self.assertTrue(self.logs.has("Sync child stopped during shutdown (exit 1).", "info"))
        self.assertEqual(self.logs.text("warning"), [], "a pass cut short is not reported as a crash")

    def test_a_child_that_needs_to_be_forced_is_reported_as_not_drained(self):
        module = self.load(shutdown_grace=10)
        self.children(FakeProcess(returncode=-signal.SIGKILL, alive_polls=2))
        self.record("update_sync_status")
        module.stop_event.set()

        self.assertEqual(module.run_full_sync(), (False, -signal.SIGKILL))

        self.assertTrue(self.logs.has(
            "Sync child was killed by signal 9 instead of reaching its shutdown checkpoint within 10s.",
            "warning"))

    def test_a_child_terminated_by_someone_else_is_reported_as_not_drained(self):
        module = self.load(shutdown_grace=10)
        self.children(FakeProcess(returncode=-signal.SIGTERM, alive_polls=2))
        self.record("update_sync_status")
        module.stop_event.set()

        module.run_full_sync()

        self.assertTrue(self.logs.has(
            "Sync child was killed by signal 15 instead of reaching its shutdown checkpoint",
            "warning"))

    def test_a_child_that_drained_to_a_clean_exit_is_a_successful_sync(self):
        module = self.load()
        self.children(FakeProcess(returncode=0, alive_polls=2))
        status = self.record("update_sync_status")
        module.stop_event.set()

        self.assertEqual(module.run_full_sync(), (True, 0))

        self.assertEqual(status.kwargs(1), {"running": False, "exit_code": 0, "success": True})

    def test_a_child_still_alive_in_the_cleanup_is_asked_twice_to_stop(self):
        module = self.load()
        child = FakeProcess(returncode=0, alive_polls=99)
        self.children(child)
        self.record("update_sync_status")
        stopped = self.record("_stop_child", result=0)
        module.stop_event.set()

        self.assertEqual(module.run_full_sync(), (False, -1))

        self.assertEqual(stopped.call_count, 2, "once from the poll loop, once from the cleanup")
        self.assertEqual(child.terminated, 0)

    def test_a_child_that_cannot_be_reaped_is_reported_but_not_fatal(self):
        module = self.load()
        self.children(FakeProcess(returncode=0, wait_raises=[OSError("pid vanished")]))
        self.record("update_sync_status")

        self.assertEqual(module.run_full_sync(), (True, 0))

        self.assertTrue(self.logs.has("Could not reap sync process: pid vanished", "warning"))
        self.assertIsNone(module.current_process)


class TestWakeSignal(RunnerHarness):
    def test_a_missing_wake_file_is_nothing_to_do(self):
        module = self.load()

        module._clear_sync_trigger()

        self.assertFalse(Path(module.TRIGGER_FILE).exists())

    def test_a_satisfied_sync_wake_is_consumed(self):
        module = self.load()

        self.trigger({"action": "sync"})
        module._clear_sync_trigger()

        self.assertFalse(Path(module.TRIGGER_FILE).exists())

    def test_a_wake_for_anything_else_is_left_for_its_owner(self):
        module = self.load()
        for payload in ({"action": "jobs"}, {"action": "consistency_audit"}, {}, "[1]", "garbage"):
            with self.subTest(payload=payload):
                path = self.trigger(payload)
                module._clear_sync_trigger()
                self.assertTrue(path.exists())


class TestDurableSyncJobs(RunnerHarness):
    def job(self, payload=None, **overrides):
        job = {"id": "job-one", "payload": json.dumps(payload) if payload is not None else "{}",
               "lease_token": "lease-1"}
        job.update(overrides)
        return job

    def test_a_deployment_without_a_job_queue_claims_nothing(self):
        module = self.load()
        module.claim_job = None

        self.assertFalse(module.run_pending_sync_jobs())

        module.claim_job = lambda *_a: self.job()
        module.finish_job = None
        self.assertFalse(module.run_pending_sync_jobs())

    def test_a_claim_that_throws_is_only_a_warning(self):
        module = self.load()
        self.record("claim_job", raises=[OSError("queue is locked")])

        self.assertFalse(module.run_pending_sync_jobs())

        self.assertTrue(self.logs.has("Could not claim sync job: queue is locked", "warning"))

    def test_an_empty_queue_returns_without_touching_the_child(self):
        module = self.load()
        self.record("claim_job", result=None)
        full_sync = self.record("run_full_sync")

        self.assertFalse(module.run_pending_sync_jobs())

        self.assertFalse(full_sync.called)

    def test_the_claimed_request_becomes_the_child_arguments_and_the_result(self):
        module = self.load()
        self.record("claim_job", result=self.job({"source": "Chan", "sync_mode": "backfill"}))
        full_sync = self.record("run_full_sync", results=[(False, 2)])
        finished = self.record("finish_job", result=True)
        cleared = self.record("_clear_sync_trigger")

        self.assertTrue(module.run_pending_sync_jobs())

        self.assertEqual(full_sync.args(), ("Chan", "backfill"))
        self.assertEqual(finished.args()[:3], (module.DB_URL, "job-one", "failed"))
        self.assertEqual(finished.args()[3], {
            "source": "Chan", "sync_mode": "backfill", "local_state": "sync failed", "exit_code": 2,
            "message": "On-demand sync for Chan failed."})
        self.assertTrue(cleared.called)
        self.assertTrue(self.logs.has("Running durable sync job job-one for Chan...", "info"))

    def test_a_request_without_a_payload_runs_the_plain_scheduled_sync(self):
        module = self.load()
        self.record("claim_job", results=[self.job(), self.job(payload='"a string"'),
                                          self.job({"sync_mode": "nonsense"})])
        full_sync = self.record("run_full_sync", result=(True, 0))
        self.record("finish_job", result=True)
        self.record("_clear_sync_trigger")

        for _ in range(3):
            self.assertTrue(module.run_pending_sync_jobs())

        self.assertEqual(full_sync.every_args(), [(None, "scheduled")] * 3)
        self.assertEqual(self.logs.count("Running durable sync job job-one..."), 3)

    def test_a_payload_the_queue_never_serialised_is_read_as_a_bare_request(self):
        module = self.load()
        self.record("claim_job", results=[{"id": "job-one", "payload": "not json"},
                                          {"id": "job-one", "payload": {"source": "Chan"}}])
        full_sync = self.record("run_full_sync", result=(True, 0))
        self.record("finish_job", result=True)
        self.record("_clear_sync_trigger")

        for _ in range(2):
            self.assertTrue(module.run_pending_sync_jobs())

        self.assertEqual(full_sync.every_args(), [(None, "scheduled")] * 2)

    def test_a_failed_sync_relays_the_per_source_failures_of_that_run(self):
        module = self.load()
        self.record("claim_job", result=self.job({"source": "Chan"}))
        self.record("run_full_sync", result=(False, 1))
        finished = self.record("finish_job", result=True)
        self.record("_clear_sync_trigger")
        self.history({"started_at": module.time.time(), "source_filter": "Chan",
                      "sources": [{"source": "telegram:c", "label": "tg:[C]", "success": False,
                                   "error": "Sync failed (OSError)."}]})

        module.run_pending_sync_jobs()

        self.assertEqual(finished.args()[3]["source_failures"],
                         [{"label": "tg:[C]", "error": "Sync failed (OSError)."}])

    def test_a_failed_sync_without_per_source_detail_says_no_more(self):
        module = self.load()
        self.record("claim_job", result=self.job())
        self.record("run_full_sync", result=(False, 1))
        finished = self.record("finish_job", result=True)
        self.record("_clear_sync_trigger")

        module.run_pending_sync_jobs()

        self.assertNotIn("source_failures", finished.args()[3])
        self.assertEqual(finished.args()[3]["message"], "On-demand sync failed.")

    def test_a_job_that_was_taken_over_while_running_is_reported(self):
        module = self.load()
        self.record("claim_job", result=self.job())
        self.record("run_full_sync", result=(True, 0))
        self.record("finish_job", result=False)
        self.record("_clear_sync_trigger")

        module.run_pending_sync_jobs()

        self.assertTrue(self.logs.has(
            "Sync job job-one was no longer pending when its result arrived.", "warning"))

    def test_a_result_that_cannot_be_recorded_is_only_a_warning(self):
        module = self.load()
        self.record("claim_job", result=self.job())
        self.record("run_full_sync", result=(True, 0))
        self.record("finish_job", raises=[OSError("queue is read only")])
        self.record("_clear_sync_trigger")

        self.assertTrue(module.run_pending_sync_jobs())

        self.assertTrue(self.logs.has("Could not record sync job result: queue is read only", "warning"))

    def test_the_running_job_keeps_its_heartbeat_alive(self):
        module = self.load()
        module.SYNC_JOB_HEARTBEAT_INTERVAL_SECONDS = 0.01
        beat = threading.Event()
        self.record("claim_job", result=self.job())
        self.record("heartbeat_job", side_effect=lambda *_a, **_k: beat.set())
        self.record("run_full_sync", result=(True, 0), side_effect=lambda *_a: beat.wait(2))
        finished = self.record("finish_job", result=True)
        self.record("_clear_sync_trigger")

        self.assertTrue(module.run_pending_sync_jobs())

        self.assertTrue(beat.is_set())
        self.assertTrue(finished.called)

    def test_a_heartbeat_that_throws_does_not_stop_the_sync(self):
        module = self.load()
        module.SYNC_JOB_HEARTBEAT_INTERVAL_SECONDS = 0.01
        self.record("claim_job", result=self.job())
        self.record("heartbeat_job", raises=[OSError("job row vanished")] * 4)
        self.record("run_full_sync", result=(False, 1), side_effect=lambda *_a: time.sleep(0.05))
        self.record("finish_job", result=True)
        self.record("_clear_sync_trigger")

        self.assertTrue(module.run_pending_sync_jobs())

        self.assertTrue(self.logs.has(
            "Could not update sync-job heartbeat (OSError).", "warning"))

    def test_a_shutdown_let_the_child_reach_its_checkpoint_leaves_the_request_behind(self):
        module = self.load()
        self.record("claim_job", result=self.job())
        self.record("run_full_sync", results=[(False, 1)],
                    side_effect=lambda *_a: module.stop_event.set())
        finished = self.record("finish_job", result=True)
        cleared = self.record("_clear_sync_trigger")

        self.assertTrue(module.run_pending_sync_jobs())

        self.assertFalse(finished.called,
                         "the requester must not see another process's shutdown as its own failure")
        self.assertTrue(self.logs.has(
            "Leaving sync job job-one for the next start to requeue", "info"))
        self.assertTrue(cleared.called, "the wake signal is consumed either way")


class TestMissedDailySlot(RunnerHarness):
    """What the sync history says about a start that landed past the configured slot."""

    def ago(self, seconds):
        return self.module.time.time() - int(seconds)

    def entry(self, *, started_at, mode="scheduled", source=None, success=True):
        return {"started_at": started_at, "sync_mode": mode, "source_filter": source, "success": success}

    def test_the_newest_completed_whole_library_pass_is_the_one_that_counts(self):
        module = self.load(daily_at="03:30")
        self.history(self.entry(started_at=self.ago(200_000)),
                     self.entry(started_at=self.ago(93_600)),
                     self.entry(started_at=self.ago(72_000), mode="backfill"),
                     self.entry(started_at=self.ago(36_000), source="Chan"),
                     self.entry(started_at=self.ago(7_200), success=False),
                     {"started_at": "long ago", "sync_mode": "scheduled", "success": True})

        self.assertEqual(module._latest_scheduled_sync_start(), self.ago(93_600))

    def test_a_history_the_daemon_cannot_read_is_no_history(self):
        module = self.load(daily_at="03:30")

        self.assertIsNone(module._latest_scheduled_sync_start(), "no history file at all")

        Path(module.SYNC_HISTORY_FILE).write_text("{ truncated", encoding="utf-8")
        self.assertIsNone(module._latest_scheduled_sync_start())

        self.history({"runs": "not a list"})
        self.assertIsNone(module._latest_scheduled_sync_start())

    def test_a_restart_that_missed_the_slot_is_owed_that_slot(self):
        # The 03:30 pass ran yesterday; this start waits 19h40m for tomorrow's.
        module = self.load(daily_at="03:30")
        self.history(self.entry(started_at=self.ago(102_000)))

        self.assertTrue(module._scheduled_sync_was_missed(70_800.0))

    def test_a_restart_after_the_day_s_pass_waits_for_the_next_slot(self):
        module = self.load(daily_at="03:30")
        self.history(self.entry(started_at=self.ago(15_600)))

        self.assertFalse(module._scheduled_sync_was_missed(70_800.0))

    def test_a_first_start_with_no_history_honours_run_on_startup(self):
        module = self.load(daily_at="03:30")

        self.assertFalse(module._scheduled_sync_was_missed(70_800.0))

    def test_a_daemon_on_an_interval_has_no_slot_to_miss(self):
        module = self.load()
        self.history(self.entry(started_at=self.ago(102_000)))

        self.assertFalse(module._scheduled_sync_was_missed(900.0))


class TestWaitingForWork(RunnerHarness):
    """The periodic wait: what wakes the daemon early, and what it refuses to repeat."""

    def idle_wait(self, *, dispatch=None, queue_actions=None):
        module = self.load()
        module.dispatch_due_schedules = dispatch
        self.idle_queue(queue_actions)
        self.stop = self.fake_stop()
        module.time.step = 0.05
        self.worker = self.record("run_jobs_worker", result=0)
        self.sync_jobs = self.record("run_pending_sync_jobs", result=False)
        self.full_sync = self.record("run_full_sync", result=(True, 0))
        self.logs.entries.clear()  # the startup announcement has its own suite
        return module

    def test_an_idle_wait_only_ages_the_clock(self):
        module = self.idle_wait(dispatch=None, queue_actions=[])

        self.assertFalse(module.wait_with_trigger(2))

        self.assertEqual(self.worker.call_count, 0)
        self.assertEqual(self.sync_jobs.call_count, 0)
        self.assertEqual(self.full_sync.call_count, 0)
        self.assertTrue(all(0 < delay <= 1 for delay in self.stop.waits))
        self.assertLessEqual(sum(self.stop.waits), 2)
        self.assertEqual(self.logs.text(), [])

    def test_a_shutdown_ends_the_wait_before_the_first_look_at_the_queue(self):
        module = self.idle_wait(queue_actions=[])
        self.stop.set()

        self.assertFalse(module.wait_with_trigger(60))

        self.assertEqual(self.module.pending_actions.call_count, 0)

    def test_due_admin_work_is_handed_to_the_jobs_worker(self):
        dispatch = Recorder("dispatch_due_schedules", result=1)
        module = self.idle_wait(dispatch=dispatch, queue_actions=[])

        self.assertFalse(module.wait_with_trigger(8))

        self.assertEqual(dispatch.args(), (module.DB_URL,))
        self.assertEqual(dispatch.call_count, 1, "the next dispatch is thirty seconds away")
        self.assertEqual(self.worker.call_count, 1)

    def test_a_dispatch_that_throws_is_said_once_a_minute(self):
        dispatch = Recorder("dispatch_due_schedules", raises=OSError("Admin is unreachable"))
        module = self.idle_wait(dispatch=dispatch)

        self.assertFalse(module.wait_with_trigger(10))

        self.assertEqual(self.logs.count("Could not dispatch Admin schedules: Admin is unreachable"), 1)

    def test_a_queue_that_cannot_be_read_is_said_once_a_minute(self):
        module = self.idle_wait()
        module.pending_actions = Recorder("pending_actions", raises=OSError("the queue is locked"))

        self.assertFalse(module.wait_with_trigger(10))

        self.assertGreater(module.pending_actions.call_count, 1)
        self.assertEqual(self.logs.count("Could not inspect durable queue: the queue is locked"), 1)

    def test_a_durable_sync_request_is_claimed_before_the_wake_file_is_read(self):
        module = self.idle_wait(queue_actions=["sync"])
        self.sync_jobs.results = [True]
        self.sync_jobs.side_effect = lambda *_a: self.stop.set()
        path = self.trigger({"source": "Chan"})

        self.assertFalse(module.wait_with_trigger(20))

        self.assertEqual(self.sync_jobs.call_count, 1)
        self.assertTrue(path.exists(), "the wake file stays for the claim that follows")

    def test_a_claim_that_ran_dry_still_leaves_the_wake_file_to_be_read(self):
        module = self.idle_wait(queue_actions=["sync"])
        self.trigger({"source": "Chan"})

        self.assertTrue(module.wait_with_trigger(20))

        self.assertEqual(self.sync_jobs.call_count, 1)
        self.assertEqual(module.requested_source, "Chan")

    def test_a_worker_wake_is_not_able_to_respawn_the_child_within_five_seconds(self):
        module = self.idle_wait(queue_actions=["consistency_audit"])
        module.WORKER_ACTIONS = ("consistency_audit",)

        self.assertFalse(module.wait_with_trigger(4))

        self.assertEqual(self.worker.call_count, 1)
        self.assertEqual(self.sync_jobs.call_count, 0)

    def test_a_worker_wake_does_respawn_the_child_after_the_cooldown(self):
        module = self.idle_wait(queue_actions=["consistency_audit"])
        module.WORKER_ACTIONS = ("consistency_audit",)

        self.assertFalse(module.wait_with_trigger(8))

        self.assertEqual(self.worker.call_count, 2)

    def test_a_jobs_wake_starts_the_worker_and_is_consumed(self):
        module = self.idle_wait()
        path = self.trigger({"action": "jobs"})

        self.assertFalse(module.wait_with_trigger(20))

        self.assertEqual(self.worker.call_count, 1)
        self.assertFalse(path.exists())

    def test_a_sync_wake_is_only_a_note_that_a_request_is_waiting(self):
        module = self.idle_wait()
        path = self.trigger({"action": "sync"})

        self.assertFalse(module.wait_with_trigger(2))

        self.assertEqual(self.sync_jobs.call_count, 0)
        self.assertEqual(self.worker.call_count, 0)
        self.assertFalse(path.exists())

    def test_a_legacy_wake_starts_that_source_right_away(self):
        module = self.idle_wait()
        path = self.trigger({"source": "Chan", "sync_mode": "new"})

        self.assertTrue(module.wait_with_trigger(60))

        self.assertEqual((module.requested_source, module.requested_sync_mode), ("Chan", "new"))
        self.assertFalse(path.exists())
        self.assertTrue(self.logs.has(
            "On-demand sync trigger detected for Chan, executing sync immediately!", "info"))

    def test_an_unknown_mode_in_a_legacy_wake_falls_back_to_scheduled(self):
        module = self.idle_wait()
        self.trigger({"sync_mode": "whenever"})

        self.assertTrue(module.wait_with_trigger(60))

        self.assertEqual(module.requested_sync_mode, "scheduled")

    def test_a_wake_that_is_not_an_object_is_a_plain_start_now(self):
        module = self.idle_wait()
        self.trigger([1])

        self.assertTrue(module.wait_with_trigger(60))

        self.assertIsNone(module.requested_source)
        self.assertEqual(module.requested_sync_mode, "scheduled")
        self.assertTrue(self.logs.has("On-demand sync trigger detected, executing sync immediately!", "info"))

    def test_a_wake_that_cannot_be_parsed_is_a_plain_start_now(self):
        module = self.idle_wait()
        path = self.trigger("this is not json")

        self.assertTrue(module.wait_with_trigger(60))

        self.assertFalse(path.exists())
        self.assertIsNone(module.requested_source)

    def test_an_empty_wake_is_a_plain_start_now(self):
        module = self.idle_wait()
        path = self.trigger("")

        self.assertTrue(module.wait_with_trigger(60))

        self.assertFalse(path.exists())


class StopAfter:
    """End the loop once the daemon has waited for work this many times."""

    def __init__(self, stop, after):
        self.stop = stop
        self.after = after
        self.count = 0

    def __call__(self, *_args, **_kwargs):
        self.count += 1
        if self.count >= self.after:
            self.stop.set()


class TestRunLoop(RunnerHarness):
    """The daemon's own loop: recovery, drain, dispatch, sync, backoff."""

    def looping(self, *, requeue=None, stop_after=1, wake_results=None, **config):
        module = self.load(**config)
        module.requeue_processing = requeue
        self.stop = self.fake_stop()
        self.pending = self.record("run_pending_sync_jobs", result=False)
        self.dispatch = self.record("dispatch_due_schedules", result=0)
        self.full_sync = self.record("run_full_sync", result=(True, 0))
        self.sleep = self.record("calculate_next_sleep_seconds", result=3_600.0)
        self.wake = StopAfter(self.stop, stop_after)
        self.waited = self.record("wait_with_trigger", results=wake_results, side_effect=self.wake)
        self.logs.entries.clear()  # the startup announcement has its own suite
        return module

    def test_unavailable_system_schedule_refuses_to_start(self):
        module = self.looping()
        module.ensure_system_schedules = Recorder("ensure_system_schedules", raises=OSError("database unavailable"))
        with self.assertRaisesRegex(RuntimeError, "initialize system cron"):
            module.run_loop()
        self.assertFalse(self.full_sync.called)

    def test_the_daemon_reclaims_jobs_left_running_by_a_dead_worker(self):
        module = self.looping(requeue=Recorder("requeue_processing", result=2))

        module.run_loop()

        self.assertEqual(module.requeue_processing.args(), (module.DB_URL, "sync"))
        self.assertTrue(self.logs.has("Recovered 2 interrupted sync job(s).", "info"))

    def test_a_recovery_that_cannot_be_done_is_said_once_and_ignored(self):
        module = self.looping(requeue=Recorder("requeue_processing", raises=OSError("queue is locked")))

        module.run_loop()

        self.assertTrue(self.logs.has("Could not recover sync jobs: queue is locked", "warning"))
        self.assertTrue(self.logs.has("Sync runner exited.", "info"))

    def test_a_deployment_without_job_recovery_starts_the_same_loop(self):
        module = self.looping(requeue=None)

        module.run_loop()

        self.assertTrue(self.full_sync.called)

    def test_the_first_pass_syncs_and_then_waits_for_the_interval(self):
        module = self.looping()

        module.run_loop()

        self.assertEqual(self.full_sync.args(), (None, "scheduled"))
        self.assertEqual(self.dispatch.args(), (module.DB_URL,))
        self.assertEqual(self.waited.args(), (float("inf"),))
        self.assertTrue(self.logs.has("Next scheduled sync at", "info"))
        self.assertTrue(self.logs.has("Sync runner exited.", "info"))

    def test_startup_sync_claims_a_due_slot_instead_of_syncing_twice(self):
        module = self.looping()
        claim = self.record("claim_due_system_schedule", result=True)
        finish = self.record("finish_system_schedule")

        module.run_loop()

        self.assertEqual(claim.call_count, 1)
        self.assertEqual(self.full_sync.args(), (None, "scheduled"))
        self.assertEqual(finish.kwargs(), {"success": True, "interrupted": False})

    def test_a_failed_startup_claim_still_runs_the_startup_sync(self):
        module = self.looping()
        self.record("claim_due_system_schedule", raises=OSError("database busy"))
        finish = self.record("finish_system_schedule")

        module.run_loop()

        self.assertTrue(self.logs.has("Could not claim a due Library sync slot: database busy", "warning"))
        self.assertTrue(self.full_sync.called)
        self.assertFalse(finish.called)

    def test_due_admin_work_is_queued_before_the_daemon_sleeps(self):
        module = self.looping()
        self.dispatch.result = 3

        module.run_loop()

        self.assertTrue(self.logs.has("Queued 3 due Admin schedule(s) before sync.", "info"))

    def test_a_dispatch_that_throws_at_startup_does_not_stop_the_sync(self):
        module = self.looping()
        self.dispatch.raises = OSError("Admin is unreachable")

        module.run_loop()

        self.assertTrue(self.logs.has(
            "Could not dispatch Admin schedules at startup: Admin is unreachable", "warning"))
        self.assertTrue(self.full_sync.called)

    def test_a_durable_request_replaces_the_scheduled_sync_of_that_pass(self):
        module = self.looping()
        self.pending.results = [True, False]

        module.run_loop()

        self.assertFalse(self.full_sync.called, "the claimed request already synced")
        self.assertEqual(self.pending.call_count, 2)
        self.assertEqual(self.waited.call_count, 1)

    def test_a_shutdown_during_the_drain_ends_the_pass_before_the_dispatch(self):
        module = self.looping()
        self.pending.results = [True]
        self.pending.side_effect = lambda *_a: self.stop.set() if self.pending.call_count > 1 else None

        module.run_loop()

        self.assertEqual(self.pending.call_count, 2)
        self.assertFalse(self.dispatch.called)
        self.assertFalse(self.full_sync.called)
        self.assertTrue(self.logs.has("Sync runner exited.", "info"))

    def test_startup_can_be_skipped_in_favour_of_the_next_slot(self):
        module = self.looping(run_on_startup=False, stop_after=2)

        module.run_loop()

        self.assertTrue(self.logs.has("Skipping initial run on startup (run_on_startup=false)", "info"))
        self.assertEqual(self.waited.every_args(), [(float("inf"),), (float("inf"),)])
        self.assertEqual(self.full_sync.call_count, 1)

    def test_a_wake_during_the_startup_wait_gives_the_durable_queue_another_look(self):
        module = self.looping(run_on_startup=False, wake_results=[True, False], stop_after=2)
        self.pending.results = [False, True, False]

        module.run_loop()

        self.assertEqual(self.pending.call_count, 3, "the queue is drained again after the wake")
        self.assertEqual(self.full_sync.call_count, 0)
        self.assertEqual(self.dispatch.call_count, 2)

    def test_a_restart_that_missed_the_daily_slot_syncs_instead_of_waiting_a_day(self):
        module = self.looping(daily_at="03:30", run_on_startup=False, stop_after=2)
        def wake_due(*_args, **_kwargs):
            if self.waited.call_count == 1:
                module.system_sync_due = True
            else:
                self.stop.set()
        self.waited.side_effect = wake_due
        module.run_loop()
        self.assertEqual(self.full_sync.call_count, 1)
        self.assertEqual(self.full_sync.args(), (None, "scheduled"))

    def test_a_restart_after_the_daily_slot_ran_waits_for_the_next_one(self):
        module = self.looping(daily_at="03:30", run_on_startup=False)
        self.sleep.result = 70_800.0
        self.history({"started_at": module.time.time() - 15_600,
                      "sync_mode": "scheduled", "success": True})

        module.run_loop()

        self.assertTrue(self.logs.has("Skipping initial run on startup (run_on_startup=false)", "info"))
        self.assertFalse(self.full_sync.called)

    def test_a_pass_cut_short_by_the_previous_shutdown_does_not_count_as_the_slot(self):
        module = self.looping(daily_at="03:30", run_on_startup=False, stop_after=2)
        finish = self.record("finish_system_schedule")
        def wake_due(*_args, **_kwargs):
            if self.waited.call_count == 1:
                module.system_sync_due = True
            else:
                self.stop.set()
        self.waited.side_effect = wake_due
        module.run_loop()
        self.assertEqual(self.full_sync.call_count, 1)
        self.assertEqual(finish.kwargs(), {"success": True, "interrupted": False})

    def test_shutdown_during_scheduled_run_keeps_it_unfinished(self):
        module = self.looping()
        module.system_sync_due = True
        finish = self.record("finish_system_schedule")
        self.full_sync.side_effect = lambda *_args: self.stop.set()
        module.run_loop()
        self.assertEqual(finish.kwargs(), {"success": False, "interrupted": True})

    def test_a_daemon_that_has_never_finished_a_pass_waits_for_its_slot(self):
        module = self.looping(daily_at="03:30", run_on_startup=False)
        self.sleep.result = 70_800.0
        self.history({"started_at": module.time.time() - 15_600,
                      "sync_mode": "scheduled", "success": False})

        module.run_loop()

        self.assertFalse(self.full_sync.called,
                         "an unproven daemon is not started outside its slot by a failed history")

    def test_a_daily_slot_promises_the_next_run_instead_of_a_sleep_length(self):
        module = self.looping(daily_at="03:30")

        module.run_loop()

        self.assertTrue(self.logs.has("Next scheduled sync at", "info"))
        self.assertFalse(self.logs.has("Sleeping for", "info"))

    def test_a_failed_sync_waits_the_backoff_and_doubles_it(self):
        module = self.looping(stop_after=3)
        self.full_sync.result = (False, 1)

        module.run_loop()

        self.assertEqual(self.waited.every_args(), [(30,), (60,), (120,)])
        self.assertEqual(self.module.consecutive_errors, 3)
        self.assertTrue(self.logs.has(
            "Sync failed (attempt 1). Retrying with backoff in 30s...", "warning"))
        self.assertTrue(self.logs.has(
            "Sync failed (attempt 2). Retrying with backoff in 60s...", "warning"))

    def test_failed_scheduled_run_waits_backoff_before_reclaiming_due_slot(self):
        module = self.looping(stop_after=2)
        module.system_sync_due = True
        self.full_sync.result = (False, 1)
        finish = self.record("finish_system_schedule")
        module.run_loop()
        self.assertEqual(self.waited.every_args(), [(30,), (float("inf"),)])
        self.assertEqual(self.waited.kwargs(0), {"allow_schedule": False})
        self.assertEqual(finish.kwargs(), {"success": False, "interrupted": False})

    def test_the_backoff_stops_growing_at_the_configured_ceiling(self):
        module = self.looping(stop_after=4, backoff_base=30, backoff_max=90)
        self.full_sync.result = (False, 1)

        module.run_loop()

        self.assertEqual(self.waited.every_args(), [(30,), (60,), (90,), (90,)])

    def test_a_persistent_failure_stops_repeating_itself(self):
        module = self.looping(stop_after=1)
        self.module.consecutive_errors = 3
        self.full_sync.result = (False, 1)

        module.run_loop()

        self.assertTrue(self.logs.has(
            "Consecutive sync failures (4). Suppressed repetitive logs. Retrying with backoff in 240s...",
            "warning"))
        self.assertEqual(self.logs.count("Sync failed (attempt"), 0)

    def test_a_sync_that_recovers_says_how_many_attempts_it_took(self):
        module = self.looping()
        self.module.consecutive_errors = 2

        module.run_loop()

        self.assertTrue(self.logs.has("Sync succeeded after 2 failed attempts.", "info"))
        self.assertEqual(self.module.consecutive_errors, 0)

    def test_a_shutdown_during_the_startup_wait_ends_the_daemon_without_a_sync(self):
        module = self.looping(run_on_startup=False)

        module.run_loop()

        self.assertFalse(self.full_sync.called)
        self.assertEqual(self.waited.call_count, 1)
        self.assertTrue(self.logs.has("Sync runner exited.", "info"))

    def test_a_shutdown_during_the_sync_ends_the_daemon_before_it_promises_a_sleep(self):
        module = self.looping()
        self.full_sync.side_effect = lambda *_a: self.stop.set()

        module.run_loop()

        self.assertEqual(self.waited.call_count, 0)
        self.assertEqual(self.sleep.call_count, 0)
        self.assertFalse(self.logs.has("Sleeping for", "info"))
        self.assertTrue(self.logs.has("Sync runner exited.", "info"))

    def test_the_requested_source_is_used_once_and_then_forgotten(self):
        module = self.looping(stop_after=2)
        self.module.requested_source = "Chan"
        self.module.requested_sync_mode = "backfill"

        module.run_loop()

        self.assertEqual(self.full_sync.every_args(), [("Chan", "backfill"), (None, "scheduled")])


if __name__ == "__main__":
    unittest.main()
