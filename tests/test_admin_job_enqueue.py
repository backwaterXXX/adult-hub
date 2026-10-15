"""Durable enqueue deduplication across independent PostgreSQL connections."""
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase, reset_db

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'resolver'))
from video_duplicates import enqueue_job
from common import db


class TestAdminJobEnqueue(PgTestCase):
    def test_heartbeat_job_refreshes_only_a_processing_task(self):
        from common.job_queue import heartbeat_job

        sync_id = enqueue_job(self.db_url, "sync", {})
        from common.job_queue import claim_job
        self.assertIsNotNone(claim_job(self.db_url, "sync"))
        self.assertTrue(heartbeat_job(self.db_url, sync_id, now=123456))
        row = self.conn.execute(
            "SELECT state,updated_at FROM video_duplicate_jobs WHERE id=%s", (sync_id,)
        ).fetchone()
        self.assertEqual(row, ("processing", 123456))

        with self.conn:
            self.conn.execute(
                "UPDATE video_duplicate_jobs SET state='done' WHERE id=%s", (sync_id,)
            )
        self.assertFalse(heartbeat_job(self.db_url, sync_id, now=123999))
        self.assertEqual(
            self.conn.execute(
                "SELECT updated_at FROM video_duplicate_jobs WHERE id=%s", (sync_id,)
            ).fetchone()[0],
            123456,
        )

    def test_pending_scan_is_read_only_and_preserves_worker_ownership(self):
        from common.job_queue import pending_actions
        self.assertEqual(pending_actions(self.db_url), set())
        sync_id = enqueue_job(self.db_url, "sync", {})
        worker_id = enqueue_job(self.db_url, "quarantine", {"video_id": "test"})
        with self.conn:
            self.conn.execute("UPDATE video_duplicate_jobs SET state='processing'")
        self.assertEqual(pending_actions(self.db_url), {"quarantine"})
        with self.conn:
            states = dict(self.conn.execute("SELECT id,state FROM video_duplicate_jobs").fetchall())
        self.assertEqual(states, {sync_id: "processing", worker_id: "processing"})

    def test_claim_reports_database_lock_instead_of_empty_queue(self):
        from common import job_queue
        enqueue_job(self.db_url, "sync", {})
        with patch.object(job_queue, "_open", side_effect=db.OperationalError("database locked")):
            with self.assertRaises(db.OperationalError):
                job_queue.claim_job(self.db_url, "sync")

    def test_active_jobs_reuse_id_across_calls_and_payload_key_order(self):
        first = enqueue_job(self.db_url, 'performer_merge', {'merges': [{'alias': 'A', 'canonical': 'B'}]})
        equivalent = {'merges': [{'canonical': 'B', 'alias': 'A'}]}
        self.assertEqual(enqueue_job(self.db_url, 'performer_merge', equivalent), first)
        with self.conn:
            self.conn.execute("UPDATE video_duplicate_jobs SET state='processing' WHERE id=%s", (first,))
        self.assertEqual(enqueue_job(self.db_url, 'performer_merge', equivalent), first)

    def test_terminal_jobs_allow_new_request(self):
        for state in ('done', 'failed'):
            with self.subTest(state=state):
                reset_db(self.db_url)
                first = enqueue_job(self.db_url, 'collections_refresh', {})
                with self.conn:
                    self.conn.execute('UPDATE video_duplicate_jobs SET state=%s WHERE id=%s', (state, first))
                self.assertNotEqual(enqueue_job(self.db_url, 'collections_refresh', {}), first)

    def test_different_actions_or_merge_targets_remain_distinct(self):
        first = enqueue_job(self.db_url, 'performer_merge', {'merges': [{'alias': 'A', 'canonical': 'B'}]})
        second = enqueue_job(self.db_url, 'performer_merge', {'merges': [{'alias': 'A', 'canonical': 'C'}]})
        collection = enqueue_job(self.db_url, 'collections_refresh', {})
        self.assertEqual(len({first, second, collection}), 3)

    def test_hide_loser_order_and_dismiss_pair_order_are_semantic(self):
        hide = enqueue_job(self.db_url, 'hide', {'keeper_id': 'k', 'loser_ids': ['a', 'b']})
        self.assertEqual(enqueue_job(self.db_url, 'hide', {'loser_ids': ['b', 'a'], 'keeper_id': 'k'}), hide)
        dismiss = enqueue_job(self.db_url, 'dismiss', {'left_id': 'a', 'right_id': 'b'})
        self.assertEqual(enqueue_job(self.db_url, 'dismiss', {'left_id': 'b', 'right_id': 'a'}), dismiss)

    def test_changed_fingerprints_are_not_deduplicated(self):
        first = enqueue_job(self.db_url, 'hide', {'keeper_id': 'k', 'loser_ids': ['a'], 'fingerprints': {'a': 'old'}})
        second = enqueue_job(self.db_url, 'hide', {'keeper_id': 'k', 'loser_ids': ['a'], 'fingerprints': {'a': 'new'}})
        self.assertNotEqual(first, second)

    def test_concurrent_connections_enqueue_one_pending_job(self):
        seed = enqueue_job(self.db_url, 'collections_refresh', {})
        with self.conn:
            self.conn.execute("UPDATE video_duplicate_jobs SET state='done' WHERE id=%s", (seed,))
        workers = 8
        start = threading.Barrier(workers)

        def enqueue(_):
            start.wait(timeout=5)
            return enqueue_job(self.db_url, 'collections_refresh', {})

        with ThreadPoolExecutor(max_workers=workers) as pool:
            ids = list(pool.map(enqueue, range(workers)))
        self.assertEqual(len(set(ids)), 1)
        count = self.conn.execute("SELECT count(*) FROM video_duplicate_jobs WHERE state='queued'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_sync_jobs_deduplicate_by_normalized_source(self):
        first = enqueue_job(self.db_url, 'sync', {'source': 'Chan'})
        self.assertEqual(enqueue_job(self.db_url, 'sync', {'source': ' Chan '}), first)
        self.assertEqual(enqueue_job(self.db_url, 'sync', {}), enqueue_job(self.db_url, 'sync', {'source': None}))
        self.assertNotEqual(enqueue_job(self.db_url, 'sync', {'source': 'Other'}), first)
        backfill = enqueue_job(self.db_url, 'sync', {'source': 'Chan', 'sync_mode': 'backfill'})
        self.assertNotEqual(backfill, first)
        self.assertEqual(enqueue_job(self.db_url, 'sync', {'source': 'Chan', 'sync_mode': 'backfill'}), backfill)

    def test_retry_requeues_terminal_job_and_missing_returns_none(self):
        from video_duplicates import retry_job
        original = enqueue_job(self.db_url, 'performer_merge', {'merges': [{'alias': 'A', 'canonical': 'B'}]})
        with self.conn:
            self.conn.execute("UPDATE video_duplicate_jobs SET state='failed' WHERE id=%s", (original,))
        retried = retry_job(self.db_url, original)
        self.assertNotEqual(retried, original)
        payload, state = self.conn.execute(
            'SELECT payload,state FROM video_duplicate_jobs WHERE id=%s', (retried,)).fetchone()
        self.assertEqual(state, 'queued')
        self.assertEqual(json.loads(payload), {'merges': [{'alias': 'A', 'canonical': 'B'}]})
        self.assertIsNone(retry_job(self.db_url, 'missing'))

    def test_retry_does_not_requeue_a_successfully_completed_job(self):
        from common.job_queue import retry_job
        original = enqueue_job(self.db_url, 'collections_refresh', {})
        with self.conn:
            self.conn.execute(
                "UPDATE video_duplicate_jobs SET state='done',result=%s WHERE id=%s",
                (json.dumps({"collections_refreshed": 3}), original),
            )
        before = self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0]
        self.assertIsNone(retry_job(self.db_url, original))
        after = self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(
            self.conn.execute("SELECT state FROM video_duplicate_jobs WHERE id=%s", (original,)).fetchone()[0],
            "done",
        )

    def test_cancelled_job_is_terminal_and_can_be_retried(self):
        from common.job_queue import cancel_queued_job, read_job, retry_job
        original = enqueue_job(self.db_url, 'collections_refresh', {})
        self.assertEqual(cancel_queued_job(self.db_url, original), {"outcome": "cancelled", "state": "cancelled"})
        job = read_job(self.db_url, original)
        self.assertEqual(job["state"], "cancelled")
        result = json.loads(job["result"])
        self.assertEqual(result["progress"]["stage"], "cancelled")
        self.assertEqual(result["events"][-1]["stage"], "cancelled")
        retried = retry_job(self.db_url, original)
        self.assertNotEqual(retried, original)
        self.assertEqual(read_job(self.db_url, retried)["state"], "queued")

    def test_cancel_refuses_processing_and_terminal_tasks(self):
        from common.job_queue import cancel_queued_job
        for state in ('processing', 'done', 'failed', 'cancelled'):
            with self.subTest(state=state):
                reset_db(self.db_url)
                job_id = enqueue_job(self.db_url, 'collections_refresh', {})
                with self.conn:
                    self.conn.execute('UPDATE video_duplicate_jobs SET state=%s WHERE id=%s', (state, job_id))
                self.assertEqual(cancel_queued_job(self.db_url, job_id), {"outcome": "conflict", "state": state})
        self.assertEqual(cancel_queued_job(self.db_url, 'missing'), {"outcome": "not_found"})

    def test_cancel_races_atomically_with_worker_claim(self):
        from common.job_queue import cancel_queued_job, claim_job, read_job
        job_id = enqueue_job(self.db_url, 'collections_refresh', {})
        start = threading.Barrier(2)

        def cancel():
            start.wait(timeout=5)
            return cancel_queued_job(self.db_url, job_id)

        def claim():
            start.wait(timeout=5)
            return claim_job(self.db_url, 'collections_refresh')

        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel_result, claimed = [future.result(timeout=10) for future in (pool.submit(cancel), pool.submit(claim))]
        final_state = read_job(self.db_url, job_id)['state']
        if cancel_result['outcome'] == 'cancelled':
            self.assertIsNone(claimed)
            self.assertEqual(final_state, 'cancelled')
        else:
            self.assertEqual(cancel_result['outcome'], 'conflict')
            self.assertIsNotNone(claimed)
            self.assertEqual(final_state, 'processing')

    def test_preexisting_rows_are_preserved(self):
        original = enqueue_job(self.db_url, 'undo', {'operation_id': 'old-op'})
        before = self.conn.execute('SELECT * FROM video_duplicate_jobs WHERE id=%s', (original,)).fetchone()
        self.assertEqual(enqueue_job(self.db_url, 'undo', {'operation_id': 'old-op'}), original)
        after = self.conn.execute('SELECT * FROM video_duplicate_jobs WHERE id=%s', (original,)).fetchone()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
