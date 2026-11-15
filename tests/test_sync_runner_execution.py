"""Unit tests for sync runner full sync passes, child process control, and status logging."""

import contextlib
import json
from pathlib import Path
import signal
import subprocess
import unittest
from unittest.mock import patch

try:
    from tests.test_sync_runner import FakeProcess, RunnerHarness
except ImportError:
    from test_sync_runner import FakeProcess, RunnerHarness


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

        self.assertEqual(popen.argv()[1:], ["-m", "sync.sync", "--source", "Chan", "--sync-mode", "backfill"])
        self.assertTrue(self.logs.has("Executing sync for Chan...", "info"))

    def test_a_scheduled_sync_needs_no_mode_argument(self):
        module = self.load()
        popen = self.children(FakeProcess())
        self.record("update_sync_status")

        module.run_full_sync("Chan", "scheduled")

        self.assertEqual(popen.argv()[1:], ["-m", "sync.sync", "--source", "Chan"])

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


if __name__ == "__main__":
    unittest.main()
