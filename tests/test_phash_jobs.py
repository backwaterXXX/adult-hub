"""PostgreSQL-backed invariants for the durable pHash generation queue."""

import time

from common import phash_jobs
from common.telegram_transfer_budget import current_local_day_start_timestamp
from pg_helper import PgTestCase


class TestPhashJobs(PgTestCase):
    algorithm = "test-stash-phash-v1"
    profile = "resolver-ffmpeg-v1"

    def add_video(self, video_id):
        self.conn.execute(
            "INSERT INTO videos (id,provider,title) VALUES (%s,'telegram',%s)",
            (video_id, video_id),
        )
        self.conn.commit()

    def enqueue(self, video_id):
        return phash_jobs.enqueue(
            self.db_url, video_id=video_id, duration_us=120_000_000,
            algorithm_version=self.algorithm, decoder_profile=self.profile,
            user_id="test-admin",
        )

    def test_enqueue_deduplicates_and_claim_allows_only_one_active_job(self):
        first_id = "telegram:-100:1:11"
        second_id = "telegram:-100:1:12"
        self.add_video(first_id)
        self.add_video(second_id)
        first = self.enqueue(first_id)
        duplicate = self.enqueue(first_id)
        second = self.enqueue(second_id)
        self.assertEqual(first["outcome"], "queued")
        self.assertEqual(duplicate["outcome"], "existing")
        self.assertEqual(first["job"]["id"], duplicate["job"]["id"])
        self.assertEqual(second["outcome"], "queued")

        claimed = phash_jobs.claim_next(self.db_url)
        self.assertEqual(claimed["id"], first["job"]["id"])
        self.assertEqual(claimed["attempts"], 1)
        self.assertIsNone(phash_jobs.claim_next(self.db_url))
        self.assertTrue(phash_jobs.heartbeat(self.db_url, claimed["id"], claimed["lease_token"]))
        self.assertTrue(phash_jobs.finish_failure(
            self.db_url, claimed, "source_unavailable", retryable=True,
        ))
        row = self.conn.execute(
            "SELECT state,error_code,lease_token FROM video_phash_jobs WHERE id=%s",
            (claimed["id"],),
        ).fetchone()
        self.assertEqual(tuple(row), ("queued", "source_unavailable", None))

    def test_expired_lease_is_requeued_and_terminal_jobs_can_be_retried(self):
        video_id = "telegram:-100:1:21"
        self.add_video(video_id)
        queued = self.enqueue(video_id)["job"]
        claimed = phash_jobs.claim_next(self.db_url)
        self.assertEqual(claimed["id"], queued["id"])
        self.conn.execute(
            "UPDATE video_phash_jobs SET lease_until=CURRENT_TIMESTAMP - INTERVAL '1 second' WHERE id=%s",
            (claimed["id"],),
        )
        self.conn.commit()
        self.assertEqual(phash_jobs.recover_expired(self.db_url), 1)
        recovered = self.conn.execute(
            "SELECT state,error_code,lease_token FROM video_phash_jobs WHERE id=%s",
            (claimed["id"],),
        ).fetchone()
        self.assertEqual(tuple(recovered), ("queued", "worker_restarted", None))

        claimed = phash_jobs.claim_next(self.db_url)
        self.assertTrue(phash_jobs.finish_failure(
            self.db_url, claimed, "invalid_video", retryable=False,
        ))
        self.assertTrue(phash_jobs.retry_job(self.db_url, claimed["id"]))
        self.assertFalse(phash_jobs.retry_job(self.db_url, claimed["id"]))
        self.assertEqual(
            self.conn.execute("SELECT state,attempts FROM video_phash_jobs WHERE id=%s", (claimed["id"],)).fetchone(),
            ("queued", 0),
        )

    def test_done_requires_matching_provenance_and_thumbnail(self):
        video_id = "telegram:-100:1:31"
        fingerprint = "0123456789abcdef"
        self.add_video(video_id)
        self.conn.execute(
            "INSERT INTO video_fingerprints(video_id,algorithm,fingerprint) VALUES (%s,'PHASH',%s)",
            (video_id, fingerprint),
        )
        self.conn.execute(
            "INSERT INTO video_phash_provenance(video_id,fingerprint,generator_version,decoder_profile,submitted_by_user_id) VALUES (%s,%s,%s,%s,%s)",
            (video_id, fingerprint, self.algorithm + ":" + self.profile, self.profile, "test-admin"),
        )
        self.conn.commit()
        queued = self.enqueue(video_id)
        self.assertEqual(queued["outcome"], "queued")
        self.assertEqual(queued["job"]["state"], "queued")

        self.conn.execute(
            "INSERT INTO video_phash_thumbnails(video_id,image_bytes) VALUES (%s,%s)",
            (video_id, b"jpeg-test"),
        )
        self.conn.commit()
        complete = self.enqueue(video_id)
        self.assertEqual(complete["outcome"], "complete")
        self.assertEqual(complete["job"]["state"], "done")

    def fail_on_budget(self, video_id):
        """Drive a job to a terminal daily-transfer-budget failure and return it."""
        job = self.enqueue(video_id)["job"]
        claimed = phash_jobs.claim_next(self.db_url)
        self.assertEqual(claimed["id"], job["id"])
        self.assertTrue(phash_jobs.finish_failure(
            self.db_url, claimed, "daily_transfer_budget_exhausted", retryable=False,
        ))
        return job

    def set_failure_time(self, job_id, epoch):
        self.conn.execute(
            "UPDATE video_phash_jobs SET updated_at=to_timestamp(%s) WHERE id=%s",
            (epoch, job_id),
        )
        self.conn.commit()

    def test_budget_failure_reatttempts_only_after_the_local_day_rolls_over(self):
        """A budget-stopped job rejoins the queue once the daily cap resets."""
        video_id = "telegram:-100:1:41"
        self.add_video(video_id)
        job = self.fail_on_budget(video_id)

        # The shared adapter hands timestamptz columns back as epoch seconds,
        # so the requeue decision must read that shape, not a datetime.
        persisted = self.conn.execute(
            "SELECT updated_at FROM video_phash_jobs WHERE id=%s", (job["id"],),
        ).fetchone()
        self.assertIsInstance(persisted[0], int)

        day_start = current_local_day_start_timestamp()
        # Anywhere inside the current budget day: the cap is still spent.
        self.set_failure_time(job["id"], day_start + (time.time() - day_start) / 2)
        same_day = self.enqueue(video_id)
        self.assertEqual(same_day["outcome"], "terminal")
        self.assertEqual(same_day["job"]["state"], "failed")
        self.assertEqual(same_day["job"]["error_code"], "daily_transfer_budget_exhausted")

        # Failed before this local day began: the budget reset, so requeue.
        self.set_failure_time(job["id"], day_start - 1)
        after_reset = self.enqueue(video_id)
        self.assertEqual(after_reset["outcome"], "queued")
        self.assertEqual(after_reset["job"]["state"], "queued")
        self.assertEqual(after_reset["job"]["attempts"], 0)
        self.assertIsNone(after_reset["job"]["error_code"])
