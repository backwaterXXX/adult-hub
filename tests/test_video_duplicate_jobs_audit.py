import json
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import sync.duplicate_jobs as duplicate_jobs
from resolver.video_duplicates import enqueue_job
from common.admin_operations import (
    execute_operation,
    preview_audit_repair,
)
from tests.test_video_duplicate_jobs import BaseVideoDuplicateJobsTestCase, sync


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
             patch.object(sync, "download_image_if_missing", side_effect=create_only):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertEqual(downloader_calls, [("https://images.invalid/catalog-poster.jpg", artwork)])
        self.assertEqual(artwork.read_bytes(), b"restored-artwork")
        self.assertEqual(self.conn.execute("SELECT status FROM admin_audit_items WHERE id=%s", (finding_id,)).fetchone()[0], "resolved")
        result = self.conn.execute("SELECT result FROM video_duplicate_jobs WHERE id=%s",
                                   (queued["jobs"][0]["job_id"],)).fetchone()[0]
        self.assertEqual(json.loads(result)["repaired_artwork_files"], 1)


    def test_stream_repair_marks_the_video_unavailable_when_it_cannot_restore(self):
        """A stream that cannot be recreated must stop advertising playback."""
        video_id = "telegram:-1001:34:unrestorable"
        self._add_video(video_id)
        audit_id = "audit-repair-unrestorable"
        file_path = self.conn.execute(
            "SELECT file_path FROM synced_files WHERE video_id=%s", (video_id,)
        ).fetchone()[0]
        stream_path = sync.resolve_media_path(file_path, self.media)
        stream_path.unlink()
        with self.conn:
            self.conn.execute("UPDATE videos SET playable=1 WHERE id=%s", (video_id,))
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

        with patch.object(sync, "create_stream_file", return_value=False):
            self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)

        result = json.loads(self.conn.execute(
            "SELECT result FROM video_duplicate_jobs WHERE id=%s", (queued["jobs"][0]["job_id"],)
        ).fetchone()[0])
        self.assertEqual(result["streams_marked_unavailable"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT playable FROM videos WHERE id=%s", (video_id,)
        ).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM dead_streams WHERE video_id=%s", (video_id,)
        ).fetchone()[0], 1)

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
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 0)
        terminal = self.conn.execute("SELECT state,result FROM video_duplicate_jobs WHERE id=%s",
                                     (queued["jobs"][0]["job_id"],)).fetchone()
        self.assertEqual(terminal[0], "failed")
        self.assertIn("different data", json.loads(terminal[1])["error"])
        self.assertEqual(stream_path.read_text(encoding="utf-8"), "unreviewed content")


    def test_consistency_audit_is_read_only_and_recovery_replaces_partial_findings(self):
        from common.job_queue import enqueue_job
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
            ("catalog_reference_without_video", "orphan:catalog-reference"),
            ("orphan_file", None), ("missing_metadata", self.keeper), ("missing_metadata", self.loser),
        })
        self.assertEqual(self.conn.execute("SELECT count(*) FROM videos").fetchone()[0], before_videos)
        with self.conn:
            self.conn.execute("UPDATE video_duplicate_jobs SET state='queued' WHERE id=%s", (job_id,))
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM admin_audit_items WHERE job_id=%s", (job_id,)).fetchone()[0], 4)


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


    def test_file_consistency_mode_does_not_emit_catalog_missing_metadata_findings(self):
        from common.job_queue import enqueue_job
        self.media.joinpath(f"{sync.stable_media_basename(self.keeper)}.strm").unlink()
        job_id = enqueue_job(self.db_path, "consistency_audit", {"mode": "file_consistency"})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        result_row = self.conn.execute("SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)).fetchone()
        result = result_row[0] if isinstance(result_row[0], dict) else json.loads(result_row[0])
        kinds = {row[0] for row in self.conn.execute(
            "SELECT kind FROM admin_audit_items WHERE job_id=%s", (job_id,),
        ).fetchall()}
        self.assertEqual(result["audit_mode"], "file_consistency")
        self.assertNotIn("missing_metadata", kinds)
        self.assertIn("missing_stream", kinds)


    def test_consistency_audit_finds_nested_orphan_stream_and_artwork_without_following_symlinks(self):
        from common.job_queue import enqueue_job
        nested = self.media / "nested-managed-folder"
        nested.mkdir()
        (nested / "unregistered.strm").write_text("https://private.invalid/video", encoding="utf-8")
        (nested / "unregistered.jpg").write_bytes(b"artwork")
        outside = self.root / "outside-library"
        outside.mkdir()
        (outside / "must-not-be-scanned.jpg").write_bytes(b"artwork")
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
        self.assertEqual(rows[0][0], "Unregistered STRM or artwork files share this basename family.")
        details = rows[0][1] if isinstance(rows[0][1], dict) else json.loads(rows[0][1])
        self.assertEqual({item["path"] for item in details["files"]}, {
            "nested-managed-folder/unregistered.strm", "nested-managed-folder/unregistered.jpg",
        })
        self.assertNotIn(str(self.media), json.dumps(details))
        self.assertNotIn("must-not-be-scanned", json.dumps(details))
        result = json.loads(self.conn.execute(
            "SELECT result FROM video_duplicate_jobs WHERE id=%s", (job_id,)
        ).fetchone()[0])
        self.assertEqual(result["library_inventory_status"], "complete")
        self.assertGreater(result["library_inventory_entries"], 0)
        self.assertGreaterEqual(result["library_inventory_directories"], 1)


    def test_consistency_audit_ignores_performer_artwork_registered_under_performers(self):
        """Portraits registered in performer_photos are not orphans just because they have no .strm."""
        from common.job_queue import enqueue_job
        performers = self.media / "performers"
        performers.mkdir()
        (performers / "known-portrait-ab12cd34ef.jpg").write_bytes(b"artwork")
        with self.conn:
            self.conn.execute(
                "INSERT INTO performer_photos (name, thumb, updated_at) VALUES (%s, %s, now())",
                ("Known Performer", "performers/known-portrait-ab12cd34ef.jpg"),
            )

        job_id = enqueue_job(self.db_path, "consistency_audit", {"mode": "file_consistency"})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        orphans = self.conn.execute(
            "SELECT details FROM admin_audit_items WHERE job_id=%s AND kind='orphan_file'",
            (job_id,),
        ).fetchall()

        self.assertEqual(orphans, [])

    def test_consistency_audit_still_flags_unregistered_artwork_under_performers(self):
        from common.job_queue import enqueue_job
        performers = self.media / "performers"
        performers.mkdir()
        (performers / "stray-portrait-ffeeddccbb.jpg").write_bytes(b"artwork")

        job_id = enqueue_job(self.db_path, "consistency_audit", {"mode": "file_consistency"})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        rows = self.conn.execute(
            "SELECT details FROM admin_audit_items WHERE job_id=%s AND kind='orphan_file'",
            (job_id,),
        ).fetchall()

        self.assertEqual(len(rows), 1)
        details = rows[0][0] if isinstance(rows[0][0], dict) else json.loads(rows[0][0])
        self.assertEqual([item["path"] for item in details["files"]], [
            "performers/stray-portrait-ffeeddccbb.jpg",
        ])

    def test_consistency_audit_skips_dotfile_artwork_temporaries(self):
        from common.job_queue import enqueue_job
        (self.media / ".artwork-gen-abcd1234.jpg").write_bytes(b"")

        job_id = enqueue_job(self.db_path, "consistency_audit", {"mode": "file_consistency"})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        orphans = self.conn.execute(
            "SELECT id FROM admin_audit_items WHERE job_id=%s AND kind='orphan_file'",
            (job_id,),
        ).fetchall()

        self.assertEqual(orphans, [])

    def test_complete_audit_resolves_orphan_findings_for_files_that_disappeared(self):
        from common.job_queue import enqueue_job
        stray = self.media / "nested-managed-folder"
        stray.mkdir()
        (stray / "temporary.jpg").write_bytes(b"artwork")
        first_job = enqueue_job(self.db_path, "consistency_audit", {"mode": "file_consistency"})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM admin_audit_items WHERE job_id=%s AND kind='orphan_file'",
            (first_job,),
        ).fetchone()[0], 1)

        (stray / "temporary.jpg").unlink()
        second_job = enqueue_job(self.db_path, "consistency_audit", {"mode": "file_consistency"})
        self.assertEqual(sync.process_video_duplicate_jobs(self.conn, self.media), 1)

        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM admin_audit_items WHERE job_id=%s AND kind='orphan_file'",
            (second_job,),
        ).fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM admin_audit_items WHERE job_id=%s AND kind='orphan_file'",
            (first_job,),
        ).fetchone()[0], "resolved")

    def test_orphan_quarantine_preview_refuses_registered_performer_artwork(self):
        from common.admin_operation_orphan_previews import _safe_orphan_files
        performers = self.media / "performers"
        performers.mkdir()
        portrait = performers / "portrait-ab12cd34ef.jpg"
        portrait.write_bytes(b"artwork")
        info = portrait.stat()
        item = {
            "path": "performers/portrait-ab12cd34ef.jpg", "suffix": ".jpg",
            "size": info.st_size, "mtime_ns": info.st_mtime_ns,
            "dev": info.st_dev, "ino": info.st_ino,
        }

        self.assertEqual(
            len(_safe_orphan_files([item], self.media)), 1,
            "an unregistered portrait stays quarantinable",
        )
        self.assertIsNone(
            _safe_orphan_files([item], self.media, {"performers/portrait-ab12cd34ef.jpg"}),
            "a portrait registered in performer_photos must be refused",
        )

    def test_partial_walk_still_reports_the_directory_tree_it_reached(self):
        """A tight budget must not stop at the root and hide nested folders."""
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir)
            deep = media / "zz-performers"
            deep.mkdir()
            (deep / "portrait.jpg").write_bytes(b"artwork")
            for index in range(6):
                (media / f"root-{index}.jpg").write_bytes(b"x")

            with patch.object(duplicate_jobs, "_AUDIT_INVENTORY_MAX_ENTRIES", 3):
                orphans, scanned, complete, directories = duplicate_jobs._audit_library_inventory(
                    media, set(),
                )

        self.assertFalse(complete)
        self.assertEqual(scanned, 3)
        self.assertGreaterEqual(directories, 2, "the walk must still reach nested directories")
        paths = [item["path"] for family in orphans for item in family["files"]]
        self.assertNotIn("zz-performers/portrait.jpg", paths)

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
        for name in ("one.strm", "two.jpg", "three.jpg"):
            (self.media / name).write_bytes(b"fixture")
        with patch.object(duplicate_jobs, "_AUDIT_INVENTORY_MAX_ENTRIES", 10):
            _orphans, scanned, complete, _directories = duplicate_jobs._audit_library_inventory(
                self.media, set(),
            )
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
