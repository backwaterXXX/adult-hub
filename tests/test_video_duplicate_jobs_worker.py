import ast
import fcntl
import inspect
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import sync.duplicate_jobs as duplicate_jobs
from sync.duplicate_jobs import _heartbeat_processing_job
from resolver.video_duplicates import enqueue_job
from common.job_queue import retry_job
from tests.test_video_duplicate_jobs import BaseVideoDuplicateJobsTestCase, sync


class TestWorkerActionDispatchContract(unittest.TestCase):
    def test_every_advertised_worker_action_has_a_dispatch_branch(self):
        from common.job_queue import WORKER_ACTIONS

        # The dispatch branches live in _execute_action, not in the claim/run loop.
        worker = sys.modules[duplicate_jobs.process_video_duplicate_jobs_locked.__module__]
        function = ast.parse(inspect.getsource(worker._execute_action))
        dispatched = {
            node.comparators[0].value
            for node in ast.walk(function)
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "action"
            and node.comparators
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.comparators[0].value, str)
        }
        self.assertEqual(dispatched, set(WORKER_ACTIONS))



class TestVideoDuplicateJobsWorker(BaseVideoDuplicateJobsTestCase):
    def test_metadata_backfill_is_excluded_from_sync_checkpoints_but_runs_in_jobs_worker(self):
        job_id = enqueue_job(self.db_url, "metadata_backfill", {})
        progress = []

        def scan(conn, media_dir, report_progress):
            self.assertIs(conn, self.conn)
            self.assertEqual(media_dir, self.media)
            report_progress(0, 5)
            report_progress(5, 5)
            return {
                "providers_completed": 5,
                "providers_failed": 0,
                "entities_checked": 125,
                "entities_enriched": 28,
            }

        with patch.object(sync, "_run_external_metadata_backfill", side_effect=scan), \
             patch.object(duplicate_jobs, "_record_job_progress", wraps=duplicate_jobs._record_job_progress) as record:
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
            self.assertEqual(self.conn.execute(
                "SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)
            ).fetchone()[0], "queued")
            self.assertEqual(
                sync.process_video_duplicate_jobs(self.conn, self.media, include_long_running=True), 1
            )
            progress = [call.args[2] for call in record.call_args_list if call.args[2] == "scanning_metadata"]

        self.assertEqual(progress, ["scanning_metadata", "scanning_metadata"])
        state, raw_result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)
        ).fetchone()
        self.assertEqual(state, "done")
        result = json.loads(raw_result)
        self.assertEqual(result["local_state"], "metadata_scan_completed")
        self.assertEqual(result["entities_checked"], 125)
        self.assertEqual(result["entities_enriched"], 28)


    def test_processing_job_heartbeat_refreshes_activity_without_mutating_result(self):
        job_id = "heartbeat-" + self.keeper.rsplit(":", 1)[-1]
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                "VALUES (%s,'backup_create','processing','{}','{}',1,1)", (job_id,)
            )
        stop_event = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat_processing_job,
            args=(self.db_url, job_id, stop_event, sync.logger, 0.02), daemon=True,
        )
        heartbeat.start()
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                row = self.conn.execute("SELECT updated_at,result FROM video_duplicate_jobs WHERE id=%s",
                                        (job_id,)).fetchone()
                if row[0] > 1:
                    break
                time.sleep(0.02)
        finally:
            stop_event.set()
            heartbeat.join(timeout=2)
        self.assertGreater(row[0], 1)
        self.assertEqual(row[1], "{}")


    def test_metadata_update_keeps_source_and_retries_jellyfin_after_local_nfo_write(self):
        payload = {
            "video_id": self.loser,
            "expected_revision": 0,
            "changes": {"title": "Manual title", "plot": "", "studio": "Manual studio",
                        "performers": ["Manual actor"], "tags": ["Manual tag"],
                        "date_released": "2026-02-03"},
            "reset_fields": [],
        }
        job_id = enqueue_job(self.db_path, "metadata_update", payload)
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False) as refresh:
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
        self.assertEqual(refresh.call_count, 1)
        state, raw_result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)
        ).fetchone()
        self.assertEqual(state, "failed")
        failed_result = json.loads(raw_result)
        self.assertEqual(failed_result["local_state"], "metadata_updated")
        self.assertEqual(failed_result["jellyfin_refresh"], "failed")
        self.assertEqual(failed_result["jellyfin_import"], "not_requested")
        self.assertTrue(failed_result["nfo_written"])

        source_title = self.conn.execute("SELECT title FROM videos WHERE id=%s", (self.loser,)).fetchone()[0]
        self.assertEqual(source_title, self.loser)
        override = self.conn.execute(
            "SELECT fields,revision FROM video_metadata_overrides WHERE video_id=%s", (self.loser,)
        ).fetchone()
        self.assertEqual(json.loads(override[0]), {
            "title": "Manual title", "plot": "", "studio": "Manual studio",
            "performers": ["Manual actor"], "tags": ["Manual tag"],
            "date_released": "2026-02-03",
        })
        self.assertEqual(override[1], 1)
        audit = self.conn.execute(
            "SELECT event_type,object_type,object_id,job_id,changes FROM admin_audit_events WHERE job_id=%s",
            (job_id,),
        ).fetchone()
        self.assertEqual(audit[0:4], ("metadata_update", "video", self.loser, job_id))
        audit_changes = audit[4] if isinstance(audit[4], dict) else json.loads(audit[4])
        self.assertEqual(audit_changes["title"], {
            "before": self.loser, "after": "Manual title",
            "before_overridden": False, "after_overridden": True,
        })
        self.assertEqual(audit_changes["plot"]["before"], None)
        self.assertEqual(audit_changes["plot"]["after"], "")
        self.assertEqual(set(audit_changes), {
            "title", "plot", "studio", "performers", "tags", "date_released",
        })
        nfo_path = self.media / f"{sync.stable_media_basename(self.loser)}.nfo"
        nfo_text = nfo_path.read_text(encoding="utf-8")
        self.assertIn("Manual title", nfo_text)
        self.assertIn("Manual studio", nfo_text)
        self.assertIn("Manual actor", nfo_text)
        self.assertIn("Manual tag", nfo_text)
        self.assertIn("2026-02-03", nfo_text)
        self.assertNotIn(self.loser, nfo_text)

        retry_id = retry_job(self.db_path, job_id)
        self.assertIsNotNone(retry_id)
        with patch.object(sync, "create_nfo_file", wraps=sync.create_nfo_file) as nfo_writer, \
                patch.object(sync, "trigger_jellyfin_refresh", return_value=True) as refresh:
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(nfo_writer.call_count, 0, "Retry should resume at Jellyfin after a successful NFO write.")
        result = json.loads(self.conn.execute(
            "SELECT result FROM video_duplicate_jobs WHERE id=%s", (retry_id,)
        ).fetchone()[0])
        self.assertEqual(result["metadata_revision"], 1)
        self.assertEqual(result["jellyfin_refresh"], "updated")
        self.assertEqual(result["jellyfin_import"], "not_confirmed")
        self.assertEqual(self.conn.execute(
            "SELECT revision FROM video_metadata_overrides WHERE video_id=%s", (self.loser,)
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM admin_audit_events WHERE job_id=%s", (job_id,)
        ).fetchone()[0], 1, "Resuming after the local commit must not duplicate the change event.")


    def test_source_sync_updates_raw_title_but_keeps_manual_nfo_title(self):
        from common.video_metadata import update_metadata_state

        with self.conn:
            result = update_metadata_state(
                self.conn, self.loser, 0, {"title": "Manual title"}, []
            )
            self.assertEqual(result["outcome"], "saved")
        sync.sync_unique_video({
            "id": self.loser,
            "provider": "telegram",
            "provider_video_id": self.loser[len("telegram:"):],
            "title": "Updated source title",
            "duration": 600,
            "performers": [],
        }, set(), set(), self.media, self.conn)

        self.assertEqual(
            self.conn.execute("SELECT title FROM videos WHERE id=%s", (self.loser,)).fetchone()[0],
            "Updated source title",
        )
        nfo_path = self.media / f"{sync.stable_media_basename(self.loser)}.nfo"
        self.assertIn("Manual title", nfo_path.read_text(encoding="utf-8"))
        self.assertNotIn("Updated source title", nfo_path.read_text(encoding="utf-8"))


    def test_availability_check_records_evidence_and_only_changes_confirmed_results(self):
        from common.job_queue import enqueue_job
        with self.conn:
            self.conn.execute(
                "INSERT INTO dead_streams (video_id,reason,detected_at) VALUES (%s,'old failure',1)",
                (self.keeper,),
            )
        job_id = enqueue_job(self.db_path, "availability_check", {"video_ids": [self.keeper, self.loser]})
        with patch.object(sync, "probe_stream_via_resolver_outcome", side_effect=["available", "unknown"]):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        checks = self.conn.execute(
            "SELECT video_id,status,evidence FROM video_availability_checks WHERE job_id=%s ORDER BY video_id",
            (job_id,),
        ).fetchall()
        self.assertEqual({(row[0], row[1]) for row in checks}, {
            (self.keeper, "available"), (self.loser, "unknown"),
        })
        self.assertTrue(all(row[2] for row in checks))
        self.assertEqual(self.conn.execute("SELECT playable FROM videos WHERE id=%s", (self.keeper,)).fetchone()[0], 1)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM dead_streams WHERE video_id=%s", (self.keeper,)).fetchone())
        self.assertIsNone(self.conn.execute("SELECT playable FROM videos WHERE id=%s", (self.loser,)).fetchone()[0])
        transient_job = enqueue_job(self.db_path, "availability_check", {"video_ids": [self.loser]})
        with patch.object(sync, "probe_stream_via_resolver_outcome", return_value="transient_error"):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        transient = self.conn.execute(
            "SELECT status FROM video_availability_checks WHERE job_id=%s AND video_id=%s",
            (transient_job, self.loser),
        ).fetchone()
        self.assertEqual(transient[0], "transient_error")
        self.assertIsNone(self.conn.execute("SELECT playable FROM videos WHERE id=%s", (self.loser,)).fetchone()[0])


    def test_availability_check_restart_reuses_job_without_duplicate_findings(self):
        from common.job_queue import enqueue_job

        job_id = enqueue_job(self.db_path, "availability_check", {"video_ids": [self.keeper, self.loser]})
        with patch.object(sync, "probe_stream_via_resolver_outcome",
                          side_effect=["available", KeyboardInterrupt("simulated worker restart")]):
            with self.assertRaises(KeyboardInterrupt):
                sync.process_video_duplicate_jobs(self.conn, self.media)

        partial = self.conn.execute(
            "SELECT video_id,status FROM video_availability_checks WHERE job_id=%s",
            (job_id,),
        ).fetchall()
        self.assertEqual(partial, [(self.keeper, "available")])
        self.assertEqual(
            self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0],
            "processing",
        )

        with patch.object(sync, "probe_stream_via_resolver_outcome", side_effect=["available", "unknown"]):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)

        final_rows = self.conn.execute(
            "SELECT video_id,status FROM video_availability_checks WHERE job_id=%s ORDER BY video_id",
            (job_id,),
        ).fetchall()
        self.assertEqual({(row[0], row[1]) for row in final_rows}, {
            (self.keeper, "available"), (self.loser, "unknown"),
        })
        self.assertEqual(len(final_rows), 2)
        self.assertEqual(
            self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0],
            "done",
        )
        self.assertEqual(
            self.conn.execute("SELECT playable FROM videos WHERE id=%s", (self.keeper,)).fetchone()[0], 1,
        )
        self.assertIsNone(
            self.conn.execute("SELECT playable FROM videos WHERE id=%s", (self.loser,)).fetchone()[0],
        )


    def test_rate_limited_availability_probe_preserves_confirmed_unavailable_state(self):
        from common.job_queue import enqueue_job

        video_id = "telegram:-1001:3:rate-limited"
        self._add_video(video_id)
        with self.conn:
            self.conn.execute("UPDATE videos SET playable=0 WHERE id=%s", (video_id,))
            self.conn.execute(
                "INSERT INTO dead_streams(video_id,reason,detected_at) VALUES (%s,'confirmed earlier',122)",
                (video_id,),
            )
        job_id = enqueue_job(self.db_path, "availability_check", {"video_ids": [video_id]})
        with patch.object(sync, "probe_stream_via_resolver_outcome", return_value="transient_error"):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)

        check = self.conn.execute(
            "SELECT status,evidence FROM video_availability_checks WHERE job_id=%s AND video_id=%s",
            (job_id, video_id),
        ).fetchone()
        self.assertEqual(check[0], "transient_error")
        self.assertIn("transient network", check[1])
        self.assertEqual(self.conn.execute(
            "SELECT playable FROM videos WHERE id=%s", (video_id,),
        ).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT reason FROM dead_streams WHERE video_id=%s", (video_id,),
        ).fetchone(), ("confirmed earlier",))


    def test_backup_restore_test_worker_passes_only_a_checked_backup_path(self):
        backup_root = self.root / "backups"
        backup_root.mkdir()
        backup = backup_root / "adulthub-test.dump.gz"
        backup.write_bytes(b"archive")
        job_id = enqueue_job(self.db_path, "backup_restore_test", {"filename": backup.name})
        with patch.dict(os.environ, {"BACKUP_DIR": str(backup_root),
                                     "BACKUP_RESTORE_DATABASE_URL": "postgresql://user:secret@localhost:5432/maintenance"}), \
             patch.object(sync.db, "pg_restore_test", return_value={"outcome": "passed", "catalog_rows": 7}) as restore:
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        state, result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)
        ).fetchone()
        self.assertEqual(state, "done")
        self.assertEqual(json.loads(result)["restore_test"], True)
        self.assertEqual(restore.call_args.args[0], backup.resolve())
        self.assertEqual(restore.call_args.args[1], "postgresql://user:secret@localhost:5432/maintenance")
        self.assertEqual(restore.call_args.kwargs["job_id"], job_id)
        self.assertNotIn("database_url", json.dumps(json.loads(result)))


    def test_backup_create_worker_records_only_safe_backup_completion(self):
        backup_root = self.root / "backups"
        backup_root.mkdir()
        expected = backup_root / "adulthub-test.dump.gz"
        job_id = enqueue_job(self.db_path, "backup_create", {})
        with patch.dict(os.environ, {"BACKUP_DIR": str(backup_root), "DATABASE_URL": "not-used"}), \
             patch.object(sync.db, "pg_backup", return_value=str(expected)) as create_backup:
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)

        state, result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)
        ).fetchone()
        payload = json.loads(result)
        self.assertEqual(state, "done")
        self.assertTrue(payload["backup_created"])
        self.assertEqual(payload["backup_name"], expected.name)
        self.assertNotIn(str(backup_root), result)
        create_backup.assert_called_once_with(self.db_path, str(backup_root), job_id=job_id)


    def test_metadata_worker_rejects_source_changes_after_plan_execution(self):
        from common.admin_operations import execute_operation, preview_metadata_plan
        plan = preview_metadata_plan(self.db_path, self.loser, 0, {"title": "Manual title"}, [])
        queued = execute_operation(self.db_path, plan["plan_id"])
        job_id = queued["jobs"][0]["job_id"]
        with self.conn:
            self.conn.execute("UPDATE videos SET plot='Changed source description' WHERE id=%s", (self.loser,))
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
        job = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        self.assertEqual(job[0], "failed")
        self.assertIn("source values changed after the preview", json.loads(job[1])["error"])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM video_metadata_overrides WHERE video_id=%s", (self.loser,)).fetchone())


    def test_confirmed_jellyfin_nfo_hash_updates_completed_metadata_job(self):
        job_id = enqueue_job(self.db_path, "metadata_update", {"video_id": self.loser})
        nfo_path = self.media / (sync.stable_media_basename(self.loser) + ".nfo")
        digest = sync.hashlib.sha256(nfo_path.read_bytes()).hexdigest()
        with self.conn:
            self.conn.execute("UPDATE video_duplicate_jobs SET state='done',result=%s WHERE id=%s",
                              (json.dumps({"local_state": "metadata_updated", "nfo_written": True,
                                           "jellyfin_import": "not_confirmed",
                                           "nfo_content_hashes": {nfo_path.name: digest}}), job_id))
        old_job_id = enqueue_job(self.db_path, "metadata_update", {"video_id": self.loser})
        with self.conn:
            self.conn.execute("UPDATE video_duplicate_jobs SET state='done',result=%s WHERE id=%s",
                              (json.dumps({"local_state": "metadata_updated", "nfo_written": True,
                                           "jellyfin_import": "not_confirmed",
                                           "nfo_content_hashes": {nfo_path.name: "old-hash"}}), old_job_id))
        filename = sync.stable_media_basename(self.loser) + ".strm"
        sync._confirm_metadata_import_jobs({filename: digest}, self.db_path)
        result = json.loads(self.conn.execute(
            "SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)
        ).fetchone()[0])
        self.assertEqual(result["jellyfin_import"], "confirmed")
        self.assertIsInstance(result["jellyfin_import_confirmed_at"], int)
        old_result = json.loads(self.conn.execute(
            "SELECT result FROM video_duplicate_jobs WHERE id=%s", (old_job_id,)
        ).fetchone()[0])
        self.assertEqual(old_result["jellyfin_import"], "not_confirmed")


    def test_performer_merge_job_recovery_and_single_worker_lock(self):
        job_id = enqueue_job(self.db_path, "performer_merge", {
            "merges": [{"alias": "Alias Name", "canonical": "Canonical Name"}],
        })
        self.conn.execute("UPDATE video_duplicate_jobs SET state='processing' WHERE id=%s", (job_id,))
        self.conn.commit()

        lock_path = self.root / ".video_duplicate_jobs.lock"
        with lock_path.open("a") as other_worker:
            fcntl.flock(other_worker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
            self.assertEqual(self.conn.execute(
                "SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0], "processing")
            fcntl.flock(other_worker.fileno(), fcntl.LOCK_UN)

        with patch("sync.fuzzy_performers.execute_performer_merges", return_value={
                "ok": True, "videos_updated": 1, "nfos_updated": 1, "message": "merged"}) as execute:
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        execute.assert_called_once_with(self.db_path, self.media, [("Alias Name", "Canonical Name")])
        state, result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        self.assertEqual(state, "done")
        self.assertEqual(json.loads(result)["message"], "merged")


    def test_revision_bound_performer_merge_rejects_stale_catalog_before_mutation(self):
        job_id = enqueue_job(self.db_path, "performer_merge", {
            "expected_revision": "0" * 64,
            "merges": [{"alias": "Alias Name", "canonical": "Canonical Name"}],
        })
        with patch("sync.fuzzy_performers.execute_performer_merges") as execute:
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
        execute.assert_not_called()
        state, result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        self.assertEqual(state, "failed")
        self.assertIn("changed after this preview", json.loads(result)["error"])


    def test_revision_bound_performer_merge_recovers_after_merge_stage_started(self):
        job_id = enqueue_job(self.db_path, "performer_merge", {
            "expected_revision": "0" * 64,
            "merges": [{"alias": "Alias Name", "canonical": "Canonical Name"}],
        })
        with self.conn:
            self.conn.execute(
                "UPDATE video_duplicate_jobs SET state='processing',result=%s WHERE id=%s",
                (json.dumps({"progress": {"stage": "merging_performers"}}), job_id),
            )
        with patch("sync.fuzzy_performers.execute_performer_merges", return_value={
                "ok": True, "videos_updated": 1, "nfos_updated": 1, "message": "merged"}) as execute:
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        execute.assert_called_once_with(
            self.db_path, self.media, [("Alias Name", "Canonical Name")])
        state, result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        self.assertEqual(state, "done")
        self.assertEqual(json.loads(result)["message"], "merged")


    def test_collection_job_recovers_processing_obeys_lock_and_runs_once(self):
        job_id = enqueue_job(self.db_path, "collections_refresh", {})
        self.conn.execute("UPDATE video_duplicate_jobs SET state='processing' WHERE id=%s", (job_id,))
        self.conn.commit()
        result = {"collections": [{"name": "Adult Hub · Test", "items": 2}], "matched_videos": 2}
        refresh = AsyncMock(return_value=result)
        with patch.dict(os.environ, {"JELLYFIN_URL": "http://jellyfin/", "JELLYFIN_API_KEY": "environment-secret"}), \
             patch("common.managed_collections.refresh_managed_collections", refresh):
            with (self.root / ".video_duplicate_jobs.lock").open("a") as other_worker:
                fcntl.flock(other_worker.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
                refresh.assert_not_called()
                fcntl.flock(other_worker.fileno(), fcntl.LOCK_UN)
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
        refresh.assert_awaited_once()
        args = refresh.await_args.args
        self.assertEqual(args[1:], ("http://jellyfin", "environment-secret", self.db_path))
        state, payload, result_raw = self.conn.execute("SELECT state,payload,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        self.assertEqual(state, "done")
        self.assertEqual(json.loads(result_raw)["matched_videos"], 2)
        self.assertEqual(json.loads(result_raw)["collections"], result["collections"])
        self.assertEqual(json.loads(payload), {})
        self.assertNotIn("environment-secret", result_raw)


    def test_collection_worker_rechecks_config_and_marks_job_failed(self):
        job_id = enqueue_job(self.db_path, "collections_refresh", {})
        with patch.dict(os.environ, {"JELLYFIN_URL": "", "JELLYFIN_API_KEY": ""}), \
             patch("common.managed_collections.refresh_managed_collections", new_callable=AsyncMock) as refresh:
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
            refresh.assert_not_called()
        state, result = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        self.assertEqual(state, "failed")
        self.assertIn("error", json.loads(result))


    def test_collection_remote_failure_persists_then_new_request_can_retry(self):
        job_id = enqueue_job(self.db_path, "collections_refresh", {})
        with patch.dict(os.environ, {"JELLYFIN_URL": "http://jellyfin", "JELLYFIN_API_KEY": "secret"}), \
             patch("common.managed_collections.refresh_managed_collections", new_callable=AsyncMock) as refresh:
            refresh.side_effect = RuntimeError("temporary upstream failure")
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
            self.assertEqual(self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0], "failed")
            retry = enqueue_job(self.db_path, "collections_refresh", {})
            self.assertNotEqual(retry, job_id)
            refresh.side_effect = None
            refresh.return_value = {"collections": [], "matched_videos": 0}
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
            self.assertEqual(self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (retry,)).fetchone()[0], "done")
        self.assertEqual(refresh.await_count, 2)


    def test_safe_checkpoint_skips_when_collected_not_empty(self):
        job_id = enqueue_job(self.db_path, "performer_merge", {
            "merges": [{"alias": "Alias Name", "canonical": "Canonical Name"}],
        })
        collected = {"some_video_id": {"video": {"id": "some_video_id"}}}
        with patch.object(sync, "process_video_duplicate_jobs") as mock_proc:
            completed = sync.safe_process_duplicate_jobs_checkpoint(self.conn, self.media, collected=collected)
            self.assertEqual(completed, 0)
            mock_proc.assert_not_called()
        self.assertEqual(
            self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0],
            "queued"
        )


    def test_safe_checkpoint_executes_when_collected_empty_and_cleans_jobs_trigger(self):
        job_id = enqueue_job(self.db_path, "performer_merge", {
            "merges": [{"alias": "Alias Name", "canonical": "Canonical Name"}],
        })
        trigger_path = self.root / "sync.trigger"
        trigger_path.write_text(json.dumps({"action": "jobs"}))

        with patch.dict(os.environ, {"SYNC_TRIGGER_FILE": str(trigger_path)}):
            with patch("sync.fuzzy_performers.execute_performer_merges", return_value={"ok": True, "videos_updated": 1, "nfos_updated": 1, "message": "merged"}):
                completed = sync.safe_process_duplicate_jobs_checkpoint(self.conn, self.media, collected={})
                self.assertEqual(completed, 1)

        self.assertEqual(
            self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0],
            "done"
        )
        self.assertFalse(trigger_path.exists())


    def test_run_jobs_only_obeys_sync_lock(self):
        job_id = enqueue_job(self.db_path, "performer_merge", {
            "merges": [{"alias": "Alias Name", "canonical": "Canonical Name"}],
        })
        sync_lock_path = self.root / ".adult-hub-sync.lock"

        with sync_lock_path.open("a") as sync_lock:
            fcntl.flock(sync_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(sync, "DATA_DIR", self.root), \
                 patch.object(sync, "DB_URL", self.db_path), \
                 patch.object(sync, "MEDIA_DIR", self.media):
                res = sync.run_jobs_only()
                self.assertEqual(res, 0)
            fcntl.flock(sync_lock.fileno(), fcntl.LOCK_UN)

        self.assertEqual(
            self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0],
            "queued"
        )

        with patch.object(sync, "DATA_DIR", self.root), \
             patch.object(sync, "DB_URL", self.db_path), \
             patch.object(sync, "MEDIA_DIR", self.media), \
             patch("sync.fuzzy_performers.execute_performer_merges", return_value={"ok": True, "videos_updated": 1, "nfos_updated": 1, "message": "merged"}):
            res = sync.run_jobs_only()
            self.assertEqual(res, 1)

        self.assertEqual(
            self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0],
            "done"
        )


    def test_blocked_channel_readd_does_not_stall_other_queued_jobs(self):
        readd_id = enqueue_job(self.db_url, "source_readd", {"channel_id": "-100777"})
        enqueue_job(self.db_url, "source_remove", {"channel_id": "-100777"})
        quarantine_id = enqueue_job(self.db_path, "quarantine", {"video_id": self.loser})
        with self.conn:
            self.conn.execute(
                "UPDATE video_duplicate_jobs SET created_at = created_at - INTERVAL '1 hour' WHERE id=%s",
                (readd_id,),
            )

        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)

        states = dict(self.conn.execute(
            "SELECT id, state FROM video_duplicate_jobs WHERE id = ANY(%s)",
            ([readd_id, quarantine_id],)).fetchall())
        self.assertEqual(states[readd_id], "queued")
        self.assertEqual(states[quarantine_id], "done")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())



class TestScheduledReconcileMergeGate(unittest.TestCase):
    """A scheduled reconciliation queues hides only after an explicit opt-in."""

    def execute_merges_argument(self) -> tuple[bool, bool]:
        import sync.duplicate_job_external_handlers as duplicate_job_external_handlers
        import sync.video_duplicate_automation as video_duplicate_automation

        with patch.object(video_duplicate_automation, "reconcile",
                          return_value={"outcome": "completed"}) as reconcile:
            result, committed = duplicate_job_external_handlers.execute_external_operation(
                "duplicate_reconcile", None, "job-1", {}, Path("/nonexistent-media-dir"), MagicMock())
        self.assertTrue(committed)
        self.assertEqual(result["local_state"], "duplicate_reconciliation_completed")
        return (
            reconcile.call_args.kwargs["execute_merges"],
            reconcile.call_args.kwargs.get("merge_telegram_file_id", True),
        )

    def test_reports_candidates_when_the_flag_is_unset(self):
        with patch.dict(os.environ):
            os.environ.pop("DUPLICATE_AUTO_MERGE_ENABLED", None)
            execute_merges, merge_tg = self.execute_merges_argument()
            self.assertFalse(execute_merges)
            self.assertTrue(merge_tg)

        import sync.video_duplicate_automation as video_duplicate_automation
        fake_scan = {
            "complete": True,
            "scanned": 2,
            "pairs": [{
                "evidence": "telegram_file_id",
                "left": {"id": "telegram:1:1:aaa"},
                "right": {"id": "telegram:1:2:aaa"},
                "members": [{"id": "telegram:1:1:aaa"}, {"id": "telegram:1:2:aaa"}],
                "score": 100.0,
                "category": "Same file",
            }],
        }
        auto_groups = {
            ("telegram:1:1:aaa", ("telegram:1:2:aaa",)): {
                "keeper_id": "telegram:1:1:aaa",
                "loser_ids": ["telegram:1:2:aaa"],
                "evidence": "telegram_file_id",
            }
        }
        with patch("resolver.video_duplicates.scan_duplicate_index", return_value=fake_scan), \
             patch.object(video_duplicate_automation, "_reconcile_visual", return_value={}), \
             patch.object(video_duplicate_automation, "_load_reconcile_state", return_value=({}, {}, set(), [])), \
             patch.object(video_duplicate_automation, "_enqueue_missing_phashes", return_value=0), \
             patch.object(video_duplicate_automation, "_build_auto_groups", return_value=(auto_groups, 0)), \
             patch.object(video_duplicate_automation, "_filter_work_by_scale_guard", side_effect=lambda db_url, work: work), \
             patch("common.job_queue.enqueue_jobs_batch", return_value=[]) as mock_enqueue:
            with patch.dict(os.environ):
                os.environ.pop("DUPLICATE_AUTO_MERGE_ENABLED", None)
                res = video_duplicate_automation.reconcile(None, execute_merges=False)
                mock_enqueue.assert_called_once_with(None, [])
                self.assertEqual(res["mode"], "report")
                self.assertEqual(res["merge_jobs_queued"], 0)

    def test_operator_opt_in_queues_automatic_merges(self):
        with patch.dict(os.environ, {"DUPLICATE_AUTO_MERGE_ENABLED": "true"}):
            execute_merges, merge_tg = self.execute_merges_argument()
            self.assertTrue(execute_merges)
            self.assertTrue(merge_tg)



if __name__ == "__main__":
    unittest.main()
