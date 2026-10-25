"""Process-level checks for stopping the sync runner cleanly."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase


class TestSyncRunnerProcess(PgTestCase):

    def test_durable_sync_job_heartbeats_while_sync_is_running(self):
        import importlib.util
        import threading
        from unittest.mock import Mock

        spec = importlib.util.spec_from_file_location("runner_heartbeat_module", ROOT / "sync/runner.py")
        runner_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner_module)

        heartbeat_seen = threading.Event()
        runner_module.DB_URL = self.db_url
        runner_module.claim_job = lambda _db_url, _action: {
            "id": "active-sync-job", "payload": json.dumps({"source": "telegram:test"})
        }
        runner_module.heartbeat_job = Mock(side_effect=lambda *_args: heartbeat_seen.set())
        runner_module.SYNC_JOB_HEARTBEAT_INTERVAL_SECONDS = 0.01
        runner_module.run_full_sync = lambda *_args: (heartbeat_seen.wait(1), 0)
        runner_module.finish_job = Mock()
        runner_module._clear_sync_trigger = Mock()

        self.assertTrue(runner_module.run_pending_sync_jobs())
        runner_module.heartbeat_job.assert_called()
        runner_module.finish_job.assert_called_once()
        self.assertEqual(runner_module.finish_job.call_args.args[1:3],
                         ("active-sync-job", "done"))

    def test_shutdown_stops_child_and_publishes_final_status(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_runner_") as temp:
            temp_path = Path(temp)
            child_pid_file = temp_path / "child.pid"
            status_file = temp_path / "sync_status.json"
            capabilities_file = temp_path / "worker-capabilities.json"
            trigger_file = temp_path / "sync.trigger"
            (temp_path / "sync.py").write_text(
                "import os, signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"open({str(child_pid_file)!r}, 'w').write(str(os.getpid()))\n"
                "time.sleep(120)\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            current_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(ROOT), current_pythonpath) if part)
            env["CONFIG_PATH"] = str(ROOT / "tests/fixtures/config_telegram_only.yaml")
            env["SYNC_STATUS_FILE"] = str(status_file)
            env["SYNC_TRIGGER_FILE"] = str(trigger_file)
            env["ADMIN_WORKER_CAPABILITIES_PATH"] = str(capabilities_file)

            runner = subprocess.Popen(
                [sys.executable, str(ROOT / "sync/runner.py")],
                cwd=temp_path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.monotonic() + 8
                while not child_pid_file.exists() and runner.poll() is None:
                    if time.monotonic() >= deadline:
                        self.fail("runner did not start the sync child")
                    time.sleep(0.05)
                self.assertTrue(child_pid_file.exists(), "sync child did not start")
                child_pid = int(child_pid_file.read_text())
                runner.send_signal(signal.SIGTERM)
                output, _ = runner.communicate(timeout=15)
                self.assertEqual(runner.returncode, 0, output)
                self.assertTrue(status_file.is_file())
                status = json.loads(status_file.read_text())
                self.assertFalse(status["running"])
                self.assertFalse(status["last_success"])
                worker = json.loads(capabilities_file.read_text())
                self.assertFalse(worker["running"])
                self.assertIn("availability_check", worker["actions"])
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
                self.assertEqual(list(temp_path.glob(".sync_status.json.*.tmp")), [])
            finally:
                if runner.poll() is None:
                    runner.kill()
                    runner.wait(timeout=5)

    def test_calculate_next_sleep_seconds_and_format_duration(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("runner_module", ROOT / "sync/runner.py")
        runner_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner_module)
        calculate_next_sleep_seconds = runner_module.calculate_next_sleep_seconds
        format_duration = runner_module.format_duration

        # None returns default interval
        self.assertEqual(calculate_next_sleep_seconds(None, 3600), 3600.0)

        # Returns positive seconds up to 86400
        sec = calculate_next_sleep_seconds("03:30", 3600)
        self.assertGreater(sec, 0)
        self.assertLessEqual(sec, 86400)

        # Test duration formatting
        self.assertEqual(format_duration(45), "45s")
        self.assertEqual(format_duration(3665), "1h 1m 5s")
        self.assertEqual(format_duration(7200), "2h 0m 0s")

    def test_due_admin_schedule_wakes_durable_jobs_worker(self):
        import importlib.util
        from unittest.mock import Mock
        from common import admin_schedules

        spec = importlib.util.spec_from_file_location("runner_schedule_module", ROOT / "sync/runner.py")
        runner_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner_module)

        now = datetime.now(timezone.utc)
        schedule = admin_schedules.create_schedule(
            self.db_url,
            name="Runner integration",
            action="consistency_audit",
            schedule_type="interval",
            interval_minutes=60,
            now=now - timedelta(hours=2),
        )
        with self.conn:
            self.conn.execute(
                "UPDATE admin_schedules SET next_run_at=%s WHERE id=%s",
                (now - timedelta(seconds=1), schedule["id"]),
            )

        runner_module.DB_URL = self.db_url
        runner_module.dispatch_due_schedules = lambda _db_url: admin_schedules.dispatch_due_schedules(
            self.db_url, now=now,
        )
        runner_module.pending_actions = lambda _db_url: []
        runner_module.stop_event.clear()
        worker = Mock(side_effect=runner_module.stop_event.set)
        runner_module.run_jobs_worker = worker
        with tempfile.TemporaryDirectory(prefix="adult_hub_schedule_runner_") as temp:
            runner_module.TRIGGER_FILE = str(Path(temp) / "sync.trigger")
            self.assertFalse(runner_module.wait_with_trigger(duration=5))

        worker.assert_called_once_with()

        scheduled = [s for s in admin_schedules.list_schedules(self.db_url) if s["id"] == schedule["id"]][0]
        self.assertEqual(scheduled["last_result"], "queued")
        job = self.conn.execute(
            "SELECT action,state FROM video_duplicate_jobs WHERE id=%s",
            (scheduled["last_job_id"],),
        ).fetchone()
        self.assertEqual(job, ("consistency_audit", "queued"))

    def test_due_schedule_job_is_recovered_after_runner_restart_without_duplicate(self):
        import importlib.util
        from unittest.mock import Mock
        from common import admin_schedules, job_queue

        now = datetime.now(timezone.utc)
        schedule = admin_schedules.create_schedule(
            self.db_url,
            name="Restart recovery",
            action="consistency_audit",
            schedule_type="interval",
            interval_minutes=60,
            now=now - timedelta(hours=2),
        )
        with self.conn:
            self.conn.execute(
                "UPDATE admin_schedules SET next_run_at=%s WHERE id=%s",
                (now - timedelta(seconds=1), schedule["id"]),
            )

        # The first runner commits the due job and advances the schedule clock,
        # then exits before the worker claims the durable queue row.
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=now), 1)
        before_restart = [s for s in admin_schedules.list_schedules(self.db_url) if s["id"] == schedule["id"]][0]
        job_id = before_restart["last_job_id"]
        self.assertEqual(before_restart["last_result"], "queued")

        # A fresh runner process sees the persisted queue row, wakes its worker,
        # and does not enqueue the same schedule occurrence a second time.
        spec = importlib.util.spec_from_file_location("runner_restarted_module", ROOT / "sync/runner.py")
        runner_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner_module)
        runner_module.DB_URL = self.db_url
        runner_module.dispatch_due_schedules = admin_schedules.dispatch_due_schedules
        runner_module.pending_actions = job_queue.pending_actions
        runner_module.stop_event.clear()
        worker = Mock(side_effect=runner_module.stop_event.set)
        runner_module.run_jobs_worker = worker
        with tempfile.TemporaryDirectory(prefix="adult_hub_schedule_restart_") as temp:
            runner_module.TRIGGER_FILE = str(Path(temp) / "sync.trigger")
            self.assertFalse(runner_module.wait_with_trigger(duration=5))

        worker.assert_called_once_with()
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url), 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)
            ).fetchone()[0],
            "queued",
        )
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 1)

    def test_due_admin_schedules_dispatch_before_initial_full_sync(self):
        import importlib.util
        from unittest.mock import Mock

        spec = importlib.util.spec_from_file_location("runner_startup_schedule_module", ROOT / "sync/runner.py")
        runner_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner_module)
        events = []

        def full_sync(*_args):
            events.append("full_sync")
            runner_module.stop_event.set()
            return True, 0

        runner_module.DB_URL = self.db_url
        runner_module.requeue_processing = None
        runner_module.dispatch_due_schedules = Mock(
            side_effect=lambda _db_url: events.append("dispatch_schedules") or 0
        )
        runner_module.run_pending_sync_jobs = Mock(return_value=False)
        runner_module.run_full_sync = Mock(side_effect=full_sync)
        runner_module.run_on_startup = True
        runner_module.stop_event.clear()

        runner_module.run_loop()

        self.assertEqual(events, ["dispatch_schedules", "full_sync"])
        runner_module.dispatch_due_schedules.assert_called_once_with(self.db_url)

    def test_daily_at_skips_initial_run_when_startup_disabled(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_runner_daily_") as temp:
            temp_path = Path(temp)
            child_pid_file = temp_path / "child.pid"
            status_file = temp_path / "sync_status.json"
            trigger_file = temp_path / "sync.trigger"
            config_file = temp_path / "config.yaml"
            config_file.write_text(
                "sync:\n"
                "  daily_at: '03:30'\n"
                "  run_on_startup: false\n"
                "sources: []\n",
                encoding="utf-8",
            )
            (temp_path / "sync.py").write_text(
                "import os, time\n"
                f"open({str(child_pid_file)!r}, 'w').write(str(os.getpid()))\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            current_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(ROOT), current_pythonpath) if part)
            env["CONFIG_PATH"] = str(config_file)
            env["SYNC_STATUS_FILE"] = str(status_file)
            env["SYNC_TRIGGER_FILE"] = str(trigger_file)

            runner = subprocess.Popen(
                [sys.executable, str(ROOT / "sync/runner.py")],
                cwd=temp_path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                # Wait 1.5s - child should NOT have started
                time.sleep(1.5)
                self.assertFalse(child_pid_file.exists(), "child started despite run_on_startup=false")

                # Now trigger sync via trigger file
                trigger_file.write_text("{}")
                deadline = time.monotonic() + 5
                while not child_pid_file.exists():
                    if time.monotonic() >= deadline:
                        self.fail("runner did not execute sync upon trigger")
                    time.sleep(0.05)
                self.assertTrue(child_pid_file.exists())
            finally:
                runner.terminate()
                runner.wait(timeout=5)

    def test_durable_sync_job_preserves_source_and_mode_after_restart(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_runner_syncjob_") as temp:
            temp_path = Path(temp)
            argv_file = temp_path / "argv.txt"
            status_file = temp_path / "sync_status.json"
            trigger_file = temp_path / "sync.trigger"
            db_path = self.db_url
            config_file = temp_path / "config.yaml"
            config_file.write_text(
                "sync:\n  daily_at: '03:30'\n  run_on_startup: false\nsources: []\n",
                encoding="utf-8",
            )
            (temp_path / "sync.py").write_text(
                "import sys\n"
                f"open({str(argv_file)!r}, 'w').write(' '.join(sys.argv[1:]))\n",
                encoding="utf-8",
            )
            sys.path.insert(0, str(ROOT))
            from common.job_queue import enqueue_job
            from common import db
            job_id = enqueue_job(db_path, "sync", {"source": "Chan", "sync_mode": "backfill"})
            # Simulate a worker crash: the request is left mid-flight.
            with db.connect(db_path) as conn:
                with conn:
                    conn.execute("UPDATE video_duplicate_jobs SET state='processing' WHERE id=%s", (job_id,))

            env = dict(os.environ)
            current_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(ROOT), current_pythonpath) if part)
            env["CONFIG_PATH"] = str(config_file)
            env["SYNC_STATUS_FILE"] = str(status_file)
            env["SYNC_TRIGGER_FILE"] = str(trigger_file)
            env["DB_PATH"] = str(db_path)
            env["DATABASE_URL"] = str(db_path)
            env["TEST_DATABASE_URL"] = str(db_path)

            runner = subprocess.Popen(
                [sys.executable, str(ROOT / "sync/runner.py")],
                cwd=temp_path, env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
            )
            try:
                deadline = time.monotonic() + 8
                while not argv_file.exists():
                    if runner.poll() is not None:
                        self.fail("runner exited before running the durable sync job")
                    if time.monotonic() >= deadline:
                        self.fail("durable sync job did not run without a trigger")
                    time.sleep(0.05)
                self.assertEqual(argv_file.read_text(), "--source Chan --sync-mode backfill")
                deadline = time.monotonic() + 5
                row = None
                while time.monotonic() < deadline:
                    with db.connect(db_path) as conn:
                        row = conn.execute(
                            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
                    if row and row[0] == "done":
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "done")
                result = json.loads(row[1]) if isinstance(row[1], str) else row[1]
                self.assertEqual(result["source"], "Chan")
                self.assertEqual(result["sync_mode"], "backfill")
                self.assertEqual(result["local_state"], "sync completed")
                self.assertEqual(result["exit_code"], 0)
            finally:
                runner.terminate()
                runner.wait(timeout=5)

    def test_idle_durable_queue_survives_missing_or_overwritten_wake(self):
        from common.job_queue import enqueue_job
        from common import db
        for scenario in ("missing_sync_wake", "overwritten_sync_wake", "worker_restart"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                db_url = self.db_url
                marker = root / "calls.jsonl"
                trigger = root / "sync.trigger"
                config = root / "config.yaml"
                config.write_text("sync:\n  interval_minutes: 1440\n  run_on_startup: false\nsources: []\n")
                (root / "sync.py").write_text(
                    "import json, os, sys\n"
                    "for p in os.environ.get('PYTHONPATH', '').split(os.pathsep):\n"
                    "    if p and p not in sys.path: sys.path.insert(0, p)\n"
                    "from common import db\n"
                    f"with open({str(marker)!r}, 'a') as handle: handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                    "if '--jobs-only' in sys.argv:\n"
                    "    with db.connect(os.environ.get('DATABASE_URL') or os.environ.get('DB_PATH')) as conn:\n"
                    "        with conn:\n"
                    "            conn.execute(\"UPDATE video_duplicate_jobs SET state='done' WHERE action<>'sync' AND state IN ('queued','processing')\")\n"
                )
                if scenario == "worker_restart":
                    job_id = enqueue_job(db_url, "performer_merge", {"merges": []})
                    with db.connect(db_url) as conn:
                        with conn:
                            conn.execute("UPDATE video_duplicate_jobs SET state='processing' WHERE id=%s", (job_id,))
                current_pythonpath = os.environ.get("PYTHONPATH", "")
                pythonpath = os.pathsep.join(
                    part for part in (str(ROOT), current_pythonpath) if part)
                env = dict(os.environ, PYTHONPATH=pythonpath, CONFIG_PATH=str(config),
                           DB_PATH=str(db_url), DATABASE_URL=str(db_url), TEST_DATABASE_URL=str(db_url),
                           SYNC_TRIGGER_FILE=str(trigger),
                           SYNC_STATUS_FILE=str(root / "status.json"))
                runner = subprocess.Popen([sys.executable, str(ROOT / "sync/runner.py")],
                    cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, text=True)
                try:
                    if scenario != "worker_restart":
                        # Enqueue during a long idle interval, after startup.
                        time.sleep(0.4)
                        self.assertFalse(marker.exists())
                        job_id = enqueue_job(db_url, "sync", {"source": "Chan"})
                        if scenario == "overwritten_sync_wake":
                            trigger.write_text(json.dumps({"action": "sync"}))
                            trigger.write_text(json.dumps({"action": "jobs"}))
                    deadline = time.monotonic() + 8
                    state = None
                    while time.monotonic() < deadline:
                        if runner.poll() is not None:
                            self.fail("runner exited before durable work completed")
                        with db.connect(db_url) as conn:
                            state = conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0]
                        if state == "done":
                            break
                        time.sleep(0.05)
                    self.assertEqual(state, "done")
                    time.sleep(0.3)
                    calls = [json.loads(line) for line in marker.read_text().splitlines()]
                    if scenario == "worker_restart":
                        self.assertEqual(calls, [["--jobs-only"]])
                    else:
                        self.assertEqual([call for call in calls if call != ["--jobs-only"]], [["--source", "Chan"]])
                finally:
                    runner.terminate()
                    runner.wait(timeout=5)


    def test_jobs_trigger_runs_jobs_only_and_does_not_trigger_full_sync(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_runner_jobs_") as temp:
            temp_path = Path(temp)
            child_pid_file = temp_path / "child.pid"
            jobs_marker_file = temp_path / "jobs_ran.txt"
            status_file = temp_path / "sync_status.json"
            trigger_file = temp_path / "sync.trigger"
            config_file = temp_path / "config.yaml"
            config_file.write_text(
                "sync:\n"
                "  daily_at: '03:30'\n"
                "  run_on_startup: false\n"
                "sources: []\n",
                encoding="utf-8",
            )
            (temp_path / "sync.py").write_text(
                "import os, sys\n"
                "if '--jobs-only' in sys.argv:\n"
                f"    open({str(jobs_marker_file)!r}, 'w').write('jobs_ok')\n"
                "    sys.exit(0)\n"
                f"open({str(child_pid_file)!r}, 'w').write(str(os.getpid()))\n",
                encoding="utf-8",
            )
            env = dict(os.environ)
            current_pythonpath = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = os.pathsep.join(
                part for part in (str(ROOT), current_pythonpath) if part)
            env["CONFIG_PATH"] = str(config_file)
            env["SYNC_STATUS_FILE"] = str(status_file)
            env["SYNC_TRIGGER_FILE"] = str(trigger_file)

            runner = subprocess.Popen(
                [sys.executable, str(ROOT / "sync/runner.py")],
                cwd=temp_path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                time.sleep(1.0)
                self.assertFalse(child_pid_file.exists())
                self.assertFalse(jobs_marker_file.exists())

                trigger_file.write_text(json.dumps({"action": "jobs"}))
                deadline = time.monotonic() + 5
                while not jobs_marker_file.exists():
                    if time.monotonic() >= deadline:
                        self.fail("runner did not execute jobs-only worker on jobs trigger")
                    time.sleep(0.05)

                self.assertTrue(jobs_marker_file.exists())
                self.assertEqual(jobs_marker_file.read_text(), "jobs_ok")
                self.assertFalse(trigger_file.exists())

                time.sleep(1.0)
                self.assertFalse(child_pid_file.exists(), "runner launched full sync child on jobs trigger!")
            finally:
                runner.terminate()
                runner.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
