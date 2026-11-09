"""Unit tests for scheduled backup execution in the durable worker queue and sync runner.

Verifies:
1. Atomic due and manual dispatch of backup_create jobs into video_duplicate_jobs.
2. Deduplication of active backup jobs (no concurrent duplicate jobs).
3. 5-minute retry delay for failed scheduled database backups.
4. Worker schedule status updates on start, success, and failure.
5. Cron revision preservation when edits occur while a job is running.
6. History-cleanup follow-up stage enqueued on backup success when enabled.
7. Crash recovery of orphaned queued/processing schedule states.
8. Verified archive reuse on same-job retry (pg_backup idempotency).
9. Backup retention pruning (legacy timestamp, job-id, sidecar cleanup, per-file error handling).
10. Artwork generation due-check and batch enqueue in sync runner.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common import backup, db, schedule_stages, system_schedules
from sync import runner, runner_helpers


class FakeCursor:
    def __init__(self, fetchone_result=None, fetchall_result=None, rowcount=0):
        self._fetchone_result = fetchone_result
        self._fetchall_result = fetchall_result or []
        self.rowcount = rowcount

    def fetchone(self):
        return self._fetchone_result

    def fetchall(self):
        return self._fetchall_result


class FakeConnection:
    def __init__(self, queries=None):
        self.queries = queries or {}
        self.executed: list[tuple[str, tuple]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, query: str, params: tuple = ()):
        self.executed.append((query, params))
        for pattern, result in self.queries.items():
            if pattern in query:
                if callable(result):
                    return result(query, params)
                if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
                    return FakeCursor(fetchone_result=result[0], rowcount=result[1])
                return FakeCursor(fetchone_result=result)
        return FakeCursor()

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.rollback()
        else:
            self.commit()
        return False


class TestSystemScheduleDispatch(unittest.TestCase):
    def test_dispatch_due_schedule_enqueues_worker_job(self):
        now = dt.datetime(2026, 11, 7, 4, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
        due_row = (
            True,           # enabled
            "30 4 * * *",   # cron_expression
            None,           # legacy_interval_minutes
            "04:30",        # legacy_daily_at
            False,          # needs_conversion
            1,              # revision
            now - dt.timedelta(minutes=1),  # next_run_at (due)
            "done",         # last_result
            now - dt.timedelta(days=1),     # last_finished_at
            None,           # last_job_id
        )
        fake_conn = FakeConnection({
            "SELECT enabled,cron_expression": due_row,
        })
        with patch.object(db, "connect", return_value=fake_conn):
            dispatched = system_schedules.dispatch_due_system_schedules("postgresql://fixture/db", now=now)

        self.assertEqual(dispatched, 1)
        self.assertTrue(fake_conn.committed)
        insert_calls = [c for c in fake_conn.executed if "INSERT INTO video_duplicate_jobs" in c[0]]
        self.assertEqual(len(insert_calls), 1)
        query, params = insert_calls[0]
        self.assertEqual(params[1], "backup_create")
        self.assertIn("'queued'", query)
        payload = json.loads(params[2])
        self.assertEqual(payload.get("_system_schedule_id"), "database-backup")
        self.assertFalse(payload.get("_manual"))
        self.assertTrue(payload.get("_slot_due"))

        update_calls = [c for c in fake_conn.executed if "UPDATE system_schedules SET last_job_id" in c[0]]
        self.assertEqual(len(update_calls), 1)

    def test_dispatch_due_scene_fingerprints_enqueues_worker_job(self):
        now = dt.datetime(2026, 11, 7, 4, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
        due_row = (
            True,           # enabled
            "30 4 * * *",   # cron_expression
            None,           # legacy_interval_minutes
            "04:30",        # legacy_daily_at
            False,          # needs_conversion
            3,              # revision
            now - dt.timedelta(minutes=1),  # next_run_at (due)
            "done",         # last_result
            now - dt.timedelta(days=1),     # last_finished_at
            None,           # last_job_id
        )
        fake_conn = FakeConnection({
            "SELECT s.enabled": due_row,
        })
        with patch.object(db, "connect", return_value=fake_conn):
            dispatched = system_schedules.dispatch_due_system_schedules("postgresql://fixture/db", now=now)

        self.assertEqual(dispatched, 1)
        self.assertTrue(fake_conn.committed)
        insert_calls = [c for c in fake_conn.executed if "INSERT INTO video_duplicate_jobs" in c[0]]
        self.assertEqual(len(insert_calls), 1)
        query, params = insert_calls[0]
        self.assertEqual(params[1], "scene_backfill")
        self.assertIn("'queued'", query)
        payload = json.loads(params[2])
        self.assertEqual(payload.get("_system_schedule_id"), "scene-fingerprints")
        self.assertEqual(payload.get("_system_schedule_revision"), 3)
        self.assertEqual(payload.get("limit"), 500)
        self.assertFalse(payload.get("_manual"))
        self.assertTrue(payload.get("_slot_due"))

        update_calls = [c for c in fake_conn.executed if "UPDATE system_schedules SET last_job_id" in c[0]]
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1][3], "scene-fingerprints")

    def test_dispatch_deduplicates_when_active_job_exists(self):
        now = dt.datetime(2026, 11, 7, 4, 30, tzinfo=ZoneInfo("Europe/Warsaw"))
        due_row = (
            True, "30 4 * * *", None, "04:30", False, 1,
            now - dt.timedelta(minutes=1), "queued", None, "existing-job-123",
        )
        fake_conn = FakeConnection({
            "SELECT enabled,cron_expression": due_row,
            "SELECT state FROM video_duplicate_jobs": ("processing",),
        })
        with patch.object(db, "connect", return_value=fake_conn):
            dispatched = system_schedules.dispatch_due_system_schedules("postgresql://fixture/db", now=now)

        self.assertEqual(dispatched, 0)
        insert_calls = [c for c in fake_conn.executed if "INSERT INTO video_duplicate_jobs" in c[0]]
        self.assertEqual(len(insert_calls), 0)

    def test_dispatch_honors_five_minute_retry_delay_on_failure(self):
        now = dt.datetime(2026, 11, 7, 4, 32, tzinfo=ZoneInfo("Europe/Warsaw"))
        # Failed 2 minutes ago (less than 300 seconds)
        failed_row = (
            True, "30 4 * * *", None, "04:30", False, 1,
            now - dt.timedelta(minutes=2), "failed", now - dt.timedelta(minutes=2), None,
        )
        fake_conn = FakeConnection({
            "SELECT enabled,cron_expression": failed_row,
        })
        with patch.object(db, "connect", return_value=fake_conn):
            dispatched = system_schedules.dispatch_due_system_schedules("postgresql://fixture/db", now=now)
        self.assertEqual(dispatched, 0)

        # Failed 6 minutes ago (more than 300 seconds) -> should retry
        now_retry = dt.datetime(2026, 11, 7, 4, 37, tzinfo=ZoneInfo("Europe/Warsaw"))
        fake_conn_retry = FakeConnection({
            "SELECT enabled,cron_expression": failed_row,
        })
        with patch.object(db, "connect", return_value=fake_conn_retry):
            dispatched_retry = system_schedules.dispatch_due_system_schedules("postgresql://fixture/db", now=now_retry)
        self.assertEqual(dispatched_retry, 1)

    def test_manual_system_schedule_run_request(self):
        now = dt.datetime(2026, 11, 7, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        sched_row = (
            True, "30 4 * * *", None, "04:30", False, 1,
            now + dt.timedelta(hours=4), "done", None,
        )
        fake_conn = FakeConnection({
            "SELECT enabled,cron_expression": sched_row,
        })
        with patch.object(db, "connect", return_value=fake_conn):
            result = system_schedules.request_system_run("postgresql://fixture/db", "database-backup", now=now)

        self.assertEqual(result.get("outcome"), "queued")
        self.assertTrue(result.get("job_id"))
        insert_calls = [c for c in fake_conn.executed if "INSERT INTO video_duplicate_jobs" in c[0]]
        self.assertEqual(len(insert_calls), 1)
        payload = json.loads(insert_calls[0][1][1])
        self.assertTrue(payload.get("_manual"))

    def test_manual_system_schedule_run_conflict_when_active(self):
        now = dt.datetime(2026, 11, 7, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
        sched_row = (
            True, "30 4 * * *", None, "04:30", False, 1,
            now + dt.timedelta(hours=4), "processing", "active-job-999",
        )
        fake_conn = FakeConnection({
            "SELECT enabled,cron_expression": sched_row,
            "SELECT state FROM video_duplicate_jobs": ("processing",),
        })
        with patch.object(db, "connect", return_value=fake_conn):
            result = system_schedules.request_system_run("postgresql://fixture/db", "database-backup", now=now)
        self.assertEqual(result.get("outcome"), "conflict")


class TestSystemScheduleWorkerStatus(unittest.TestCase):
    def test_worker_records_job_start(self):
        fake_conn = FakeConnection()
        payload = {"_system_schedule_id": "database-backup", "_manual": False}
        system_schedules.record_system_schedule_job_start(fake_conn, "job-1", payload)

        update_calls = [c for c in fake_conn.executed if "UPDATE system_schedules SET last_result=" in c[0]]
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1][0], "processing")

    def test_worker_records_manual_job_start(self):
        fake_conn = FakeConnection()
        payload = {"_system_schedule_id": "database-backup", "_manual": True}
        system_schedules.record_system_schedule_job_start(fake_conn, "job-1", payload)

        update_calls = [c for c in fake_conn.executed if "UPDATE system_schedules SET last_result=" in c[0]]
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1][0], "processing_manual")

    def test_worker_finish_success_advances_slot(self):
        now = dt.datetime(2026, 11, 7, 4, 35, tzinfo=ZoneInfo("Europe/Warsaw"))
        fake_conn = FakeConnection({
            "SELECT cron_expression": ("30 4 * * *", None, "04:30", False, True, 1),
        })
        payload = {
            "_system_schedule_id": "database-backup",
            "_system_schedule_revision": 1,
            "_manual": False,
            "_slot_due": True,
        }
        system_schedules.finish_system_schedule_job(fake_conn, "job-1", payload, success=True, now=now)

        update_calls = [c for c in fake_conn.executed if "UPDATE system_schedules SET last_result=" in c[0]]
        self.assertEqual(len(update_calls), 1)
        params = update_calls[0][1]
        self.assertEqual(params[0], "done")
        next_run_at = params[2]
        self.assertGreater(next_run_at, now)

    def test_worker_finish_failure_leaves_slot_owed(self):
        now = dt.datetime(2026, 11, 7, 4, 35, tzinfo=ZoneInfo("Europe/Warsaw"))
        fake_conn = FakeConnection({
            "SELECT cron_expression": ("30 4 * * *", None, "04:30", False, True, 1),
        })
        payload = {
            "_system_schedule_id": "database-backup",
            "_system_schedule_revision": 1,
            "_manual": False,
            "_slot_due": True,
        }
        system_schedules.finish_system_schedule_job(fake_conn, "job-1", payload, success=False, now=now)

        update_calls = [c for c in fake_conn.executed if "UPDATE system_schedules SET last_result=" in c[0]]
        self.assertEqual(len(update_calls), 1)
        params = update_calls[0][1]
        self.assertEqual(params[0], "failed")
        # next_run_at must not be in the update params when failed
        self.assertNotIn("next_run_at=%s", update_calls[0][0])

    def test_worker_finish_preserves_new_cron_revision(self):
        now = dt.datetime(2026, 11, 7, 4, 35, tzinfo=ZoneInfo("Europe/Warsaw"))
        # Database revision changed to 2 while job payload had revision 1
        fake_conn = FakeConnection({
            "SELECT cron_expression": ("0 5 * * *", None, "05:00", False, True, 2),
        })
        payload = {
            "_system_schedule_id": "database-backup",
            "_system_schedule_revision": 1,
            "_manual": False,
            "_slot_due": True,
        }
        system_schedules.finish_system_schedule_job(fake_conn, "job-1", payload, success=True, now=now)

        update_calls = [c for c in fake_conn.executed if "UPDATE system_schedules SET last_result=" in c[0]]
        self.assertEqual(len(update_calls), 1)
        # Should not update next_run_at because revision did not match
        self.assertNotIn("next_run_at=%s", update_calls[0][0])


class TestFollowUpHistoryCleanup(unittest.TestCase):
    def test_enqueues_history_cleanup_when_stage_enabled(self):
        fake_conn = FakeConnection({
            "SELECT 1 FROM video_duplicate_jobs WHERE action='history_cleanup'": None,
        })
        with patch.object(schedule_stages, "stage_enabled", return_value=True):
            enqueued = system_schedules.maybe_enqueue_backup_history_cleanup(
                fake_conn, {"_system_schedule_id": "database-backup"}
            )
        self.assertTrue(enqueued)
        job_inserts = [c for c in fake_conn.executed if "INSERT INTO video_duplicate_jobs" in c[0]]
        self.assertEqual(len(job_inserts), 1)
        self.assertIn("'history_cleanup'", job_inserts[0][0])
        stage_inserts = [c for c in fake_conn.executed if "INSERT INTO system_schedule_stages" in c[0]]
        self.assertEqual(len(stage_inserts), 1)

    def test_deduplicates_when_already_queued(self):
        fake_conn = FakeConnection({
            "SELECT 1 FROM video_duplicate_jobs WHERE action='history_cleanup'": (1,),
        })
        with patch.object(schedule_stages, "stage_enabled", return_value=True):
            enqueued = system_schedules.maybe_enqueue_backup_history_cleanup(
                fake_conn, {"_system_schedule_id": "database-backup"}
            )
        self.assertFalse(enqueued)

    def test_skips_when_stage_disabled(self):
        fake_conn = FakeConnection()
        with patch.object(schedule_stages, "stage_enabled", return_value=False):
            enqueued = system_schedules.maybe_enqueue_backup_history_cleanup(
                fake_conn, {"_system_schedule_id": "database-backup"}
            )
        self.assertFalse(enqueued)


class TestCrashRecoveryAndArchiveReuse(unittest.TestCase):
    def test_recover_system_schedules_recovers_orphaned_state(self):
        fake_conn = FakeConnection({
            "SELECT last_job_id, last_result FROM system_schedules": ("old-job-99", "processing"),
            "SELECT state FROM video_duplicate_jobs": None,  # job gone
        })
        with patch.object(db, "connect", return_value=fake_conn):
            system_schedules.recover_system_schedules("postgresql://fixture/db")

        update_calls = [c for c in fake_conn.executed if "UPDATE system_schedules SET last_result='failed'" in c[0]]
        self.assertEqual(len(update_calls), 1)

    def test_pg_backup_reuses_verified_archive_on_retry(self):
        import gzip
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)
            job_id = "a" * 32
            archive_path = dest_dir / f"adulthub-job-{job_id}.dump.gz"
            content = gzip.compress(b"TEST_COMPRESSED_ARCHIVE_DATA")
            archive_path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            sidecar_path = Path(f"{archive_path}.sha256")
            sidecar_path.write_text(f"{digest}  {archive_path.name}\n")

            def mock_subcommand(cmd, *args, **kwargs):
                if "pg_restore" in cmd[0]:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"TABLE DATA public.test\n", stderr=b"")
                raise AssertionError(f"Unexpected command executed: {cmd}")

            with patch("shutil.which", return_value="/usr/bin/pg_restore"), \
                 patch("subprocess.run", side_effect=mock_subcommand):
                res = backup.pg_backup("postgresql://fixture/db", backup_dir=dest_dir, job_id=job_id)

            self.assertEqual(res, archive_path)


class TestBackupRetentionPruning(unittest.TestCase):
    def test_pruning_removes_legacy_and_job_backups_with_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)
            old_time = time.time() - (10 * 86400)
            fresh_time = time.time()

            # Old files
            old_legacy = dest_dir / "adulthub-20200101-0000.dump.gz"
            old_legacy.write_bytes(b"legacy")
            Path(f"{old_legacy}.sha256").write_text("dummy")
            os.utime(old_legacy, (old_time, old_time))

            old_job = dest_dir / "adulthub-job-old123.dump.gz"
            old_job.write_bytes(b"job")
            Path(f"{old_job}.sha256").write_text("dummy")
            os.utime(old_job, (old_time, old_time))

            # Fresh files
            fresh_legacy = dest_dir / "adulthub-20990101-0000.dump.gz"
            fresh_legacy.write_bytes(b"fresh_legacy")
            Path(f"{fresh_legacy}.sha256").write_text("dummy")
            os.utime(fresh_legacy, (fresh_time, fresh_time))

            fresh_job = dest_dir / "adulthub-job-fresh456.dump.gz"
            fresh_job.write_bytes(b"fresh_job")
            Path(f"{fresh_job}.sha256").write_text("dummy")
            os.utime(fresh_job, (fresh_time, fresh_time))

            backup.prune_old_backups(dest_dir, keep_days=7)

            self.assertFalse(old_legacy.exists())
            self.assertFalse(Path(f"{old_legacy}.sha256").exists())
            self.assertFalse(old_job.exists())
            self.assertFalse(Path(f"{old_job}.sha256").exists())

            self.assertTrue(fresh_legacy.exists())
            self.assertTrue(Path(f"{fresh_legacy}.sha256").exists())
            self.assertTrue(fresh_job.exists())
            self.assertTrue(Path(f"{fresh_job}.sha256").exists())

    def test_pruning_per_file_failure_logs_warning_without_failing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest_dir = Path(tmp)
            old_time = time.time() - (10 * 86400)
            old_file = dest_dir / "adulthub-20200101-0000.dump.gz"
            old_file.write_bytes(b"old")
            os.utime(old_file, (old_time, old_time))

            with patch.object(Path, "unlink", side_effect=OSError("Permission denied")), \
                 patch("logging.Logger.warning") as mock_warn:
                # Should not raise exception
                backup.prune_old_backups(dest_dir, keep_days=7)
                mock_warn.assert_called()


class TestArtworkScheduleDispatchInSync(unittest.TestCase):
    def test_artwork_dispatch_claims_and_queues_batch(self):
        timers = runner_helpers._WaitTimers()
        with patch.object(runner, "claim_due_system_schedule", return_value=True) as mock_claim, \
             patch.object(runner, "_dispatch_artwork_batch", return_value=(True, 5)) as mock_batch, \
             patch.object(runner, "finish_system_schedule") as mock_finish:
            wake = runner._dispatch_artwork_schedule_due(timers)

        self.assertTrue(wake)
        mock_claim.assert_called_once_with(runner.DB_URL, "artwork-generation")
        mock_batch.assert_called_once()
        mock_finish.assert_called_once_with(runner.DB_URL, "artwork-generation", success=True)
        self.assertGreater(timers.next_artwork_dispatch, 0.0)

    def test_artwork_dispatch_skips_when_not_due(self):
        timers = runner_helpers._WaitTimers()
        with patch.object(runner, "claim_due_system_schedule", return_value=False) as mock_claim, \
             patch.object(runner, "_dispatch_artwork_batch") as mock_batch:
            wake = runner._dispatch_artwork_schedule_due(timers)

        self.assertFalse(wake)
        mock_claim.assert_called_once()
        mock_batch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
