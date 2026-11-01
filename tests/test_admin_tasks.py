"""PostgreSQL contracts for the unified read-only Tasks projection."""

import time

from admin_v2_tasks import _list_tasks
from pg_helper import PgTestCase


class TestAdminTaskProjection(PgTestCase):
    def add_video(self, video_id: str):
        self.conn.execute(
            "INSERT INTO videos (id,provider,title) VALUES (%s,'telegram',%s)",
            (video_id, f"Title {video_id}"),
        )

    def test_merged_page_orders_both_queues_and_keeps_projection_safe(self):
        times = [1_800_000_000, 1_800_000_100]
        self.add_video("video-phash-1")
        self.add_video("video-phash-2")
        self.conn.execute(
            "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
            "VALUES ('admin-old','sync','queued','{}',NULL,%s,%s)", (times[0], times[0]),
        )
        self.conn.execute(
            "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
            "VALUES ('admin-new','backup_create','failed','{}',%s,%s,%s)",
            ('{"error":"private token must not escape","failure_code":"remote_service_failed"}', times[1], times[1]),
        )
        for job_id, video_id, stamp, state, error_code in (
            ("p-new", "video-phash-1", times[1], "queued", None),
            ("p-old", "video-phash-2", times[0] - 50, "failed", "generation_failed"),
        ):
            self.conn.execute(
                "INSERT INTO video_phash_jobs "
                "(id,video_id,algorithm_version,decoder_profile,duration_us,submitted_by_user_id,state,error_code,created_at,updated_at) "
                "VALUES (%s,%s,'test-v1','test-decoder',1000000,'private-user',%s,%s,%s,%s)",
                (job_id, video_id, state, error_code, stamp, stamp),
            )
        self.conn.commit()

        page = _list_tasks(self.db_url, queue="all", state="", action="", offset=0, limit=3)
        self.assertTrue(page["available"])
        self.assertEqual([(task["queue"], task["id"]) for task in page["tasks"]], [
            ("phash", "p-new"), ("jobs", "admin-new"), ("jobs", "admin-old"),
        ])
        self.assertEqual((page["total"], page["active_total"], page["truncated"]), (4, 2, True))
        self.assertNotIn("submitted_by_user_id", str(page))
        self.assertNotIn("private-user", str(page))
        self.assertNotIn("private token", str(page))
        self.assertNotIn("payload", page["tasks"][1])

        next_page = _list_tasks(self.db_url, queue="all", state="", action="", offset=3, limit=3)
        self.assertEqual([(task["queue"], task["id"]) for task in next_page["tasks"]], [("phash", "p-old")])
        self.assertEqual(next_page["active_total"], 2)

    def test_shared_filters_keep_queue_specific_states_and_actions(self):
        self.add_video("video-phash-filter")
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO video_duplicate_jobs (id,action,state,payload,created_at,updated_at) "
            "VALUES ('sync-queued','sync','queued','{}',%s,%s)", (now, now),
        )
        self.conn.execute(
            "INSERT INTO video_duplicate_jobs (id,action,state,payload,created_at,updated_at) "
            "VALUES ('sync-done','sync','done','{}',%s,%s)", (now + 1, now + 1),
        )
        self.conn.execute(
            "INSERT INTO video_phash_jobs "
            "(id,video_id,algorithm_version,decoder_profile,duration_us,submitted_by_user_id,state,created_at,updated_at) "
            "VALUES ('p-queued','video-phash-filter','test-v1','test-decoder',1000000,'test','queued',%s,%s)",
            (now, now),
        )
        self.conn.commit()

        active = _list_tasks(self.db_url, queue="all", state="active", action="", offset=0, limit=10)
        self.assertEqual({task["id"] for task in active["tasks"]}, {"sync-queued", "p-queued"})
        only_phash = _list_tasks(self.db_url, queue="phash", state="", action="", offset=0, limit=10)
        self.assertEqual([task["id"] for task in only_phash["tasks"]], ["p-queued"])
        only_sync = _list_tasks(self.db_url, queue="all", state="", action="sync", offset=0, limit=10)
        self.assertEqual({task["id"] for task in only_sync["tasks"]}, {"sync-queued", "sync-done"})
        only_phash_action = _list_tasks(
            self.db_url, queue="all", state="", action="phash_thumbnail_legacy", offset=0, limit=10,
        )
        self.assertEqual([task["queue"] for task in only_phash_action["tasks"]], ["phash"])

    def test_separate_video_job_filters_and_counts(self):
        self.add_video("video-separated")
        for kind in ("phash", "thumbnail", "combined"):
            self.conn.execute(
                "INSERT INTO video_phash_jobs (id,video_id,algorithm_version,decoder_profile,duration_us,submitted_by_user_id,state,job_kind) "
                "VALUES (%s,'video-separated','v1','decoder',1000000,'test','queued',%s)", (kind, kind))
        self.conn.commit()
        for action, expected in (("phash_generation", "phash"), ("thumbnail_generation", "thumbnail"),
                                 ("phash_thumbnail_legacy", "combined")):
            page = _list_tasks(self.db_url, queue="all", state="", action=action, offset=0, limit=10)
            self.assertEqual([task["id"] for task in page["tasks"]], [expected])
            self.assertEqual((page["total"], page["active_total"]), (1, 1))

    def test_history_and_visual_projection_keep_existing_states_and_hide_private_data(self):
        self.add_video("visual-task-video")
        for status in ("queued", "processing", "done", "failed", "cancelled"):
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs(id,action,state,payload,created_at,updated_at) "
                "VALUES (%s,'sync',%s,%s,now(),now())",
                (f"admin-{status}", status, '{"source":"-1001","sync_mode":"new","token":"private-token"}'),
            )
            self.conn.execute(
                "INSERT INTO video_visual_jobs(id,dedupe_key,kind,video_id,algorithm_version,state,lease_token,lease_until,error_code,error_detail,source_key) "
                "VALUES (%s,%s,'signature','visual-task-video','test',%s,%s,%s,%s,'private-error-detail','private-source-url')",
                (f"visual-{status}", f"dedupe-{status}", status,
                 "lease" if status == "processing" else None,
                 int(time.time()) + 30 if status == "processing" else None,
                 "decode_failed" if status == "failed" else None),
            )
        self.conn.commit()
        history = _list_tasks(self.db_url, queue="all", state="history", action="", offset=0, limit=20)
        self.assertEqual(history["total"], 6)
        self.assertEqual({job["state"] for job in history["tasks"]}, {"done", "failed", "cancelled"})
        self.assertNotIn("private-token", str(history))
        self.assertNotIn("private-error-detail", str(history))
        self.assertNotIn("private-source-url", str(history))
        general = next(job for job in history["tasks"] if job["queue"] == "jobs")
        self.assertEqual(general["parameters"], {"source": "-1001", "sync_mode": "new"})
        visual = _list_tasks(self.db_url, queue="visual", state="", action="visual_signature", offset=0, limit=2)
        self.assertEqual((visual["total"], visual["active_total"], visual["truncated"]), (5, 2, True))
        selected = _list_tasks(self.db_url, queue="visual", state="", action="", offset=0, limit=1, job_id="visual-failed")
        self.assertEqual(selected["tasks"][0]["id"], "visual-failed")
        self.assertIn("Visual processing failed", selected["tasks"][0]["error"])
        self.assertEqual(self.conn.execute("SELECT state FROM video_visual_jobs WHERE id='visual-failed'").fetchone()[0], "failed")

    def test_interrupted_sync_history_preserves_its_recorded_outcome(self):
        from admin_v2_sync_history import safe_sync_history
        report = safe_sync_history({"runs": [{"started_at": 1700000000, "success": False,
                                               "interrupted": True, "sources": []}]})
        self.assertTrue(report["runs"][0]["interrupted"])
