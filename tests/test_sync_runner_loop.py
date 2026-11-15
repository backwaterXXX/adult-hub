"""Unit tests for the sync runner main loop, wake signals, and waiting logic."""

from pathlib import Path
import unittest

try:
    from tests.test_sync_runner import Recorder, RunnerHarness
except ImportError:
    from test_sync_runner import Recorder, RunnerHarness


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
