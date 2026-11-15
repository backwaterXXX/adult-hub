import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from pg_helper import PgTestCase
from test_support import load_sync_module

sync = load_sync_module()
from resolver.video_duplicates import enqueue_job
from common.job_queue import retry_job
from common.admin_operations import (
    execute_operation,
    preview_orphan_file_quarantine,
    preview_orphan_file_restore,
)


class BaseVideoDuplicateJobsTestCase(PgTestCase):
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



class TestVideoDuplicateJobs(BaseVideoDuplicateJobsTestCase):
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



if __name__ == "__main__":
    unittest.main()
