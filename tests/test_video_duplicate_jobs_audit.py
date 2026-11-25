from dataclasses import replace
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

import sync.duplicate_jobs as duplicate_jobs
from resolver.video_duplicates import enqueue_job
from common.job_queue import retry_job
from common.admin_operations import (
    execute_operation,
    preview_audit_import_refresh,
    preview_audit_repair,
)
from tests.test_video_duplicate_jobs import BaseVideoDuplicateJobsTestCase, sync


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



class TestVideoDuplicateJobsAudit(BaseVideoDuplicateJobsTestCase):
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
        """The walk must finish on the real library, or every audit stays partial."""

        self.assertGreaterEqual(duplicate_jobs._AUDIT_INVENTORY_MAX_ENTRIES, 250000)

    def test_the_inventory_bound_can_be_raised_from_the_environment(self):
        with patch.dict(os.environ, {"AUDIT_INVENTORY_MAX_ENTRIES": "400000"}):
            self.assertEqual(duplicate_jobs._audit_entry_limit(), 400000)
        with patch.dict(os.environ, {"AUDIT_INVENTORY_MAX_ENTRIES": "nonsense"}):
            self.assertEqual(duplicate_jobs._audit_entry_limit(), 250000)
        with patch.dict(os.environ, {"AUDIT_INVENTORY_MAX_ENTRIES": "0"}):
            self.assertEqual(duplicate_jobs._audit_entry_limit(), 250000)


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



if __name__ == "__main__":
    unittest.main()
