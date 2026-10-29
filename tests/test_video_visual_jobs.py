"""Visual job queue under more than one worker.

``video_visual_jobs_single_active_idx`` keeps the hub to one active job, so a
second worker that loses the race must step back instead of raising, and the
startup sweep a restarting worker runs must only reclaim jobs whose heartbeat
went stale - never a lease another worker is still renewing.
"""

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import video_visual_jobs


class VisualJobQueueTest(PgTestCase):
    def setUp(self):
        super().setUp()
        self._seq = 0

    # -- fixtures -----------------------------------------------------------

    def _video(self, index: int) -> str:
        video_id = f"telegram:-100200200:30{index:03d}:40{index:03d}"
        self.conn.execute(
            "INSERT INTO videos (id,provider,title,duration,first_seen,last_seen)"
            " VALUES (%s,'telegram',%s,600,1,1)", (video_id, video_id))
        self.conn.commit()
        return video_id

    def _job(self, kind: str, *, state: str = "queued", video_id: str = "",
             left_id: str = "", right_id: str = "", lease_seconds: int = 0) -> str:
        self._seq += 1
        now = int(time.time())
        if kind == "signature":
            job_id, video_ref = f"vs-{self._seq:08d}", video_id or self._video(self._seq)
            self.conn.execute(
                """INSERT INTO video_visual_jobs
                   (id,dedupe_key,kind,video_id,algorithm_version,state,priority,attempts,
                    available_at,lease_token,lease_until)
                   VALUES (%s,%s,'signature',%s,'visual-sequence-v1',%s,30,0,%s,%s,%s)""",
                (job_id, f"dk-{job_id}", video_ref, state, now,
                 "token" if state == "processing" else None,
                 now + lease_seconds if state == "processing" else None))
        else:
            left = left_id or self._video(self._seq * 2)
            right = right_id or self._video(self._seq * 2 + 1)
            job_id = f"vp-{self._seq:08d}"
            self.conn.execute(
                """INSERT INTO video_visual_jobs
                   (id,dedupe_key,kind,left_id,right_id,algorithm_version,state,priority,attempts,
                    available_at,lease_token,lease_until)
                   VALUES (%s,%s,'verify_pair',%s,%s,'visual-sequence-v1',%s,20,0,%s,%s,%s)""",
                (job_id, f"dk-{job_id}", left, right, state, now,
                 "token" if state == "processing" else None,
                 now + lease_seconds if state == "processing" else None))
            self.conn.execute(
                """INSERT INTO video_visual_comparisons
                   (id,left_id,right_id,algorithm_version,left_source_key,right_source_key,state)
                   VALUES (%s,%s,%s,'visual-sequence-v1','l','r','processing')""",
                (job_id, left, right))
        self.conn.commit()
        return job_id

    def _row(self, job_id: str):
        return self.conn.execute(
            "SELECT state,lease_token FROM video_visual_jobs WHERE id=%s", (job_id,)).fetchone()

    def _comparison_state(self, job_id: str):
        return self.conn.execute(
            "SELECT state FROM video_visual_comparisons WHERE id=%s", (job_id,)).fetchone()[0]

    # -- claiming while a peer holds the single active slot ------------------

    def test_a_lost_claim_race_returns_no_job_instead_of_failing(self):
        self._job("signature")
        claimed = video_visual_jobs.claim_next(self.db_url)
        self.assertIsNotNone(claimed, "the first worker must take the queued job")
        self._job("signature")

        # The queue still holds a claimable row, but the single-active index is
        # taken: this worker has to report "nothing for me".
        self.assertIsNone(video_visual_jobs.claim_next(self.db_url))

    def test_concurrent_workers_leave_exactly_one_claim(self):
        for _ in range(3):
            self._job("signature")
        results, errors = [], []
        started = threading.Barrier(4)

        def compete():
            try:
                started.wait(timeout=10)
                results.append(video_visual_jobs.claim_next(self.db_url))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=compete) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [], "a lost race must not surface as an error")
        self.assertEqual(sum(result is not None for result in results), 1)
        active = self.conn.execute(
            "SELECT count(*) FROM video_visual_jobs WHERE state='processing'").fetchone()[0]
        self.assertEqual(active, 1)

    # -- lease recovery ------------------------------------------------------

    def test_a_second_worker_startup_does_not_steal_a_live_lease(self):
        job_id = self._job("signature")
        claimed = video_visual_jobs.claim_next(self.db_url)
        self.assertEqual(claimed["id"], job_id)

        recovered = video_visual_jobs.recover_expired(self.db_url, force=True)

        self.assertEqual(recovered, 0)
        row = self._row(job_id)
        self.assertEqual(row[0], "processing")
        self.assertEqual(row[1], claimed["lease_token"], "the live lease token must survive")

    def test_a_startup_sweep_reclaims_a_job_whose_worker_stopped_beating(self):
        job_id = self._job("verify_pair")
        claimed = video_visual_jobs.claim_next(self.db_url)
        self.assertEqual(claimed["id"], job_id)
        with self.conn:
            self.conn.execute("UPDATE video_visual_jobs SET updated_at = %s WHERE id=%s",
                              (int(time.time()) - video_visual_jobs.LEASE_STALE_SECONDS - 1, job_id))

        recovered = video_visual_jobs.recover_expired(self.db_url, force=True)

        self.assertEqual(recovered, 1)
        self.assertEqual(self._row(job_id), ("queued", None))
        self.assertEqual(self._comparison_state(job_id), "queued")

    def test_the_periodic_sweep_leaves_a_renewing_lease_alone(self):
        job_id = self._job("signature", state="processing", lease_seconds=600)
        with self.conn:
            self.conn.execute("UPDATE video_visual_jobs SET updated_at = %s WHERE id=%s",
                              (int(time.time()) - 20, job_id))

        self.assertEqual(video_visual_jobs.recover_expired(self.db_url), 0)
        self.assertEqual(self._row(job_id), ("processing", "token"))

    def test_an_expired_lease_is_recovered_without_a_forced_sweep(self):
        job_id = self._job("signature", state="processing", lease_seconds=-1)

        self.assertEqual(video_visual_jobs.recover_expired(self.db_url), 1)
        self.assertEqual(self._row(job_id), ("queued", None))

    def test_the_sweep_only_reopens_comparisons_it_actually_recovered(self):
        # A comparison still marked processing while its job already finished is
        # not this sweep's business: it belongs to another stage of the pipeline.
        finished = self._job("verify_pair", state="processing", lease_seconds=-1)
        with self.conn:
            self.conn.execute(
                "UPDATE video_visual_jobs SET state='done',lease_token=NULL,lease_until=NULL"
                " WHERE id=%s", (finished,))

        live = self._job("verify_pair", state="processing", lease_seconds=-1)
        recovered = video_visual_jobs.recover_expired(self.db_url)

        self.assertEqual(recovered, 1)
        self.assertEqual(self._comparison_state(live), "queued")
        self.assertEqual(self._comparison_state(finished), "processing")


if __name__ == "__main__":
    unittest.main()
