"""The sync daemon's own unit surface: what one runner instance does with its config, files and children."""

import contextlib
import datetime
import json
import importlib.util
import os
import signal
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
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
             backoff_base=30, backoff_max=900, shutdown_grace=180, config_error=None, temp_files=True,
             init_config=True):
        """Execute the daemon with the given config and hand it a recorded logger.

        Like the daemon entry point, this calls ``_init_config()`` after import unless
        ``init_config=False``. ``temp_files=False`` leaves the file paths the environment
        asked for, so a test can read the ones the module body picked up.
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
        if init_config:
            module._init_config()
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
        module = self.load(interval_minutes=25, backoff_base=45, backoff_max=1_200)
        module.log_startup_banner()

        self.assertTrue(self.logs.has("starting periodic sync runner (every 25 min", "info"))
        self.assertTrue(self.logs.has("retry base 45s, max 1200s", "info"))
        self.assertFalse(self.logs.has("scheduled daily sync", "info"))

    def test_the_startup_log_announces_the_daily_slot_instead(self):
        module = self.load(daily_at="03:30", backoff_base=5, backoff_max=60)
        module.log_startup_banner()

        self.assertTrue(self.logs.has("scheduled daily sync at 03:30 (retry base 5s, max 60s)", "info"))
        self.assertFalse(self.logs.has("periodic sync runner", "info"))

    def test_a_fatal_config_error_stops_the_daemon_before_it_logs_anything_else(self):
        with self.assertRaises(SystemExit) as stop:
            self.load(config_error=ConfigError("sources: at least one is required"))

        self.assertEqual(stop.exception.code, 1)
        self.assertEqual(self.logs.text("critical"),
                         ["[sync-daemon] FATAL CONFIG ERROR: sources: at least one is required"])

    def test_importing_the_module_neither_reads_config_nor_exits(self):
        # H-11: the entry point, not import, owns config loading and sys.exit.
        module = self.load(config_error=ConfigError("sources: at least one is required"),
                           init_config=False)
        self.assertEqual(module.interval, 900)
        self.assertEqual(self.logs.text("critical"), [])

    def test_an_unexpected_config_failure_stops_the_daemon_with_its_own_message(self):
        with self.assertRaises(SystemExit) as stop:
            self.load(config_error=OSError("config file vanished"))

        self.assertEqual(stop.exception.code, 1)
        self.assertTrue(self.logs.has("FATAL: config load failed unexpectedly: config file vanished", "critical"))

    def test_init_config_is_a_named_callable_not_bare_module_code(self):
        # H-11: sys.exit must live inside _init_config(), not at the bare module
        # level, so the module is importable without side-effects in test code.
        module = self.load()
        self.assertTrue(callable(module._init_config),
                        "_init_config must be a callable function (not removed or inlined)")
        # Defaults must be set even before _init_config is called explicitly.
        self.assertIsInstance(module.interval, int)
        self.assertIsInstance(module.BASE_BACKOFF_SECONDS, int)
        self.assertIsInstance(module.MAX_BACKOFF_SECONDS, int)
        self.assertIsInstance(module.SHUTDOWN_GRACE_SECONDS, int)

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
        module.install_signal_handlers()

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

if __name__ == "__main__":
    unittest.main()
