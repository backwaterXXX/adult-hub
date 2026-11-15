"""Unit tests for sync runner worker capabilities and durable background jobs."""

import json
import threading
import time
import unittest

try:
    from tests.test_sync_runner import FakeProcess, RunnerHarness
except ImportError:
    from test_sync_runner import FakeProcess, RunnerHarness


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


class TestJobsWorker(RunnerHarness):
    def test_the_jobs_only_child_is_started_and_its_exit_reaches_the_caller(self):
        module = self.load()
        child = FakeProcess(returncode=0, alive_polls=2)
        popen = self.children(child)

        self.assertEqual(module.run_jobs_worker(), 0)

        self.assertEqual(popen.argv()[1:], ["-m", "sync.sync", "--jobs-only"])
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


if __name__ == "__main__":
    unittest.main()
