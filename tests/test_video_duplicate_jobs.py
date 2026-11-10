import ast
import inspect
import json
import fcntl
import os
import sys
import tempfile
import threading
import time
import uuid
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from unittest.mock import patch, AsyncMock, MagicMock
from pathlib import Path

from pg_helper import PgTestCase


from test_support import load_sync_module
sync = load_sync_module()
import sync.duplicate_jobs as duplicate_jobs
from sync.duplicate_jobs import _heartbeat_processing_job
from resolver.video_duplicates import enqueue_job
from common.job_queue import retry_job
from common.admin_operations import (execute_operation, preview_audit_import_refresh, preview_audit_repair,
                                     preview_orphan_file_quarantine, preview_orphan_file_restore)


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


class TestAuditNfoMetadataDifferences(unittest.TestCase):
    def test_only_allowlisted_field_labels_are_reported(self):
        actual = ET.fromstring(
            "<movie><title>Private actual title</title><plot>Private actual plot</plot></movie>"
        )
        expected = ET.fromstring(
            "<movie><title>Private expected title</title><plot>Private expected plot</plot></movie>"
        )
        differences = duplicate_jobs._audit_nfo_metadata_differences(actual, expected)
        self.assertEqual(differences, ["title", "plot"])
        self.assertNotIn("Private", " ".join(differences))

    def test_xml_tag_order_and_outer_whitespace_do_not_change_fields(self):
        actual = ET.fromstring(
            "<movie><title> Example\r\n</title><genre> Drama </genre><tag> One </tag></movie>"
        )
        expected = ET.fromstring(
            "<movie><tag>One</tag><genre>Drama</genre><title>Example</title></movie>"
        )
        self.assertEqual(duplicate_jobs._audit_nfo_metadata_differences(actual, expected), [])

    def test_unordered_metadata_values_ignore_xml_element_order(self):
        actual = ET.fromstring(
            "<movie><studio>Studio B</studio><studio>Studio A</studio>"
            "<genre>Drama</genre><genre>Comedy</genre><tag>Performer B</tag><tag>Performer A</tag>"
            "<set><name>Collection B</name></set><set><name>Collection A</name></set></movie>"
        )
        expected = ET.fromstring(
            "<movie><studio>Studio A</studio><studio>Studio B</studio>"
            "<genre>Comedy</genre><genre>Drama</genre><tag>Performer A</tag><tag>Performer B</tag>"
            "<set><name>Collection A</name></set><set><name>Collection B</name></set></movie>"
        )
        self.assertEqual(duplicate_jobs._audit_nfo_metadata_differences(actual, expected), [])

    def test_cast_order_remains_significant(self):
        actual = ET.fromstring(
            "<movie><actor><name>Performer A</name><role>Lead</role></actor>"
            "<actor><name>Performer B</name><role>Lead</role></actor></movie>"
        )
        expected = ET.fromstring(
            "<movie><actor><name>Performer B</name><role>Lead</role></actor>"
            "<actor><name>Performer A</name><role>Lead</role></actor></movie>"
        )
        self.assertEqual(duplicate_jobs._audit_nfo_metadata_differences(actual, expected), ["cast"])

    def test_collection_and_cast_changes_have_named_labels_only(self):
        actual = ET.fromstring(
            "<movie><set><name>Private collection</name></set>"
            "<actor><name>Private performer</name><role>Lead</role></actor></movie>"
        )
        expected = ET.fromstring(
            "<movie><set><name>Different collection</name></set>"
            "<actor><name>Different performer</name><role>Lead</role></actor></movie>"
        )
        differences = duplicate_jobs._audit_nfo_metadata_differences(actual, expected)
        self.assertEqual(differences, ["collections", "cast"])
        self.assertNotIn("Private", " ".join(differences))

    def test_combined_mismatch_reports_all_supported_labels_in_stable_order(self):
        actual = ET.fromstring("<movie/>")
        expected = ET.fromstring(
            "<movie><title>1</title><originaltitle>2</originaltitle><studio>3</studio>"
            "<premiered>4</premiered><year>5</year><plot>6</plot><runtime>7</runtime>"
            "<genre>8</genre><tag>9</tag><set><name>10</name></set>"
            "<actor><name>11</name><role>12</role></actor></movie>"
        )
        self.assertEqual(
            duplicate_jobs._audit_nfo_metadata_differences(actual, expected),
            ["title", "original title", "studio", "premiered date", "year", "plot",
             "runtime", "genres", "tags", "collections", "cast"],
        )

    def test_long_mismatch_values_show_context_around_the_first_difference(self):
        prefix = "Shared description text. " * 16
        actual_cast = "".join(
            f"<actor><name>Performer {index:02d} LongstageName</name><role>Performer</role></actor>"
            for index in range(1, 16)
        ) + "<actor><name>NFO-only performer</name><role>Performer</role></actor>"
        expected_cast = "".join(
            f"<actor><name>Performer {index:02d} LongstageName</name><role>Performer</role></actor>"
            for index in range(1, 16)
        ) + "<actor><name>Catalog performer</name><role>Performer</role></actor>"
        actual = ET.fromstring(f"<movie><plot>{prefix}NFO-only ending.</plot>{actual_cast}</movie>")
        expected = ET.fromstring(f"<movie><plot>{prefix}Catalog-only ending.</plot>{expected_cast}</movie>")

        diffs = duplicate_jobs._audit_nfo_metadata_diff_details(actual, expected)["diffs"]
        description = next(diff for diff in diffs if diff["field"] == "Description")
        cast = next(diff for diff in diffs if diff["field"] == "Cast")

        self.assertIn("NFO-only ending.", description["current"])
        self.assertIn("Catalog-only ending.", description["expected"])
        self.assertNotEqual(description["current"], description["expected"])
        self.assertIn("NFO-only performer", cast["current"])
        self.assertIn("Catalog performer", cast["expected"])
        self.assertNotEqual(cast["current"], cast["expected"])


class TestVideoDuplicateJobs(PgTestCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir_patch = patch.object(sync, "DATA_DIR", self.root)
        self.data_dir_patch.start()
        self.env_patch = patch.dict(os.environ, {
            "JELLYFIN_URL": os.getenv("JELLYFIN_URL") or "http://jellyfin.test:8096",
            "JELLYFIN_API_KEY": os.getenv("JELLYFIN_API_KEY") or "test-jellyfin-key",
        })
        self.env_patch.start()
        self.db_path = self.db_url
        self.media = self.root / "library"
        self.media.mkdir()
        self.keeper = "telegram:-1001:2:keeperid"
        self.loser = "telegram:-1001:1:loserid"
        self._add_video(self.keeper)
        self._add_video(self.loser)

    def tearDown(self):
        self.env_patch.stop()
        self.data_dir_patch.stop()
        self.temp.cleanup()
        super().tearDown()

    def _add_video(self, video_id):
        sync.sync_unique_video({
            "id": video_id, "provider": "telegram",
            "provider_video_id": video_id[len("telegram:"):],
            "title": video_id, "duration": 600, "performers": [],
        }, set(), set(), self.media, self.conn)
        (self.media / (sync.stable_media_basename(video_id) + ".jpg")).write_bytes(b"thumb")

    def _confirm_duplicate(self, *ids):
        with self.conn:
            self.conn.executemany("INSERT INTO video_fingerprints (video_id,algorithm,fingerprint) VALUES (%s,'OSHASH','0123456789abcdef') ON CONFLICT (video_id,algorithm) DO UPDATE SET fingerprint=EXCLUDED.fingerprint", [(value,) for value in ids])

    def test_full_catalog_audit_queues_both_audits(self):
        job_id = enqueue_job(self.db_url, "catalog_full_audit", {})
        with patch("common.job_queue.enqueue_jobs_batch", return_value=["quality", "consistency"]) as batch:
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        state, raw = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        result = json.loads(raw)
        self.assertEqual(state, "done")
        self.assertEqual(result["audit_job_ids"], ["quality", "consistency"])
        jobs = batch.call_args.args[1]
        self.assertEqual([job[0] for job in jobs], ["metadata_quality_audit", "consistency_audit"])
        self.assertEqual(jobs[0][1]["audit_batch_id"], jobs[1][1]["audit_batch_id"])
        self.assertTrue(jobs[1][1]["auto_repair"])

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

    def test_audit_nfo_repair_revalidates_then_regenerates_and_resolves_finding(self):
        video_id = "telegram:-1001:3:repairnfo"
        self._add_video(video_id)
        audit_id = "audit-repair-test-" + video_id.rsplit(":", 1)[-1]
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                "VALUES (%s,'consistency_audit','done','{}','{}',1,1)", (audit_id,)
            )
            finding_id = self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence) "
                "VALUES (%s,%s,'nfo_metadata_mismatch','open','test') RETURNING id", (audit_id, video_id)
            ).fetchone()[0]
            self.conn.execute("UPDATE videos SET title='Corrected title' WHERE id=%s", (video_id,))
        plan = preview_audit_repair(self.db_url, audit_id, [int(finding_id)])
        self.assertEqual(plan["outcome"], "previewed")
        queued = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(queued["outcome"], "queued")
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=True):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        terminal = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s",
                                     (queued["jobs"][0]["job_id"],)).fetchone()
        self.assertEqual(terminal[0], "done")
        self.assertEqual(json.loads(terminal[1])["local_state"], "nfo_repaired")
        file_path = self.conn.execute("SELECT file_path FROM synced_files WHERE video_id=%s", (video_id,)).fetchone()[0]
        nfo_path = sync.resolve_media_path(file_path, self.media).with_suffix(".nfo")
        import xml.etree.ElementTree as ET
        self.assertEqual(ET.parse(nfo_path).getroot().findtext("originaltitle"), "Corrected title")
        self.assertEqual(self.conn.execute("SELECT status FROM admin_audit_items WHERE id=%s", (finding_id,)).fetchone()[0], "resolved")

    def test_audit_artwork_repair_uses_catalog_thumbnail_and_creates_only_missing_file(self):
        video_id = "telegram:-1001:artwork-repair"
        self._add_video(video_id)
        with self.conn:
            self.conn.execute("UPDATE videos SET thumbnail=%s WHERE id=%s",
                              ("https://images.invalid/catalog-poster.jpg", video_id))
        file_path = self.conn.execute("SELECT file_path FROM synced_files WHERE video_id=%s", (video_id,)).fetchone()[0]
        stream = sync.resolve_media_path(file_path, self.media)
        artwork = stream.with_suffix(".jpg")
        artwork.unlink()
        audit_id = "audit-artwork-repair"
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                "VALUES (%s,'consistency_audit','done','{}','{}',1,1)", (audit_id,)
            )
            finding_id = self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence) "
                "VALUES (%s,%s,'missing_artwork','open','Missing artwork') RETURNING id",
                (audit_id, video_id),
            ).fetchone()[0]
        plan = preview_audit_repair(self.db_url, audit_id, [int(finding_id)])
        self.assertEqual(plan["outcome"], "previewed")
        queued = execute_operation(self.db_url, plan["plan_id"])
        downloader_calls = []

        def create_only(url, path):
            downloader_calls.append((url, path))
            if path.exists():
                return path.read_bytes() == b"restored-artwork"
            path.write_bytes(b"restored-artwork")
            return True

        with patch.object(sync, "thumbnail_urls", return_value=("https://public.invalid/p.jpg", "https://images.invalid/catalog-poster.jpg")), \
             patch.object(sync, "download_image_if_missing", side_effect=create_only), \
             patch.object(sync, "trigger_jellyfin_refresh", return_value=True):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertEqual(downloader_calls, [("https://images.invalid/catalog-poster.jpg", artwork)])
        self.assertEqual(artwork.read_bytes(), b"restored-artwork")
        self.assertEqual(self.conn.execute("SELECT status FROM admin_audit_items WHERE id=%s", (finding_id,)).fetchone()[0], "resolved")
        result = self.conn.execute("SELECT result FROM video_duplicate_jobs WHERE id=%s",
                                   (queued["jobs"][0]["job_id"],)).fetchone()[0]
        self.assertEqual(json.loads(result)["repaired_artwork_files"], 1)

    def test_audit_stream_repair_recreates_only_registered_stream_and_resolves_finding(self):
        video_id = "telegram:-1001:33:repairstream"
        self._add_video(video_id)
        audit_id = "audit-repair-stream"
        file_path = self.conn.execute(
            "SELECT file_path FROM synced_files WHERE video_id=%s", (video_id,)
        ).fetchone()[0]
        stream_path = sync.resolve_media_path(file_path, self.media)
        stream_path.unlink()
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                "VALUES (%s,'consistency_audit','done','{}','{}',1,1)", (audit_id,)
            )
            finding_id = self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence) "
                "VALUES (%s,%s,'missing_stream','open','test') RETURNING id", (audit_id, video_id)
            ).fetchone()[0]
        plan = preview_audit_repair(self.db_url, audit_id, [int(finding_id)])
        self.assertEqual(plan["outcome"], "previewed")
        queued = execute_operation(self.db_url, plan["plan_id"])
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=True):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        terminal = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s",
                                     (queued["jobs"][0]["job_id"],)).fetchone()
        result = json.loads(terminal[1])
        self.assertEqual(terminal[0], "done")
        self.assertEqual(result["restored_streams"], 1)
        self.assertEqual(result["local_state"], "files_repaired")
        self.assertTrue(stream_path.is_file())
        self.assertIn(f"/admin/play/{video_id}/stream", stream_path.read_text())
        self.assertEqual(self.conn.execute("SELECT status FROM admin_audit_items WHERE id=%s",
                                           (finding_id,)).fetchone()[0], "resolved")

    def test_audit_stream_repair_refuses_to_overwrite_file_that_appears_after_preview(self):
        video_id = "telegram:-1001:34:repaircollision"
        self._add_video(video_id)
        audit_id = "audit-repair-stream-collision"
        file_path = self.conn.execute(
            "SELECT file_path FROM synced_files WHERE video_id=%s", (video_id,)
        ).fetchone()[0]
        stream_path = sync.resolve_media_path(file_path, self.media)
        stream_path.unlink()
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                "VALUES (%s,'consistency_audit','done','{}','{}',1,1)", (audit_id,)
            )
            finding_id = self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence) "
                "VALUES (%s,%s,'missing_stream','open','test') RETURNING id", (audit_id, video_id)
            ).fetchone()[0]
        plan = preview_audit_repair(self.db_url, audit_id, [int(finding_id)])
        queued = execute_operation(self.db_url, plan["plan_id"])
        stream_path.write_text("unreviewed content", encoding="utf-8")
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=True):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
        terminal = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s",
                                     (queued["jobs"][0]["job_id"],)).fetchone()
        self.assertEqual(terminal[0], "failed")
        self.assertIn("different data", json.loads(terminal[1])["error"])
        self.assertEqual(stream_path.read_text(encoding="utf-8"), "unreviewed content")

    def test_consistency_audit_restart_replaces_partial_findings_without_duplicates(self):
        job_id = enqueue_job(self.db_url, "consistency_audit", {})
        first = self.conn.execute(
            "SELECT file_path,video_id FROM synced_files ORDER BY file_path LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(first)
        video_id = first[1]
        stream = sync.resolve_media_path(first[0], self.media)
        stream.unlink()

        record_progress = duplicate_jobs._record_job_progress

        def interrupt_after_first_row(conn, current_job, stage, **progress):
            record_progress(conn, current_job, stage, **progress)
            if stage == "auditing_library" and progress.get("completed") == 1:
                raise KeyboardInterrupt("simulated worker restart")

        with patch.object(duplicate_jobs, "_record_job_progress", side_effect=interrupt_after_first_row):
            with self.assertRaises(KeyboardInterrupt):
                sync.process_video_duplicate_jobs(self.conn, self.media)

        state, = self.conn.execute(
            "SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)
        ).fetchone()
        self.assertEqual(state, "processing")
        partial = self.conn.execute(
            "SELECT video_id,kind FROM admin_audit_items WHERE job_id=%s", (job_id,)
        ).fetchall()
        self.assertEqual(partial, [(video_id, "missing_metadata"), (video_id, "missing_stream")])

        with patch.object(sync, "_audit_telegram_import_state",
                          return_value={"status": "unknown", "findings": []}):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)

        state, raw_result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)
        ).fetchone()
        findings = self.conn.execute(
            "SELECT video_id,kind FROM admin_audit_items WHERE job_id=%s ORDER BY id", (job_id,)
        ).fetchall()
        self.assertEqual(state, "done")
        self.assertEqual(json.loads(raw_result)["findings"], 3)
        self.assertEqual(set(findings), {
            (self.keeper, "missing_metadata"), (self.loser, "missing_metadata"),
            (video_id, "missing_stream"),
        })

    def test_audit_nfo_repair_retry_resumes_after_jellyfin_refresh_failure(self):
        video_id = "telegram:-1001:4:repairretry"
        self._add_video(video_id)
        audit_id = "audit-repair-retry"
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                "VALUES (%s,'consistency_audit','done','{}','{}',1,1)", (audit_id,)
            )
            finding_id = self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence) "
                "VALUES (%s,%s,'missing_nfo','open','test') RETURNING id", (audit_id, video_id)
            ).fetchone()[0]
        plan = preview_audit_repair(self.db_url, audit_id, [int(finding_id)])
        original_job_id = execute_operation(self.db_url, plan["plan_id"])["jobs"][0]["job_id"]
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            sync.process_video_duplicate_jobs(self.conn, self.media)
        failed = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s",
                                   (original_job_id,)).fetchone()
        self.assertEqual(failed[0], "failed")
        self.assertEqual(json.loads(failed[1])["local_state"], "nfo_repaired")
        self.assertEqual(self.conn.execute("SELECT status FROM admin_audit_items WHERE id=%s", (finding_id,)).fetchone()[0], "open")
        retry_id = retry_job(self.db_url, original_job_id)
        self.assertIsNotNone(retry_id)
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=True):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        retried = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (retry_id,)).fetchone()
        self.assertEqual(retried[0], "done")
        self.assertEqual(json.loads(retried[1])["local_state"], "nfo_repaired")
        self.assertEqual(self.conn.execute("SELECT status FROM admin_audit_items WHERE id=%s", (finding_id,)).fetchone()[0], "resolved")

    def test_audit_import_refresh_never_claims_import_confirmation_or_resolves_finding(self):
        video_id = "telegram:-1001:5:reimport"
        self._add_video(video_id)
        audit_id = "audit-reimport-test"
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                "VALUES (%s,'consistency_audit','done','{}','{}',1,1)", (audit_id,)
            )
            finding_id = self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence) "
                "VALUES (%s,%s,'nfo_import_state_missing','open','test') RETURNING id", (audit_id, video_id)
            ).fetchone()[0]
        plan = preview_audit_import_refresh(self.db_url, audit_id, [int(finding_id)])
        self.assertEqual(plan["outcome"], "previewed")
        queued = execute_operation(self.db_url, plan["plan_id"])
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=True):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        result = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s",
                                   (queued["jobs"][0]["job_id"],)).fetchone()
        self.assertEqual(result[0], "done")
        self.assertEqual(json.loads(result[1])["jellyfin_import"], "not_confirmed")
        self.assertEqual(self.conn.execute("SELECT status FROM admin_audit_items WHERE id=%s", (finding_id,)).fetchone()[0], "open")

    def test_hide_quarantines_and_sync_does_not_readd_then_undo_restores(self):
        self._confirm_duplicate(self.keeper, self.loser)
        job_id = enqueue_job(self.db_path, "hide", {
            "keeper_id": self.keeper, "loser_ids": [self.loser],
        })
        def inspect_checkpoint():
            checkpoint = self.conn.execute(
                "SELECT state, result FROM video_duplicate_jobs WHERE id=%s", (job_id,)
            ).fetchone()
            self.assertEqual(checkpoint[0], "processing")
            progress = json.loads(checkpoint[1])
            self.assertEqual(progress["local_state"], "hidden")
            self.assertEqual(progress["jellyfin_refresh"], "pending")
            self.assertTrue(progress["snapshots"], "Progress updates must preserve the recovery snapshots.")
            self.assertEqual(progress["progress"]["stage"], "refreshing_jellyfin")
            self.assertTrue(any(event["stage"] == "moving_files" for event in progress["events"]))
            return False

        with patch.object(sync, "trigger_jellyfin_refresh", side_effect=inspect_checkpoint):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        terminal = self.conn.execute(
            "SELECT state, result FROM video_duplicate_jobs WHERE id=%s", (job_id,)
        ).fetchone()
        self.assertEqual(terminal[0], "done")
        terminal_result = json.loads(terminal[1])
        self.assertEqual(terminal_result["events"][-1]["stage"], "completed")
        self.assertIn("moving_files", [event["stage"] for event in terminal_result["events"]])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())
        quarantine = self.root / "duplicate-quarantine" / job_id / "files"
        self.assertTrue(list(quarantine.rglob("*.strm")))
        self.assertTrue(list(quarantine.rglob("*.nfo")))
        self.assertTrue(list(quarantine.rglob("*.jpg")))

        # Check video_sources registration
        sources = self.conn.execute(
            "SELECT video_id, source_id, priority, is_active FROM video_sources WHERE video_id=%s ORDER BY priority",
            (self.keeper,)).fetchall()
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[0], (self.keeper, self.keeper, 1, 1))
        self.assertEqual(sources[1], (self.keeper, self.loser, 2, 1))

        collected = {self.loser: {"video": {"id": self.loser}, "genres": set(), "tags": set()}}
        self.assertEqual(sync.flush_collected(collected, self.conn, self.media, "test"), 0)
        self.assertFalse(collected)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())

        undo_id = enqueue_job(self.db_path, "undo", {"operation_id": job_id})
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())
        basename = sync.stable_media_basename(self.loser)
        self.assertTrue((self.media / f"{basename}.strm").exists())
        self.assertTrue((self.media / f"{basename}.jpg").exists())
        state = self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (undo_id,)).fetchone()[0]
        self.assertEqual(state, "done")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM video_sources WHERE source_id=%s", (self.loser,)).fetchone())

    def test_hide_merges_metadata_fusion(self):
        k_id = "telegram:-1001:5:kmeta"
        l_id = "telegram:-1002:6:lmeta"
        sync.sync_unique_video({
            "id": k_id, "provider": "telegram", "provider_video_id": "-1001:5:kmeta",
            "title": "Title K", "duration": 600, "performers": ["Actor A"],
            "studio": "Studio K",
        }, {"Anal"}, {"Tag1"}, self.media, self.conn)
        sync.sync_unique_video({
            "id": l_id, "provider": "telegram", "provider_video_id": "-1002:6:lmeta",
            "title": "Title L", "duration": 600, "performers": ["Actor B"],
            "studio": "Studio L",
        }, {"Blowjob"}, {"Tag2"}, self.media, self.conn)

        self._confirm_duplicate(k_id, l_id)
        enqueue_job(self.db_path, "hide", {"keeper_id": k_id, "loser_ids": [l_id]})
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)

        row = self.conn.execute("SELECT performers, categories, tags, studio FROM videos WHERE id=%s", (k_id,)).fetchone()
        self.assertIsNotNone(row)
        perfs = json.loads(row[0])
        cats = json.loads(row[1])
        tags = json.loads(row[2])
        studio = row[3]

        self.assertEqual(sorted(perfs), ["Actor A", "Actor B"])
        self.assertEqual(sorted(cats), ["Anal", "Oral & Rimming"])
        # Loser's studio "Studio L" is merged into keeper's tags
        self.assertIn("Studio L", tags)
        self.assertIn("Tag1", tags)
        self.assertIn("Tag2", tags)
        self.assertEqual(studio, "Studio K")
        vc = {r[0] for r in self.conn.execute(
            "SELECT category FROM video_categories WHERE video_id=%s", (k_id,),
        ).fetchall()}
        self.assertTrue({"Anal", "Oral & Rimming"}.issubset(vc))

    def test_hide_copies_and_undo_restores_loser_manual_metadata_overrides(self):
        self._confirm_duplicate(self.keeper, self.loser)
        fields = {"title": "Manual source title", "plot": "Manual scene notes"}
        self.conn.execute(
            "INSERT INTO video_metadata_overrides (video_id,fields,revision,updated_at) "
            "VALUES (%s,%s::jsonb,3,CURRENT_TIMESTAMP)",
            (self.loser, json.dumps(fields)),
        )
        hide_id = enqueue_job(self.db_url, "hide", {
            "keeper_id": self.keeper, "loser_ids": [self.loser], "automatic": True,
            "rules_version": 4, "evidence": "phash",
        })
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        keeper_title, keeper_plot = self.conn.execute(
            "SELECT title,plot FROM videos WHERE id=%s", (self.keeper,),
        ).fetchone()
        self.assertEqual((keeper_title, keeper_plot), ("Manual source title", "Manual scene notes"))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM video_metadata_overrides WHERE video_id=%s", (self.loser,),
        ).fetchone())

        undo_id = enqueue_job(self.db_url, "undo", {"operation_id": hide_id})
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        restored = self.conn.execute(
            "SELECT fields,revision FROM video_metadata_overrides WHERE video_id=%s", (self.loser,),
        ).fetchone()
        self.assertEqual(json.loads(restored[0]), fields)
        self.assertEqual(restored[1], 3)
        self.assertEqual(self.conn.execute(
            "SELECT state FROM video_duplicate_jobs WHERE id=%s", (undo_id,),
        ).fetchone()[0], "done")

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

    def test_consistency_audit_is_read_only_and_recovery_replaces_partial_findings(self):
        from common.job_queue import enqueue_job
        keeper_path = self.media / f"{sync.stable_media_basename(self.keeper)}.strm"
        keeper_path.with_suffix(".nfo").unlink()
        (self.media / "unregistered.strm").write_text("http://example.invalid/placeholder", encoding="utf-8")
        with self.conn:
            self.conn.execute(
                "INSERT INTO synced_files (file_path,video_id,category,created_at) VALUES (%s,%s,'',1)",
                ("missing-item.strm", "orphan:catalog-reference"),
            )
        before_videos = self.conn.execute("SELECT count(*) FROM videos").fetchone()[0]
        job_id = enqueue_job(self.db_path, "consistency_audit", {})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        findings = self.conn.execute(
            "SELECT kind,video_id FROM admin_audit_items WHERE job_id=%s ORDER BY kind", (job_id,)
        ).fetchall()
        self.assertEqual({(row[0], row[1]) for row in findings}, {
            ("missing_nfo", self.keeper), ("catalog_reference_without_video", "orphan:catalog-reference"),
            ("orphan_file", None), ("missing_metadata", self.keeper), ("missing_metadata", self.loser),
        })
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], before_videos)
        with self.conn:
            self.conn.execute("UPDATE video_duplicate_jobs SET state='queued' WHERE id=%s", (job_id,))
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM admin_audit_items WHERE job_id=%s", (job_id,)).fetchone()[0], 5)

    def test_metadata_quality_audit_scans_all_catalog_videos_without_filesystem_work(self):
        from common.job_queue import enqueue_job
        catalog_only = "telegram:metadata-only-audit"
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id,provider,title,performers,categories,tags) "
                "VALUES (%s,'telegram','','[]','[]','[]')", (catalog_only,),
            )
        expected_total = self.conn.execute("SELECT count(*) FROM videos").fetchone()[0]
        batch_id = "c" * 32
        job_id = enqueue_job(self.db_path, "metadata_quality_audit", {"audit_batch_id": batch_id})
        processed = sync.process_video_duplicate_jobs(self.conn, self.media)
        state, raw_result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,),
        ).fetchone()
        self.assertEqual((processed, state), (1, "done"), f"metadata audit worker failed: {raw_result}")
        result = raw_result if isinstance(raw_result, dict) else json.loads(raw_result)
        findings = self.conn.execute(
            "SELECT video_id,kind FROM admin_audit_items WHERE job_id=%s", (job_id,),
        ).fetchall()
        self.assertEqual(result["audit_mode"], "metadata_quality")
        self.assertEqual(result["audit_batch_id"], batch_id)
        self.assertEqual((result["checked"], result["total"]), (expected_total, expected_total))
        self.assertTrue(any(row[0] == catalog_only and row[1] == "missing_metadata" for row in findings))
        self.assertTrue(all(row[1] == "missing_metadata" for row in findings))
        self.assertNotIn("jellyfin_import_state", result)

    def test_file_consistency_mode_does_not_emit_catalog_missing_metadata_findings(self):
        from common.job_queue import enqueue_job
        self.media.joinpath(f"{sync.stable_media_basename(self.keeper)}.nfo").unlink()
        job_id = enqueue_job(self.db_path, "consistency_audit", {"mode": "file_consistency"})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        result_row = self.conn.execute("SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        result = result_row[0] if isinstance(result_row[0], dict) else json.loads(result_row[0])
        kinds = {row[0] for row in self.conn.execute(
            "SELECT kind FROM admin_audit_items WHERE job_id=%s", (job_id,),
        ).fetchall()}
        self.assertEqual(result["audit_mode"], "file_consistency")
        self.assertNotIn("missing_metadata", kinds)
        self.assertIn("missing_nfo", kinds)

    def test_consistency_audit_finds_nested_orphan_stream_nfo_and_artwork_without_following_symlinks(self):
        from common.job_queue import enqueue_job
        nested = self.media / "nested-managed-folder"
        nested.mkdir()
        (nested / "unregistered.strm").write_text("https://private.invalid/video", encoding="utf-8")
        (nested / "unregistered.nfo").write_text("<movie/>", encoding="utf-8")
        (nested / "unregistered.jpg").write_bytes(b"artwork")
        outside = self.root / "outside-library"
        outside.mkdir()
        (outside / "must-not-be-scanned.nfo").write_text("<movie/>", encoding="utf-8")
        try:
            (self.media / "linked-outside").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Filesystem does not support symlinks: {exc}")

        job_id = enqueue_job(self.db_path, "consistency_audit", {})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        rows = self.conn.execute(
            "SELECT evidence,details FROM admin_audit_items WHERE job_id=%s AND kind='orphan_file' ORDER BY id",
            (job_id,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "Unregistered STRM, NFO, or artwork files share this basename family.")
        details = rows[0][1] if isinstance(rows[0][1], dict) else json.loads(rows[0][1])
        self.assertEqual({item["path"] for item in details["files"]}, {
            "nested-managed-folder/unregistered.strm", "nested-managed-folder/unregistered.nfo",
            "nested-managed-folder/unregistered.jpg",
        })
        self.assertNotIn(str(self.media), json.dumps(details))
        self.assertNotIn("must-not-be-scanned", json.dumps(details))
        result = json.loads(self.conn.execute(
            "SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)
        ).fetchone()[0])
        self.assertEqual(result["library_inventory_status"], "complete")
        self.assertGreater(result["library_inventory_entries"], 0)

    def test_consistency_audit_marks_bounded_filesystem_inventory_partial(self):
        from common.job_queue import enqueue_job
        job_id = enqueue_job(self.db_path, "consistency_audit", {})
        with patch.object(duplicate_jobs, "_AUDIT_INVENTORY_MAX_ENTRIES", 1):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        result = json.loads(self.conn.execute(
            "SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)
        ).fetchone()[0])
        self.assertEqual(result["library_inventory_status"], "partial")
        self.assertLessEqual(result["library_inventory_entries"], 1)

    def test_consistency_audit_completes_inventory_within_configured_bound(self):
        for name in ("one.strm", "two.nfo", "three.jpg"):
            (self.media / name).write_bytes(b"fixture")
        with patch.object(duplicate_jobs, "_AUDIT_INVENTORY_MAX_ENTRIES", 10):
            _orphans, scanned, complete = duplicate_jobs._audit_library_inventory(self.media, set())
        self.assertTrue(complete)
        self.assertLessEqual(scanned, 10)

    def test_live_inventory_bound_covers_current_library_headroom(self):
        self.assertGreaterEqual(duplicate_jobs._AUDIT_INVENTORY_MAX_ENTRIES, 100000)

    def test_orphan_family_can_be_quarantined_and_restored_with_revision_checks(self):
        from psycopg.types.json import Jsonb

        family = self.media / "manual-import" / "unregistered"
        family.parent.mkdir()
        paths = [family.with_suffix(suffix) for suffix in (".strm", ".nfo", ".jpg")]
        paths[0].write_text("https://example.invalid/stream", encoding="utf-8")
        paths[1].write_text("<movie/>", encoding="utf-8")
        paths[2].write_bytes(b"artwork")
        family_files = []
        for path in paths:
            stat = path.stat()
            family_files.append({"path": path.relative_to(self.media).as_posix(), "suffix": path.suffix,
                                 "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                 "dev": stat.st_dev, "ino": stat.st_ino})
        audit_id = "orphan-audit-" + uuid.uuid4().hex
        now = int(time.time())
        with self.conn:
            self.conn.execute("INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                              "VALUES (%s,'consistency_audit','done','{}',%s,%s,%s)",
                              (audit_id, json.dumps({"library_inventory_status": "complete"}), now, now))
            finding_id = self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,kind,status,evidence,details) "
                "VALUES (%s,'orphan_file','open','orphan family',%s) RETURNING id",
                (audit_id, Jsonb({"files": family_files, "total_bytes": sum(f["size"] for f in family_files)})),
            ).fetchone()[0]
        with patch.dict(os.environ, {"MEDIA_DIR": str(self.media), "DATA_DIR": str(self.root)}):
            plan = preview_orphan_file_quarantine(self.db_url, audit_id, [int(finding_id)])
            self.assertEqual(plan["outcome"], "previewed")
            self.assertNotIn(str(self.media), json.dumps(plan))
            queued = execute_operation(self.db_url, plan["plan_id"])
            self.assertEqual(queued["outcome"], "queued")
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
            self.assertTrue(all(not path.exists() for path in paths))
            self.assertEqual(self.conn.execute("SELECT status FROM admin_audit_items WHERE id=%s",
                                               (finding_id,)).fetchone()[0], "resolved")
            restore = preview_orphan_file_restore(self.db_url, plan["plan_id"])
            self.assertEqual(restore["outcome"], "previewed")
            execute_restore = execute_operation(self.db_url, restore["plan_id"])
            self.assertEqual(execute_restore["outcome"], "queued")
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
            self.assertTrue(all(path.is_file() for path in paths))
            self.assertEqual(self.conn.execute("SELECT state FROM admin_orphan_quarantine WHERE operation_id=%s",
                                               (plan["plan_id"],)).fetchone()[0], "restored")

    def test_consistency_audit_reports_invalid_and_stale_nfo_without_exposing_contents(self):
        invalid_stream = self.media / f"{sync.stable_media_basename(self.keeper)}.strm"
        stale_nfo = invalid_stream.with_suffix(".nfo")
        stale_nfo.write_text("<movie><originaltitle>Old title</originaltitle></movie>", encoding="utf-8")
        invalid_stream2 = self.media / f"{sync.stable_media_basename(self.loser)}.strm"
        invalid_stream2.with_suffix(".nfo").write_text("<movie>", encoding="utf-8")
        job_id = enqueue_job(self.db_path, "consistency_audit", {})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        rows = self.conn.execute(
            "SELECT kind,evidence FROM admin_audit_items WHERE job_id=%s", (job_id,)
        ).fetchall()
        self.assertEqual({row[0] for row in rows}, {"nfo_metadata_mismatch", "invalid_nfo", "missing_metadata"})
        self.assertTrue(all("Old title" not in row[1] and "<movie>" not in row[1] for row in rows))

    def test_consistency_audit_compares_effective_nfo_metadata_when_title_matches(self):
        import xml.etree.ElementTree as ET
        from common.video_metadata import load_metadata_state, update_metadata_state

        video_id = self.keeper
        with self.conn:
            self.conn.execute("UPDATE videos SET plot=%s WHERE id=%s", ("Source plot", video_id))
            update_metadata_state(self.conn, video_id, 0, {"plot": "Effective override plot"})
        state = load_metadata_state(self.conn, video_id)
        source = state["source"]
        nfo_video = {**source, **state["metadata"],
                     "dateadded": sync.format_dateadded(source.get("first_seen"))}
        categories = {row[0] for row in self.conn.execute(
            "SELECT category FROM video_categories WHERE video_id=%s", (video_id,)
        ).fetchall() if row[0]}
        tags = set(state["metadata"].get("tags") or [])
        file_paths = [row[0] for row in self.conn.execute(
            "SELECT file_path FROM synced_files WHERE video_id=%s ORDER BY file_path", (video_id,)
        ).fetchall()]
        nfo_paths = [sync.resolve_media_path(path, self.media).with_suffix(".nfo") for path in file_paths]
        for nfo_path in nfo_paths:
            sync.create_nfo_file(nfo_video, categories, tags, nfo_path, conn=self.conn)
        nfo_path = nfo_paths[0]
        generated = ET.parse(nfo_path).getroot()
        self.assertEqual(generated.findtext("originaltitle"), state["metadata"]["title"])
        self.assertEqual(generated.findtext("plot"), "Effective override plot")

        from common.job_queue import enqueue_job
        clean_audit = enqueue_job(self.db_url, "consistency_audit", {})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        clean_findings = self.conn.execute(
            "SELECT kind FROM admin_audit_items WHERE job_id=%s AND video_id=%s",
            (clean_audit, video_id),
        ).fetchall()
        self.assertNotIn(("nfo_metadata_mismatch",), clean_findings)

        generated.find("plot").text = "Manually changed private plot text"
        ET.ElementTree(generated).write(nfo_path, encoding="utf-8", xml_declaration=True)
        mismatch_audit = enqueue_job(self.db_url, "consistency_audit", {})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        mismatch = self.conn.execute(
            "SELECT kind,evidence FROM admin_audit_items WHERE job_id=%s AND video_id=%s AND kind='nfo_metadata_mismatch'",
            (mismatch_audit, video_id),
        ).fetchall()
        self.assertEqual(mismatch[0][0], "nfo_metadata_mismatch")
        self.assertEqual(
            mismatch[0][1],
            "NFO metadata differs from current effective catalog metadata (fields: plot).",
        )
        self.assertNotIn("Manually changed private plot text", mismatch[0][1])

    def test_consistency_audit_maps_jellyfin_import_hash_findings_to_catalog_items(self):
        from common.job_queue import enqueue_job
        job_id = enqueue_job(self.db_path, "consistency_audit", {})
        filenames = dict(self.conn.execute("SELECT video_id,file_path FROM synced_files").fetchall())
        keeper_filename = Path(filenames[self.keeper]).name
        loser_filename = Path(filenames[self.loser]).name
        audit = {
            "status": "checked",
            "findings": [(keeper_filename, "nfo_import_state_mismatch"),
                          (loser_filename, "nfo_import_state_missing")],
        }
        services = replace(sync._duplicate_job_services(),
                           audit_jellyfin_import_state=lambda _media_dir, _progress: audit)
        with patch.object(sync, "_duplicate_job_services", return_value=services):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        rows = self.conn.execute(
            "SELECT video_id,kind,evidence FROM admin_audit_items WHERE job_id=%s ORDER BY kind", (job_id,)
        ).fetchall()
        result_raw = self.conn.execute("SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0]
        self.assertEqual([(row[0], row[1]) for row in rows if row[1].startswith("nfo_import_state_")], [
            (self.keeper, "nfo_import_state_mismatch"),
            (self.loser, "nfo_import_state_missing"),
        ], msg=f"rows={rows!r}; result={result_raw}")
        self.assertTrue(all("sha256" not in row[2].lower() and "/" not in row[2] for row in rows))
        result = json.loads(result_raw)
        self.assertEqual(result["jellyfin_import_state"], "checked")

    def test_new_checked_audit_resolves_older_import_findings_only_for_unambiguous_files(self):
        from common.job_queue import enqueue_job
        previous_id = "audit-import-old"
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                "VALUES (%s,'consistency_audit','done','{}','{}',1,1)", (previous_id,)
            )
            previous_finding = self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence) "
                "VALUES (%s,%s,'nfo_import_state_mismatch','open','old evidence') RETURNING id",
                (previous_id, self.keeper),
            ).fetchone()[0]
        job_id = enqueue_job(self.db_path, "consistency_audit", {})
        services = replace(sync._duplicate_job_services(),
                           audit_jellyfin_import_state=lambda _media_dir, _progress: {"status": "checked", "findings": []})
        with patch.object(sync, "_duplicate_job_services", return_value=services):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertEqual(self.conn.execute("SELECT status FROM admin_audit_items WHERE id=%s",
                                           (previous_finding,)).fetchone()[0], "resolved")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM admin_audit_items WHERE job_id=%s AND kind IN ('nfo_import_state_missing','nfo_import_state_mismatch')", (job_id,)).fetchone()[0], 0)

    def test_unknown_jellyfin_audit_does_not_resolve_older_import_findings(self):
        from common.job_queue import enqueue_job
        previous_id = "audit-import-old-unknown-check"
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                "VALUES (%s,'consistency_audit','done','{}','{}',1,1)", (previous_id,)
            )
            previous_finding = self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence) "
                "VALUES (%s,%s,'nfo_import_state_mismatch','open','old evidence') RETURNING id",
                (previous_id, self.keeper),
            ).fetchone()[0]
        job_id = enqueue_job(self.db_path, "consistency_audit", {})
        services = replace(sync._duplicate_job_services(), audit_jellyfin_import_state=lambda _media_dir, _progress: {
            "status": "unknown", "findings": []
        })
        with patch.object(sync, "_duplicate_job_services", return_value=services):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)

        self.assertEqual(self.conn.execute("SELECT status FROM admin_audit_items WHERE id=%s",
                                           (previous_finding,)).fetchone()[0], "open")
        result = self.conn.execute("SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0]
        self.assertEqual(json.loads(result)["jellyfin_import_state"], "unknown")

    def test_consistency_audit_reports_missing_local_artwork_when_source_thumbnail_exists(self):
        from common.job_queue import enqueue_job
        row = self.conn.execute("SELECT file_path FROM synced_files WHERE video_id=%s", (self.keeper,)).fetchone()
        stream = self.media / Path(row[0]).name
        artwork = stream.with_suffix(".jpg")
        artwork.unlink()
        with self.conn:
            self.conn.execute("UPDATE videos SET thumbnail=%s WHERE id=%s",
                              ("https://images.invalid/poster.jpg", self.keeper))
        job_id = enqueue_job(self.db_path, "consistency_audit", {})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        findings = self.conn.execute(
            "SELECT video_id,kind FROM admin_audit_items WHERE job_id=%s", (job_id,)
        ).fetchall()
        self.assertIn((self.keeper, "missing_artwork"), findings)

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

    def test_quarantine_moves_single_item_and_undo_restores(self):
        job_id = enqueue_job(self.db_path, "quarantine", {"video_id": self.loser})
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())
        decision = self.conn.execute(
            "SELECT keeper_id,state FROM video_duplicate_decisions WHERE loser_id=%s", (self.loser,)).fetchone()
        self.assertEqual(tuple(decision), (self.loser, "active"))
        quarantine = self.root / "duplicate-quarantine" / job_id / "files"
        self.assertTrue(list(quarantine.rglob("*.strm")))
        self.assertTrue(list(quarantine.rglob("*.nfo")))
        self.assertTrue(list(quarantine.rglob("*.jpg")))

        undo_id = enqueue_job(self.db_path, "undo", {"operation_id": job_id})
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())
        basename = sync.stable_media_basename(self.loser)
        self.assertTrue((self.media / f"{basename}.strm").exists())
        self.assertEqual(self.conn.execute(
            "SELECT state FROM video_duplicate_jobs WHERE id=%s", (undo_id,)).fetchone()[0], "done")

    def test_restore_operation_plan_runs_existing_undo_worker_end_to_end(self):
        from common.admin_operations import execute_operation, preview_restore
        quarantine_id = enqueue_job(self.db_path, "quarantine", {"video_id": self.loser})
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        plan = preview_restore(self.db_path, quarantine_id)
        self.assertEqual(plan["outcome"], "previewed")
        queued = execute_operation(self.db_path, plan["plan_id"])
        restore_id = queued["jobs"][0]["job_id"]
        self.assertEqual(self.conn.execute("SELECT action FROM video_duplicate_jobs WHERE id=%s", (restore_id,)).fetchone()[0], "undo")
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())
        self.assertTrue((self.media / (sync.stable_media_basename(self.loser) + ".strm")).exists())
        self.assertEqual(self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (restore_id,)).fetchone()[0], "done")

    def test_failed_restore_compensates_file_moves_and_retry_restores_item(self):
        from common.admin_operations import execute_operation, preview_restore

        quarantine_id = enqueue_job(self.db_path, "quarantine", {"video_id": self.loser})
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        plan = preview_restore(self.db_path, quarantine_id)
        restore_id = execute_operation(self.db_path, plan["plan_id"])["jobs"][0]["job_id"]

        real_link = os.link
        calls = 0

        def fail_second_restore_move(source, target, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated restore filesystem failure")
            return real_link(source, target, **kwargs)

        with patch.object(sync.os, "link", side_effect=fail_second_restore_move), \
             patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)

        state, result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (restore_id,),
        ).fetchone()
        self.assertEqual(state, "failed")
        self.assertIn("simulated restore filesystem failure", json.loads(result)["error"])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM videos WHERE id=%s", (self.loser,),
        ).fetchone())
        basename = sync.stable_media_basename(self.loser)
        self.assertFalse((self.media / f"{basename}.strm").exists())
        quarantine_files = list((self.root / "duplicate-quarantine" / quarantine_id / "files").rglob("*.strm"))
        self.assertEqual(len(quarantine_files), 1)

        retry_id = retry_job(self.db_path, restore_id)
        self.assertIsNotNone(retry_id)
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        retry_state, retry_result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (retry_id,),
        ).fetchone()
        self.assertEqual(retry_state, "done")
        self.assertEqual(json.loads(retry_result)["local_state"], "restored")
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM videos WHERE id=%s", (self.loser,),
        ).fetchone())
        self.assertTrue((self.media / f"{basename}.strm").exists())

    def test_restore_worker_rejects_snapshot_path_outside_library(self):
        from common.admin_operations import execute_operation, preview_restore
        quarantine_id = enqueue_job(self.db_path, "quarantine", {"video_id": self.loser})
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        decision = self.conn.execute(
            "SELECT snapshot FROM video_duplicate_decisions WHERE operation_id=%s", (quarantine_id,)
        ).fetchone()
        snapshot = json.loads(decision[0])
        snapshot["files"][0]["source"] = "/tmp/not-library/escape.strm"
        with self.conn:
            self.conn.execute("UPDATE video_duplicate_decisions SET snapshot=%s WHERE operation_id=%s",
                              (json.dumps(snapshot), quarantine_id))
        plan = preview_restore(self.db_path, quarantine_id)
        queued = execute_operation(self.db_path, plan["plan_id"])
        restore_id = queued["jobs"][0]["job_id"]
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
        job = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (restore_id,)).fetchone()
        self.assertEqual(job[0], "failed")
        self.assertIn("Unsafe restore path", json.loads(job[1])["error"])
        self.assertFalse(Path("/tmp/not-library/escape.strm").exists())

    def test_restore_worker_conflicts_if_video_was_readded_after_quarantine(self):
        from common.admin_operations import execute_operation, preview_restore
        quarantine_id = enqueue_job(self.db_path, "quarantine", {"video_id": self.loser})
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self._add_video(self.loser)
        plan = preview_restore(self.db_path, quarantine_id)
        queued = execute_operation(self.db_path, plan["plan_id"])
        restore_id = queued["jobs"][0]["job_id"]

        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)

        state, result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (restore_id,),
        ).fetchone()
        self.assertEqual(state, "failed")
        self.assertEqual(json.loads(result)["error"],
                         "Item was added again; undo requires manual review.")
        self.assertEqual(self.conn.execute(
            "SELECT state FROM video_duplicate_decisions WHERE loser_id=%s", (self.loser,),
        ).fetchone()[0], "active")
        self.assertTrue((self.media / (sync.stable_media_basename(self.loser) + ".strm")).exists())

    def test_restore_worker_never_overwrites_an_existing_target_file(self):
        from common.admin_operations import execute_operation, preview_restore
        quarantine_id = enqueue_job(self.db_path, "quarantine", {"video_id": self.loser})
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        basename = sync.stable_media_basename(self.loser)
        target = self.media / f"{basename}.strm"
        target.write_text("operator-owned replacement", encoding="utf-8")
        plan = preview_restore(self.db_path, quarantine_id)
        queued = execute_operation(self.db_path, plan["plan_id"])
        restore_id = queued["jobs"][0]["job_id"]

        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)

        state, result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (restore_id,),
        ).fetchone()
        self.assertEqual(state, "failed")
        self.assertEqual(json.loads(result)["error"], "Target path exists; not overwritten.")
        self.assertEqual(target.read_text(encoding="utf-8"), "operator-owned replacement")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM videos WHERE id=%s", (self.loser,),
        ).fetchone())

    def test_restore_worker_conflicts_if_registered_path_belongs_to_another_video(self):
        from common.admin_operations import execute_operation, preview_restore
        quarantine_id = enqueue_job(self.db_path, "quarantine", {"video_id": self.loser})
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        snapshot_row = self.conn.execute(
            "SELECT snapshot FROM video_duplicate_decisions WHERE operation_id=%s AND loser_id=%s",
            (quarantine_id, self.loser),
        ).fetchone()
        snapshot = snapshot_row[0]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        original_path = snapshot["synced_files"][0]["file_path"]
        with self.conn:
            self.conn.execute("UPDATE synced_files SET file_path=%s WHERE video_id=%s",
                              (original_path, self.keeper))
        plan = preview_restore(self.db_path, quarantine_id)
        queued = execute_operation(self.db_path, plan["plan_id"])
        restore_id = queued["jobs"][0]["job_id"]

        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)

        state, result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (restore_id,),
        ).fetchone()
        self.assertEqual(state, "failed")
        self.assertEqual(json.loads(result)["error"], "File path is currently used by another entry.")
        self.assertEqual(self.conn.execute(
            "SELECT video_id FROM synced_files WHERE file_path=%s", (original_path,),
        ).fetchone()[0], self.keeper)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM videos WHERE id=%s", (self.loser,),
        ).fetchone())

    def test_quarantine_worker_rejects_catalog_changes_after_plan_execution(self):
        from common.admin_operations import execute_operation, preview_quarantine
        plan = preview_quarantine(self.db_path, [self.loser])
        queued = execute_operation(self.db_path, plan["plan_id"])
        job_id = queued["jobs"][0]["job_id"]
        with self.conn:
            self.conn.execute("UPDATE videos SET title='Changed while queued' WHERE id=%s", (self.loser,))
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
        job = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        self.assertEqual(job[0], "failed")
        self.assertIn("changed after the preview", json.loads(job[1])["error"])
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())
        self.assertTrue((self.media / (sync.stable_media_basename(self.loser) + ".strm")).exists())

    def test_manual_hide_without_automatic_evidence(self):
        from common.admin_operations import execute_operation, preview_hide
        plan = preview_hide(self.db_path, self.keeper, [self.loser])
        self.assertEqual(plan['items'][0]['evidence']['type'], 'operator_review')
        queued = execute_operation(self.db_path, plan['plan_id'])
        with patch.object(sync, 'trigger_jellyfin_refresh', return_value=False):
            sync.process_video_duplicate_jobs(self.conn, self.media)
        state = self.conn.execute('SELECT state FROM video_duplicate_jobs WHERE id=%s',
                                  (queued['jobs'][0]['job_id'],)).fetchone()[0]
        self.assertEqual(state, 'done')
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM video_duplicate_decisions WHERE loser_id=%s AND state='active'", (self.loser,)
        ).fetchone())

    def test_hide_worker_rejects_catalog_changes_after_plan_execution(self):
        self._confirm_duplicate(self.keeper, self.loser)
        from common.admin_operations import execute_operation, preview_hide
        plan = preview_hide(self.db_path, self.keeper, [self.loser])
        queued = execute_operation(self.db_path, plan["plan_id"])
        job_id = queued["jobs"][0]["job_id"]
        with self.conn:
            self.conn.execute("UPDATE videos SET title='Changed while queued' WHERE id=%s", (self.keeper,))
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
        job = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        self.assertEqual(job[0], "failed")
        self.assertIn("changed after the preview", json.loads(job[1])["error"])
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.keeper,)).fetchone())

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

    def test_quarantine_restart_after_catalog_commit_preserves_undo(self):
        from resolver.admin_services import job_listing
        job_id = enqueue_job(self.db_path, "quarantine", {"video_id": self.loser})
        with patch.object(sync, "trigger_jellyfin_refresh", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                sync.process_video_duplicate_jobs(self.conn, self.media)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())
        self.assertEqual(self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0], "processing")

        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        job = next(item for item in job_listing(self.db_path)["jobs"] if item["id"] == job_id)
        self.assertEqual(job["state"], "done")
        self.assertTrue(job["undo_available"])
        self.assertEqual(job["result"]["local_state"], "quarantined")
        undo_id = enqueue_job(self.db_path, "undo", {"operation_id": job_id})
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())
        self.assertTrue((self.media / (sync.stable_media_basename(self.loser) + ".strm")).exists())
        self.assertEqual(self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (undo_id,)).fetchone()[0], "done")

    def test_dismissal_persists_as_pair_decision(self):
        job_id = enqueue_job(self.db_path, "dismiss", {
            "left_id": self.keeper, "right_id": self.loser,
        })
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        row = self.conn.execute(
            "SELECT left_id,right_id,active,operation_id FROM video_duplicate_dismissals"
        ).fetchone()
        self.assertEqual(tuple(row), (min(self.keeper, self.loser), max(self.keeper, self.loser), 1, job_id))

    def test_failed_partial_file_move_is_compensated(self):
        job_id = enqueue_job(self.db_path, "hide", {
            "keeper_id": self.keeper, "loser_ids": [self.loser],
        })
        real_link = os.link
        calls = 0

        def fail_second_replace(source, target, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated filesystem failure")
            return real_link(source, target, **kwargs)

        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            with patch.object(sync.os, "link", side_effect=fail_second_replace):
                self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)

        basename = sync.stable_media_basename(self.loser)
        self.assertTrue((self.media / f"{basename}.strm").exists())
        self.assertTrue((self.media / f"{basename}.nfo").exists())
        self.assertTrue((self.media / f"{basename}.jpg").exists())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())
        self.assertEqual(self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0], "failed")

    def test_hide_rejects_symlink_to_keeper_files(self):
        loser_base = sync.stable_media_basename(self.loser)
        keeper_stream = self.media / f"{sync.stable_media_basename(self.keeper)}.strm"
        loser_stream = self.media / f"{loser_base}.strm"
        loser_stream.unlink()
        loser_stream.symlink_to(keeper_stream)
        job_id = enqueue_job(self.db_path, "hide", {
            "keeper_id": self.keeper, "loser_ids": [self.loser],
        })
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
        self.assertTrue(keeper_stream.exists())
        self.assertTrue((self.media / f"{sync.stable_media_basename(self.keeper)}.jpg").exists())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())
        state = self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0]
        self.assertEqual(state, "failed")

    def test_hide_rejects_quarantine_target_symlink_to_source(self):
        self._confirm_duplicate(self.keeper, self.loser)
        job_id = enqueue_job(self.db_path, "hide", {
            "keeper_id": self.keeper, "loser_ids": [self.loser],
        })
        basename = sync.stable_media_basename(self.loser)
        source = self.media / f"{basename}.strm"
        original_source = source.read_text(encoding="utf-8")
        quarantine = self.root / "duplicate-quarantine" / job_id / "files" / f"{basename}.strm"
        quarantine.parent.mkdir(parents=True)
        quarantine.symlink_to(source)

        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)

        self.assertEqual(source.read_text(encoding="utf-8"), original_source)
        self.assertTrue(quarantine.is_symlink())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM videos WHERE id=%s", (self.loser,),
        ).fetchone())
        state, result = self.conn.execute(
            "SELECT state,result FROM video_duplicate_jobs WHERE id=%s", (job_id,),
        ).fetchone()
        self.assertEqual(state, "failed")
        self.assertIn("Quarantine file has different contents", json.loads(result)["error"])

    def test_restart_resumes_from_persisted_file_journal(self):
        self._confirm_duplicate(self.keeper, self.loser)
        job_id = enqueue_job(self.db_path, "hide", {
            "keeper_id": self.keeper, "loser_ids": [self.loser],
        })
        real_link = os.link
        calls = 0

        def simulate_process_stop(source, target, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt()
            return real_link(source, target, **kwargs)

        with patch.object(sync.os, "link", side_effect=simulate_process_stop):
            with self.assertRaises(KeyboardInterrupt):
                sync.process_video_duplicate_jobs(self.conn, self.media)
        self.assertEqual(self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0], "processing")

        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
            undo_id = enqueue_job(self.db_path, "undo", {"operation_id": job_id})
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        basename = sync.stable_media_basename(self.loser)
        self.assertTrue((self.media / f"{basename}.strm").exists())
        self.assertTrue((self.media / f"{basename}.nfo").exists())
        self.assertTrue((self.media / f"{basename}.jpg").exists())
        self.assertEqual(self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (undo_id,)).fetchone()[0], "done")

    def test_quarantine_restart_resumes_from_journal(self):
        job_id = enqueue_job(self.db_path, "quarantine", {"video_id": self.loser})
        self.conn.execute("UPDATE video_duplicate_jobs SET state='processing' WHERE id=%s", (job_id,))
        self.conn.commit()
        real_link = os.link
        calls = 0

        def simulate_process_stop(source, target, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt()
            return real_link(source, target, **kwargs)

        with patch.object(sync.os, "link", side_effect=simulate_process_stop):
            with self.assertRaises(KeyboardInterrupt):
                sync.process_video_duplicate_jobs(self.conn, self.media)
        self.assertEqual(self.conn.execute(
            "SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0], "processing")

        # The next worker run requeues the interrupted job and resumes from the journal.
        with patch.object(sync, "trigger_jellyfin_refresh", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM videos WHERE id=%s", (self.loser,)).fetchone())
        self.assertEqual(self.conn.execute(
            "SELECT state FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()[0], "done")
        self.assertEqual(self.conn.execute(
            "SELECT state FROM video_duplicate_decisions WHERE loser_id=%s", (self.loser,)).fetchone()[0], "active")

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
