"""Leasing, transfer-budget and queue-loop behaviour of the resolver pHash worker."""

from __future__ import annotations

import base64
import contextlib
import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


import resolver.phash_worker as phash_worker
from common.worker_capabilities import read_worker_capabilities

JOB = {
    "id": "p-abc",
    "video_id": "telegram:-100:1:7",
    "duration_us": 120_000_000,
    "lease_token": "lease-token",
    "decoder_profile": "resolver-ffmpeg-v1",
    "submitted_by_user_id": "admin-1",
    "attempts": 1,
}
DB_URL = "postgresql://worker@host/adulthub_test"
SIXTY_FOUR = "a" * 64
LIMIT_ENV_KEYS = {
    "byte_budget": "PHASH_SERVER_BYTE_BUDGET",
    "timeout_seconds": "PHASH_SERVER_TIMEOUT_SECONDS",
    "bytes_per_second": "PHASH_SERVER_BYTES_PER_SECOND",
    "idle_bytes_per_second": "PHASH_SERVER_IDLE_BYTES_PER_SECOND",
}


@contextlib.contextmanager
def phash_env(**values):
    """Pin the transfer-limit environment, dropping anything a developer exports."""
    resolved = {LIMIT_ENV_KEYS[key]: str(value) for key, value in values.items()}
    saved = {env: os.environ.get(env) for env in LIMIT_ENV_KEYS.values()}
    for env in LIMIT_ENV_KEYS.values():
        os.environ.pop(env, None)
    os.environ.update(resolved)
    try:
        yield
    finally:
        for env, value in saved.items():
            if value is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = value


class WorkerPatch(unittest.TestCase):
    """Patch worker globals without ever leaving one set for the next test."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)

    def patch(self, name, replacement, target=phash_worker):
        self.stack.enter_context(patch.object(target, name, replacement))
        return replacement


class FakeProcess:
    def __init__(self, stdout="{}", returncode=0, error=None, poll_result=None):
        self.pid = 4_050_505
        self.stdout = stdout
        self.returncode = returncode
        self.error = error
        self.poll_result = poll_result
        self.payload = None
        self.timeout = None
        self.communications = 0

    def communicate(self, payload=None, timeout=None):
        self.communications += 1
        if self.error is not None and self.communications == 1:
            raise self.error
        self.payload = payload
        self.timeout = timeout
        return self.stdout, ""

    def poll(self):
        return self.poll_result


class TestStopSignalHandling(WorkerPatch):
    def setUp(self):
        super().setUp()
        self.stop_event = self.patch("_stop", threading.Event())

    def test_stop_terminates_the_process_group_of_a_running_child(self):
        process = self.patch("_active_process", FakeProcess(poll_result=None))
        with patch.object(os, "killpg") as killpg:
            phash_worker._handle_stop(signal.SIGTERM, None)

        self.assertTrue(self.stop_event.is_set())
        killpg.assert_called_once_with(process.pid, signal.SIGTERM)

    def test_stop_leaves_an_already_finished_child_alone(self):
        self.patch("_active_process", FakeProcess(poll_result=0))
        with patch.object(os, "killpg") as killpg:
            phash_worker._handle_stop(signal.SIGTERM, None)

        self.assertTrue(self.stop_event.is_set())
        killpg.assert_not_called()

    def test_stop_tolerates_a_process_group_that_vanished(self):
        self.patch("_active_process", FakeProcess(poll_result=None))
        with patch.object(os, "killpg", side_effect=ProcessLookupError):
            phash_worker._handle_stop(signal.SIGTERM, None)

        self.assertTrue(self.stop_event.is_set())

    def test_stop_without_an_active_child_only_sets_the_flag(self):
        self.patch("_active_process", None)
        with patch.object(os, "killpg") as killpg:
            phash_worker._handle_stop(signal.SIGINT, None)

        killpg.assert_not_called()


class TestCapabilityPublishing(WorkerPatch):
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "capabilities.json")
        self.patch("CAPABILITIES_PATH", self.path)

    def test_the_worker_advertises_phash_only_while_running(self):
        phash_worker._publish(True)
        snapshot = read_worker_capabilities(self.path, max_age_seconds=3600)
        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["actions"], ["video_phash"])

        phash_worker._publish(False)
        self.assertFalse(read_worker_capabilities(self.path, max_age_seconds=3600)["available"])

    def test_a_capability_write_failure_is_only_logged(self):
        with self.assertLogs("resolver.phash_worker", level="WARNING") as logs:
            with patch.object(phash_worker, "publish_worker_capabilities",
                              side_effect=OSError("read-only mount")):
                phash_worker._publish(True)

        self.assertIn("Could not publish pHash worker status", "\n".join(logs.output))


class TestResolverReadiness(unittest.TestCase):
    class Response:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def ready_with(self, status=None, error=None):
        def urlopen(url, timeout=None):
            if error is not None:
                raise error
            return self.Response(status)

        with patch.object(urllib.request, "urlopen", side_effect=urlopen):
            return phash_worker._resolver_ready()

    def test_only_a_200_from_the_readiness_endpoint_counts_as_ready(self):
        self.assertTrue(self.ready_with(status=200))
        self.assertFalse(self.ready_with(status=503))

    def test_transport_failures_mean_not_ready(self):
        for error in (urllib.error.URLError("refused"), TimeoutError(), OSError("reset")):
            with self.subTest(error=type(error).__name__):
                self.assertFalse(self.ready_with(error=error))


class TestCompute(WorkerPatch):
    def setUp(self):
        super().setUp()
        self.patch("PHASH_RANGE_CACHE_DIR", "/tmp/phash-ranges-under-test")

    def run_compute(self, process):
        popen = MagicMock(side_effect=lambda argv, **kwargs: process)
        with patch.object(phash_worker.subprocess, "Popen", popen), \
                patch.object(os, "killpg") as killpg:
            result = phash_worker._compute(dict(JOB))
        return result, popen, killpg

    def test_limits_are_clamped_into_the_child_payload(self):
        process = FakeProcess(stdout=json.dumps({"ok": False}))
        with phash_env(byte_budget="0", timeout_seconds="5",
                       bytes_per_second="1", idle_bytes_per_second="999999999"):
            _, popen, _ = self.run_compute(process)

        payload = json.loads(process.payload)
        self.assertEqual(payload["byte_budget"], 1)
        self.assertEqual(payload["timeout_seconds"], 30)
        self.assertEqual(payload["playback_bytes_per_second"], 64 * 1024)
        self.assertEqual(payload["idle_bytes_per_second"], 10_000_000)
        # The subprocess deadline leaves a margin on top of the transfer timeout.
        self.assertEqual(process.timeout, 60)
        self.assertEqual(popen.call_count, 1)

    def test_generous_limits_are_capped_at_the_worker_maximums(self):
        process = FakeProcess(stdout=json.dumps({"ok": False}))
        with phash_env(byte_budget="999999999999", timeout_seconds="999999999",
                       bytes_per_second="999999999", idle_bytes_per_second="1"):
            self.run_compute(process)

        payload = json.loads(process.payload)
        self.assertEqual(payload["byte_budget"], phash_worker.MAX_BYTE_BUDGET)
        self.assertEqual(payload["timeout_seconds"], phash_worker.MAX_TIMEOUT_SECONDS)
        self.assertEqual(payload["playback_bytes_per_second"], 10_000_000)
        self.assertEqual(payload["idle_bytes_per_second"], 64 * 1024)

    def test_the_defaults_match_the_documented_server_budget(self):
        process = FakeProcess(stdout=json.dumps({"ok": False}))
        with phash_env():
            _, popen, _ = self.run_compute(process)

        payload = json.loads(process.payload)
        self.assertEqual(payload["video_id"], JOB["video_id"])
        self.assertEqual(payload["duration_us"], JOB["duration_us"])
        self.assertEqual(payload["cache_id"], JOB["id"])
        self.assertEqual(payload["cache_dir"], "/tmp/phash-ranges-under-test")
        self.assertEqual(payload["timeout_seconds"], 600)
        self.assertEqual(payload["playback_bytes_per_second"], 2 * 1024 * 1024)
        self.assertEqual(payload["idle_bytes_per_second"], 10_000_000)
        argv = popen.call_args[0][0]
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1], "-c")
        self.assertTrue(popen.call_args[1]["start_new_session"])

    def test_a_successful_run_decodes_the_thumbnail(self):
        process = FakeProcess(stdout=json.dumps({
            "ok": True, "fingerprint": "ff" * 8, "bytes_read": 4096,
            "thumbnail": base64.b64encode(b"jpeg-bytes").decode("ascii")}))

        result, _, _ = self.run_compute(process)

        self.assertTrue(result["ok"])
        self.assertEqual(result["thumbnail"], b"jpeg-bytes")
        self.assertEqual(result["bytes_read"], 4096)

    def test_a_child_failure_report_passes_through(self):
        process = FakeProcess(stdout=json.dumps(
            {"ok": False, "error_code": "byte_budget_exhausted", "retryable": False}))

        result, _, _ = self.run_compute(process)

        self.assertEqual(result["error_code"], "byte_budget_exhausted")
        self.assertFalse(result["retryable"])

    def test_a_non_zero_exit_is_a_retryable_generation_failure(self):
        result, _, _ = self.run_compute(FakeProcess(stdout="", returncode=1))

        self.assertEqual(result["error_code"], "generation_failed")
        self.assertTrue(result["retryable"])
        self.assertEqual(result.get("error_detail"), "Generator exited with status 1")

    def test_unparsable_and_non_object_output_is_rejected(self):
        for stdout in ("traceback on stdout", json.dumps("a scalar"), "null"):
            with self.subTest(stdout=stdout):
                result, _, _ = self.run_compute(FakeProcess(stdout=stdout))
                self.assertEqual(result["error_code"], "generation_failed")
                self.assertTrue(result["retryable"])

    def test_an_ok_result_without_a_usable_thumbnail_is_rejected(self):
        for stdout in (json.dumps({"ok": True, "fingerprint": "ff", "bytes_read": 1}),
                       json.dumps({"ok": True, "fingerprint": "ff", "bytes_read": 1,
                                   "thumbnail": "%%% not base64 %%%"})):
            with self.subTest(stdout=stdout):
                result, _, _ = self.run_compute(FakeProcess(stdout=stdout))
                self.assertEqual(result["error_code"], "generation_failed")

    def test_a_run_that_overruns_its_deadline_is_killed_and_retried(self):
        process = FakeProcess(error=subprocess.TimeoutExpired(cmd=[sys.executable], timeout=1))

        result, _, killpg = self.run_compute(process)

        self.assertEqual(result, {"ok": False, "error_code": "timeout", "retryable": True})
        killpg.assert_called_once_with(process.pid, signal.SIGKILL)
        self.assertEqual(process.communications, 2)

    def test_killing_a_child_that_already_died_is_not_a_failure(self):
        process = FakeProcess(error=subprocess.TimeoutExpired(cmd=[sys.executable], timeout=1))
        with patch.object(phash_worker.subprocess, "Popen", return_value=process), \
                patch.object(os, "killpg", side_effect=ProcessLookupError):
            result = phash_worker._compute(dict(JOB))

        self.assertEqual(result["error_code"], "timeout")
        self.assertTrue(result["retryable"])

    def test_the_active_child_is_registered_only_while_it_runs(self):
        seen = {}
        process = FakeProcess(stdout=json.dumps({"ok": False}))
        original = process.communicate

        def communicate(payload=None, timeout=None):
            seen["active"] = phash_worker._active_process
            return original(payload, timeout)

        process.communicate = communicate
        with patch.object(phash_worker.subprocess, "Popen", return_value=process):
            phash_worker._compute(dict(JOB))

        self.assertIs(seen["active"], process)
        self.assertIsNone(phash_worker._active_process)


class TestRangeCachePurge(WorkerPatch):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.patch("PHASH_RANGE_CACHE_DIR", str(self.root))

    def cache_dir(self, name, updated_at=None):
        directory = self.root / name
        directory.mkdir()
        if updated_at is not None:
            (directory / "usage.json").write_text(json.dumps({"updated_at": updated_at}))
        return directory

    def test_expired_and_fresh_ledgers_are_split_at_the_ttl(self):
        now = 1_000_000.0
        ttl = phash_worker.PHASH_RANGE_CACHE_TTL_SECONDS
        at_the_boundary = self.cache_dir(SIXTY_FOUR, now - ttl)
        still_valid = self.cache_dir("b" * 64, now - ttl + 1)
        fresh = self.cache_dir("c" * 64, now - 60)

        self.assertEqual(phash_worker._purge_expired_range_caches(now), 1)
        self.assertFalse(at_the_boundary.exists())
        self.assertTrue(still_valid.exists())
        self.assertTrue(fresh.exists())

    def test_names_that_are_not_cache_directories_are_untouched(self):
        now = 1_000_000.0
        old = now - phash_worker.PHASH_RANGE_CACHE_TTL_SECONDS - 1
        short = self.cache_dir("sho", old)
        long = self.cache_dir("d" * 65, old)
        with_suffix = self.cache_dir("e" * 59 + ".tmp", old)
        a_file = self.root / ("f" * 64)
        a_file.write_text("stale")

        self.assertEqual(phash_worker._purge_expired_range_caches(now), 0)
        for survivor in (short, long, with_suffix, a_file):
            self.assertTrue(survivor.exists())

    def test_unreadable_ledgers_keep_their_cache(self):
        now = 1_000_000.0
        without_usage = self.root / ("0" * 64)
        without_usage.mkdir()
        broken = self.cache_dir("1" * 64)
        (broken / "usage.json").write_text("not json")
        missing_key = self.cache_dir("2" * 64)
        (missing_key / "usage.json").write_text(json.dumps({"bytes": 1}))
        not_a_number = self.cache_dir("3" * 64)
        (not_a_number / "usage.json").write_text(json.dumps({"updated_at": "yesterday"}))

        self.assertEqual(phash_worker._purge_expired_range_caches(now), 0)
        for survivor in (without_usage, broken, missing_key, not_a_number):
            self.assertTrue(survivor.exists())

    def test_a_symlinked_cache_directory_is_not_followed(self):
        now = 1_000_000.0
        target = self.cache_dir("target-dir")
        (target / "usage.json").write_text(json.dumps(
            {"updated_at": now - phash_worker.PHASH_RANGE_CACHE_TTL_SECONDS - 1}))
        (self.root / ("9" * 64)).symlink_to(target, target_is_directory=True)

        self.assertEqual(phash_worker._purge_expired_range_caches(now), 0)
        self.assertTrue(target.exists())

    def test_a_missing_cache_root_counts_as_nothing_removed(self):
        self.patch("PHASH_RANGE_CACHE_DIR", str(self.root / "absent"))

        self.assertEqual(phash_worker._purge_expired_range_caches(), 0)


class RecordingConnection:
    def __init__(self, lease_locked=True):
        self.lease_locked = lease_locked
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.row_factory = None

    def execute(self, sql, params=None):
        return SimpleNamespace(fetchone=lambda: (1,) if self.lease_locked else None)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class FakeQueue:
    def __init__(self):
        self.heartbeat_result = True
        self.heartbeats = []
        self.failures = []
        self.deferred = []
        self.successes = []
        self.finish_success_result = True

    def heartbeat(self, db_url, job_id, token):
        self.heartbeats.append((db_url, job_id, token))
        return self.heartbeat_result

    def finish_failure(self, db_url, job, error_code, *, retryable, error_detail=""):
        self.failures.append((db_url, job["id"], error_code, retryable))
        return True

    def defer_until_daily_reset(self, db_url, job):
        self.deferred.append((db_url, job["id"]))
        return True

    def finish_success(self, conn, job, fingerprint, bytes_read):
        self.successes.append((job["id"], fingerprint, bytes_read))
        return self.finish_success_result


class JobProcessingHarness(WorkerPatch):
    def setUp(self):
        super().setUp()
        self.stop_event = self.patch("_stop", threading.Event())
        self.patch("DB_URL", DB_URL)
        self.patch("HEARTBEAT_SECONDS", 3600)
        self.queue = self.patch("phash_jobs", FakeQueue())
        self.store = self.patch("_store", MagicMock(return_value="stored"))
        self.publish = self.patch("_publish", MagicMock())
        self.connection = RecordingConnection()
        self.patch("db", SimpleNamespace(connect=lambda url: self.connection, Record=object))
        self.compute = MagicMock(return_value={"ok": False, "error_code": "timeout",
                                               "retryable": True})
        self.patch("_compute", self.compute)
        self.leader = MagicMock()

    def process_job(self):
        phash_worker._process(dict(JOB), self.leader)


class TestJobProcessing(JobProcessingHarness):
    def test_phash_result_does_not_require_thumbnail(self):
        self.compute.return_value = {'ok': True, 'fingerprint': 'ff' * 8, 'bytes_read': 42, 'thumbnail': None}
        phash_worker._process({**JOB, 'job_kind': 'phash'}, self.leader)
        self.assertIsNone(self.store.call_args[0][5])
        self.assertEqual(self.queue.successes, [(JOB['id'], 'ff' * 8, 42)])

    def test_thumbnail_result_does_not_write_fingerprint(self):
        self.compute.return_value = {'ok': True, 'fingerprint': None, 'bytes_read': 42, 'thumbnail': b'jpeg'}
        phash_worker._process({**JOB, 'job_kind': 'thumbnail'}, self.leader)
        self.store.assert_not_called()
        self.assertEqual(self.queue.successes, [(JOB['id'], None, 42)])
        self.assertEqual(self.connection.commits, 1)

    def test_a_healthy_lease_stores_the_fingerprint_and_commits(self):
        self.compute.return_value = {"ok": True, "fingerprint": "ff" * 8,
                                     "bytes_read": 2048, "thumbnail": b"jpeg"}

        self.process_job()

        admin, video_id, fingerprint, profile, user_id, thumbnail = self.store.call_args[0]
        self.assertEqual(admin.HUB_DB_URL, DB_URL)
        self.assertEqual((video_id, fingerprint, profile, user_id),
                         (JOB["video_id"], "ff" * 8, JOB["decoder_profile"],
                          JOB["submitted_by_user_id"]))
        self.assertEqual(thumbnail, b"jpeg")
        self.assertIs(self.store.call_args[1]["connection"], self.connection)
        self.assertEqual(self.queue.successes, [(JOB["id"], "ff" * 8, 2048)])
        self.assertEqual(self.queue.failures, [])
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (1, 0))
        self.assertTrue(self.connection.closed)

    def test_a_failure_result_is_recorded_with_its_own_code(self):
        self.compute.return_value = {"ok": False, "error_code": "daily_transfer_budget_exhausted",
                                     "retryable": False}

        self.process_job()

        self.assertEqual(self.queue.deferred, [(DB_URL, JOB["id"])])
        self.assertEqual(self.queue.failures, [])
        self.store.assert_not_called()
        self.assertEqual(self.queue.successes, [])

    def test_a_failure_without_a_code_falls_back_to_generation_failed(self):
        self.compute.return_value = {"ok": False, "retryable": True}

        self.process_job()

        self.assertEqual(self.queue.failures[0][2:], ("generation_failed", True))

    def test_losing_the_lease_abandons_the_job_without_writing(self):
        self.queue.heartbeat_result = False
        self.patch("HEARTBEAT_SECONDS", 0.01)
        self.compute.side_effect = lambda job: (self.stop_event.wait(2),
                                                {"ok": False, "error_code": "timeout",
                                                 "retryable": True})[1]

        self.process_job()

        self.assertEqual(self.queue.failures, [])
        self.store.assert_not_called()
        self.assertTrue(self.queue.heartbeats)

    def test_a_leader_connection_error_abandons_the_job(self):
        self.leader.execute.side_effect = OSError("connection lost")
        self.patch("HEARTBEAT_SECONDS", 0.01)
        self.compute.side_effect = lambda job: (self.stop_event.wait(2),
                                                {"ok": True, "fingerprint": "ff",
                                                 "bytes_read": 1, "thumbnail": b""})[1]

        self.process_job()

        self.assertEqual(self.queue.failures, [])
        self.assertEqual(self.queue.successes, [])
        self.store.assert_not_called()

    def test_a_heartbeat_that_extends_the_lease_keeps_publishing(self):
        self.patch("HEARTBEAT_SECONDS", 0.01)

        def compute(job):
            for _ in range(200):
                if self.publish.call_count >= 2:
                    break
                threading.Event().wait(0.01)
            return {"ok": True, "fingerprint": "ab", "bytes_read": 8, "thumbnail": b"t"}

        self.compute.side_effect = compute

        self.process_job()

        self.assertGreaterEqual(len(self.queue.heartbeats), 2)
        for entry in self.queue.heartbeats:
            self.assertEqual(entry, (DB_URL, JOB["id"], JOB["lease_token"]))
        self.publish.assert_any_call(True)
        self.assertEqual(self.queue.successes, [(JOB["id"], "ab", 8)])

    def test_a_stop_signal_during_computation_abandons_the_job(self):
        self.compute.side_effect = lambda job: (phash_worker._handle_stop(signal.SIGTERM, None),
                                                {"ok": True, "fingerprint": "ab",
                                                 "bytes_read": 8, "thumbnail": b"t"})[1]

        self.process_job()

        self.store.assert_not_called()
        self.assertEqual(self.queue.failures, [])

    def test_a_lease_row_that_no_longer_matches_rolls_back(self):
        self.connection.lease_locked = False
        self.compute.return_value = {"ok": True, "fingerprint": "ff",
                                     "bytes_read": 1, "thumbnail": b"t"}

        self.process_job()

        self.store.assert_not_called()
        self.assertEqual((self.connection.rollbacks, self.connection.commits), (1, 0))
        self.assertEqual(self.queue.failures, [])

    def test_a_conflicting_fingerprint_is_a_permanent_failure(self):
        self.store.return_value = "conflict"
        self.compute.return_value = {"ok": True, "fingerprint": "ff",
                                     "bytes_read": 1, "thumbnail": b"t"}

        self.process_job()

        self.assertEqual(self.connection.rollbacks, 1)
        self.assertEqual(self.queue.failures[0][2:], ("fingerprint_conflict", False))
        self.assertEqual(self.queue.successes, [])

    def test_a_rejected_completion_update_rolls_back_without_failing(self):
        self.queue.finish_success_result = False
        self.compute.return_value = {"ok": True, "fingerprint": "ff",
                                     "bytes_read": 1, "thumbnail": b"t"}

        self.process_job()

        self.assertEqual((self.connection.rollbacks, self.connection.commits), (1, 0))
        self.assertEqual(self.queue.failures, [])

    def test_a_storage_exception_rolls_back_and_records_a_retryable_failure(self):
        self.store.side_effect = RuntimeError("provenance row vanished")
        self.compute.return_value = {"ok": True, "fingerprint": "ff",
                                     "bytes_read": 1, "thumbnail": b"t"}

        self.process_job()

        self.assertEqual(self.connection.rollbacks, 1)
        self.assertTrue(self.connection.closed)
        self.assertEqual(self.queue.failures[0][2:], ("generation_failed", True))

    def test_a_failing_failure_record_is_only_logged(self):
        self.queue.finish_failure = MagicMock(side_effect=RuntimeError("queue is down"))
        self.compute.side_effect = RuntimeError("child vanished")

        with self.assertLogs("resolver.phash_worker", level="ERROR") as logs:
            self.process_job()

        self.assertTrue(self.queue.finish_failure.called)
        self.assertIn("Could not record pHash job failure", "\n".join(logs.output))

    def test_the_heartbeat_thread_is_joined_before_returning(self):
        before = set(threading.enumerate())

        self.process_job()

        left_over = [thread for thread in set(threading.enumerate()) - before
                     if thread.is_alive()]
        self.assertEqual(left_over, [])


class FakeClock:
    """Monotonic clock far past zero so the publish (5s) and purge (1h) cadences both fire."""

    def __init__(self, start=40_000.0, step=1.0):
        self.value = start
        self.step = step

    def monotonic(self):
        current = self.value
        self.value += self.step
        return current


class FakeStop:
    """Stand-in for the stop event that lets the loop exit after a fixed number of waits."""

    def __init__(self, waits=1):
        self.remaining = waits
        self.settled = False
        self.waits = []

    def is_set(self):
        return self.settled

    def set(self):
        self.settled = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.remaining -= 1
        if self.remaining <= 0:
            self.settled = True
        return self.settled


class WorkerLoopHarness(WorkerPatch):
    def setUp(self):
        super().setUp()
        self.clock = self.patch("time", FakeClock())
        self.queue = self.patch("phash_jobs", MagicMock())
        self.queue.recover_expired.return_value = 0
        self.queue.claim_next.return_value = None
        self.process = self.patch("_process", MagicMock())
        self.publish = self.patch("_publish", MagicMock())
        self.ready = self.patch("_resolver_ready", MagicMock(return_value=True))
        self.purge = self.patch("_purge_expired_range_caches", MagicMock(return_value=0))
        self.patch("DB_URL", DB_URL)
        self.leaders = []

        def acquire(db_url):
            leader = MagicMock()
            self.leaders.append(leader)
            return leader

        self.queue.worker_lock.side_effect = acquire

    def run_main(self, upload="true", waits=1):
        self.stop = FakeStop(waits)
        self.patch("_stop", self.stop)
        with patch.dict(os.environ, {"PHASH_UPLOAD_ENABLED": upload}), \
                patch.object(signal, "signal") as install:
            code = phash_worker.main()
        return code, install


class TestWorkerLoop(WorkerLoopHarness):
    def test_signal_handlers_are_installed_before_the_loop_starts(self):
        code, install = self.run_main(upload="false")

        self.assertEqual(code, 0)
        self.assertEqual([call[0][0] for call in install.call_args_list],
                         [signal.SIGTERM, signal.SIGINT])
        for call in install.call_args_list:
            self.assertIs(call[0][1], phash_worker._handle_stop)

    def test_the_loop_stays_idle_until_uploads_are_enabled(self):
        code, _ = self.run_main(upload="false")

        self.assertEqual(code, 0)
        self.queue.worker_lock.assert_not_called()
        self.purge.assert_not_called()
        self.assertEqual([call[0][0] for call in self.publish.call_args_list], [True, False])

    def test_the_feature_flag_is_read_case_insensitively(self):
        code, _ = self.run_main(upload="TRUE")

        self.assertEqual(code, 0)
        self.queue.worker_lock.assert_called_once_with(DB_URL)

    def test_a_lock_held_by_another_worker_is_retried(self):
        self.queue.worker_lock.side_effect = lambda db_url: None

        code, _ = self.run_main()

        self.assertEqual(code, 0)
        self.queue.claim_next.assert_not_called()
        self.assertEqual([call[0][0] for call in self.publish.call_args_list], [True, False])

    def test_an_unavailable_queue_is_reported_and_retried(self):
        attempts = []

        def acquire(db_url):
            attempts.append(db_url)
            if len(attempts) == 1:
                raise RuntimeError("connection refused")
            leader = MagicMock()
            self.leaders.append(leader)
            return leader

        self.queue.worker_lock.side_effect = acquire
        code, _ = self.run_main(waits=2)

        self.assertEqual(code, 0)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.purge.call_count, 1)

    def test_a_failed_recovery_releases_the_lock_it_just_took(self):
        self.queue.recover_expired.side_effect = RuntimeError("recovery query failed")

        code, _ = self.run_main(waits=2)

        self.assertEqual(code, 0)
        self.assertTrue(self.leaders)
        for leader in self.leaders:
            leader.close.assert_called_once()
        self.queue.claim_next.assert_not_called()

    def test_a_failing_lock_release_is_ignored(self):
        self.queue.recover_expired.side_effect = RuntimeError("recovery query failed")
        original_acquire = self.queue.worker_lock.side_effect

        def acquire(db_url):
            leader = original_acquire(db_url)
            leader.close.side_effect = OSError("session already gone")
            return leader

        self.queue.worker_lock.side_effect = acquire
        code, _ = self.run_main()

        self.assertEqual(code, 0)
        self.queue.claim_next.assert_not_called()

    def test_the_steady_state_loop_keep_recovering_expired_leases(self):
        self.queue.recover_expired.return_value = 2

        code, _ = self.run_main(waits=2)

        self.assertEqual(code, 0)
        self.assertGreaterEqual(self.queue.recover_expired.call_count, 2)

    def test_expired_leases_are_recovered_when_the_lock_is_acquired(self):
        self.queue.recover_expired.return_value = 3

        code, _ = self.run_main()

        self.assertEqual(code, 0)
        self.queue.recover_expired.assert_called_with(DB_URL)

    def test_jobs_wait_for_the_resolver_to_become_ready(self):
        self.ready.return_value = False

        code, _ = self.run_main()

        self.assertEqual(code, 0)
        self.queue.claim_next.assert_not_called()
        self.process.assert_not_called()

    def test_a_claimed_job_is_processed_with_the_leader(self):
        job = {"id": "p-1", "attempts": 1}
        self.queue.claim_next.side_effect = [job, None]

        code, _ = self.run_main(waits=2)

        self.assertEqual(code, 0)
        self.process.assert_called_once_with(job, self.leaders[0])

    def test_a_broken_leader_is_closed_and_reacquired(self):
        def acquire(db_url):
            leader = MagicMock()
            if not self.leaders:
                leader.execute.side_effect = OSError("leader connection lost")
            self.leaders.append(leader)
            return leader

        self.queue.worker_lock.side_effect = acquire
        code, _ = self.run_main(waits=3)

        self.assertEqual(code, 0)
        self.assertEqual(len(self.leaders), 2)
        self.leaders[0].close.assert_called_once()
        self.leaders[1].close.assert_called_once()

    def test_a_failing_job_cycle_drops_the_leader(self):
        self.queue.claim_next.side_effect = RuntimeError("claim failed")

        code, _ = self.run_main(waits=2)

        self.assertEqual(code, 0)
        self.assertEqual(len(self.leaders), 2)
        self.leaders[0].close.assert_called_once()

    def test_the_cycle_error_handler_tolerates_a_failing_close(self):
        self.queue.claim_next.side_effect = RuntimeError("claim failed")
        original_acquire = self.queue.worker_lock.side_effect

        def acquire(db_url):
            leader = original_acquire(db_url)
            if leader is not None:
                leader.close.side_effect = OSError("already gone")
            return leader

        self.queue.worker_lock.side_effect = acquire
        code, _ = self.run_main(waits=2)

        self.assertEqual(code, 0)

    def test_expired_range_caches_are_purged_while_uploads_are_enabled(self):
        self.purge.return_value = 4

        code, _ = self.run_main()

        self.assertEqual(code, 0)
        self.purge.assert_called_once_with()

    def test_a_failing_purge_does_not_stop_the_worker(self):
        self.purge.side_effect = OSError("cache root unreadable")

        code, _ = self.run_main()

        self.assertEqual(code, 0)
        self.leaders[0].close.assert_called_once()

    def test_the_worker_publishes_idle_and_closes_its_lock_on_exit(self):
        code, _ = self.run_main()

        self.assertEqual(code, 0)
        self.leaders[0].close.assert_called_once()
        self.assertEqual(self.publish.call_args[0], (False,))
        self.assertEqual(self.stop.waits, [phash_worker.POLL_SECONDS])


if __name__ == "__main__":
    unittest.main()
