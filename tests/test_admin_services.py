"""Read-only job inspection must not alter worker recovery state."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase, TEST_URL

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'resolver'))
import admin
import admin_services
from common import db
from common.job_queue import enqueue_job, retry_job


BAD_URL = "postgresql://127.0.0.1:1/missing"


class TestAdminJobInspection(PgTestCase):
    def test_missing_database_reports_no_jobs(self):
        self.assertEqual(admin_services.job_listing(BAD_URL)["jobs"], [])
        self.assertIsNone(admin_services.get_job(BAD_URL, 'missing'))

    def test_existing_database_without_jobs_is_unchanged(self):
        conn = db.connect(TEST_URL)
        try:
            before = conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(admin_services.job_listing(TEST_URL)["jobs"], [])
        self.assertIsNone(admin_services.get_job(TEST_URL, 'missing'))
        conn = db.connect(TEST_URL)
        try:
            after = conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(after, before)

    def test_sensitive_recovery_data_is_hidden_but_retained_for_worker(self):
        job_id = enqueue_job(TEST_URL, 'hide', {'fingerprints': {'private-path': 'fingerprint'}, 'keeper_id': 'v1'})
        recovery = {'operation_id': 'op-1', 'snapshots': [{'credential': 'private-secret'}]}
        conn = db.connect(TEST_URL)
        with conn:
            conn.execute('UPDATE video_duplicate_jobs SET result=%s WHERE id=%s',
                         (json.dumps(recovery), job_id))
        conn.close()
        public = admin_services.get_job(TEST_URL, job_id)
        self.assertEqual(public['result'], {'operation_id': 'op-1', 'recovery': 'in_progress'})
        self.assertNotIn('fingerprints', public['payload'])
        self.assertNotIn('secret', json.dumps(public))
        self.assertNotIn('secret', json.dumps(admin_services.job_listing(TEST_URL)["jobs"]))
        conn = db.connect(TEST_URL)
        try:
            result, payload = conn.execute('SELECT result,payload FROM video_duplicate_jobs WHERE id=%s', (job_id,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(json.loads(result), recovery)
        self.assertEqual(json.loads(payload)['fingerprints'], {'private-path': 'fingerprint'})

    def test_nested_failure_error_is_redacted(self):
        job_id = enqueue_job(TEST_URL, 'performer_merge', {'merges': []})
        conn = db.connect(TEST_URL)
        with conn:
            conn.execute('UPDATE video_duplicate_jobs SET result=%s WHERE id=%s',
                         (json.dumps({'error': 'https://private-token@remote.example/'}), job_id))
        conn.close()
        self.assertNotIn('private-token', json.dumps(admin_services.get_job(TEST_URL, job_id)))

    def test_unresolved_failure_listing_reconciles_full_history_and_pages_groups(self):
        failed_a = enqueue_job(TEST_URL, 'sync', {
            'source': 'telegram:one', 'fingerprints': {'path': 'old'},
        })
        failed_b = enqueue_job(TEST_URL, 'sync', {
            'source': 'telegram:one', 'fingerprints': {'path': 'new'},
        })
        resolved = enqueue_job(TEST_URL, 'sync', {'source': 'telegram:resolved'})
        failed_c = enqueue_job(TEST_URL, 'backup_create', {'scope': 'database'})
        completed = enqueue_job(TEST_URL, 'sync', {'source': 'telegram:resolved'})
        conn = db.connect(TEST_URL)
        try:
            with conn:
                conn.execute("UPDATE video_duplicate_jobs SET state='failed',updated_at=to_timestamp(10) WHERE id=%s",
                             (failed_a,))
                conn.execute("UPDATE video_duplicate_jobs SET state='failed',updated_at=to_timestamp(11) WHERE id=%s",
                             (failed_b,))
                conn.execute("UPDATE video_duplicate_jobs SET state='failed',updated_at=to_timestamp(20) WHERE id=%s",
                             (resolved,))
                conn.execute("UPDATE video_duplicate_jobs SET state='done',updated_at=to_timestamp(30) WHERE id=%s",
                             (completed,))
                conn.execute("UPDATE video_duplicate_jobs SET state='failed',updated_at=to_timestamp(40) WHERE id=%s",
                             (failed_c,))
        finally:
            conn.close()

        first_page = admin_services.unresolved_failed_task_listing(TEST_URL, limit=1)
        self.assertTrue(first_page['available'])
        self.assertEqual(first_page['total'], 2)
        self.assertTrue(first_page['truncated'])
        self.assertEqual(first_page['jobs'][0]['id'], failed_c)
        self.assertEqual(first_page['jobs'][0]['attempts'], 1)

        all_groups = admin_services.unresolved_failed_task_listing(TEST_URL, limit=10)
        self.assertEqual(all_groups['total'], 2)
        by_action = {job['action']: job for job in all_groups['jobs']}
        self.assertEqual(by_action['sync']['id'], failed_b)
        self.assertEqual(by_action['sync']['attempts'], 2)
        self.assertFalse(all_groups['truncated'])

    def test_successful_retry_with_internal_resume_payload_resolves_original_failure(self):
        failed_id = enqueue_job(TEST_URL, 'metadata_update', {
            'video_id': 'video-1', 'fields': {'title': 'Reviewed title'},
        })
        conn = db.connect(TEST_URL)
        try:
            with conn:
                conn.execute(
                    "UPDATE video_duplicate_jobs SET state='failed',result=%s,updated_at=to_timestamp(10) WHERE id=%s",
                    (json.dumps({"nfo_written": True, "metadata_revision": 3}), failed_id),
                )
        finally:
            conn.close()

        retried_id = retry_job(TEST_URL, failed_id)
        self.assertIsNotNone(retried_id)
        conn = db.connect(TEST_URL)
        try:
            retried_payload = conn.execute(
                "SELECT payload FROM video_duplicate_jobs WHERE id=%s", (retried_id,)
            ).fetchone()[0]
            with conn:
                conn.execute(
                    "UPDATE video_duplicate_jobs SET state='done',updated_at=to_timestamp(20) WHERE id=%s",
                    (retried_id,),
                )
        finally:
            conn.close()
        self.assertIn('_resume', json.loads(retried_payload))

        self.assertEqual(admin_services.unresolved_failed_task_summary(TEST_URL), {
            'available': True, 'count': 0,
        })
        listing = admin_services.unresolved_failed_task_listing(TEST_URL)
        self.assertTrue(listing['available'])
        self.assertEqual(listing['total'], 0)
        self.assertEqual(listing['jobs'], [])

    def test_missing_database_stats_use_unknown_counts(self):
        with patch.object(admin, 'HUB_DB_URL', BAD_URL):
            stats = admin._db_stats()
        self.assertFalse(stats['db_ok'])
        self.assertIsNone(stats['videos_total'])
        self.assertIsNone(stats['files_total'])
