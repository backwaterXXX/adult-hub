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
