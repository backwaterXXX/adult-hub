"""Request-level checks for resolver admin maintenance, jobs, backups, and sync."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pg_helper import TEST_URL
from test_support import load_sync_module

import resolver.admin as admin_module
import resolver.app as resolver_app
from tests.helpers.db_session import db_session
from tests.test_http_api import BAD_URL, BaseHttpApiTestCase, get_job


class TestResolverHttpApiMaintenance(BaseHttpApiTestCase):
    def test_admin_v2_jobs_return_active_count_or_explicit_unknown(self):
        from common.job_queue import enqueue_job

        enqueue_job(TEST_URL, "sync", {})
        with self.admin_config(), patch.object(admin_module, "HUB_DB_URL", TEST_URL):
            available = self.request(
                "GET", "/admin/api/v2/jobs?state=active&limit=1", base_url="http://localhost"
            )
        self.assertEqual(available.status_code, 200, available.text)
        self.assertTrue(available.json()["data"]["available"])
        self.assertEqual(available.json()["active_total"], 1)

        with self.admin_config(), patch.object(admin_module, "HUB_DB_URL", BAD_URL):
            unavailable = self.request(
                "GET", "/admin/api/v2/jobs?state=active&limit=1", base_url="http://localhost"
            )
        self.assertEqual(unavailable.status_code, 200, unavailable.text)
        self.assertFalse(unavailable.json()["data"]["available"])
        self.assertIsNone(unavailable.json()["active_total"])


    def test_v2_backup_inventory_and_verification_are_read_only_and_path_safe(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            root = Path(directory)
            backup = root / "adult_hub-test.db"
            backup.write_bytes(b"legacy sqlite snapshot")
            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
            (root / f"{backup.name}.sha256").write_text(f"{digest}  {backup.name}\n", encoding="ascii")
            with patch.object(admin_module, "BACKUP_DIR", root), \
                 patch.object(admin_module, "BACKUP_RESTORE_DATABASE_URL", ""):
                inventory = self.request("GET", "/admin/api/v2/backups", base_url="http://localhost")
                valid = self.request("GET", f"/admin/api/v2/backups/verify?filename={backup.name}", base_url="http://localhost")
                traversal = self.request("GET", "/admin/api/v2/backups/verify?filename=..%2Fsecret.db", base_url="http://localhost")
        self.assertEqual(inventory.status_code, 200, inventory.text)
        self.assertFalse(inventory.json()["restore_test_available"])
        self.assertNotIn("DATABASE_URL", inventory.text)
        self.assertEqual(inventory.json()["items"][0]["scope"], "Legacy database snapshot; media and settings are not included")
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(valid.json()["archive_status"], "unsupported_format")
        self.assertEqual(valid.json()["checksum_status"], "valid")
        self.assertEqual(traversal.status_code, 404)


    def test_v2_backup_restore_test_queues_only_a_safe_filename(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config(), \
             patch.object(admin_module, "BACKUP_DIR", Path(directory)), \
             patch.object(admin_module, "BACKUP_RESTORE_DATABASE_URL", "postgresql://user:secret@localhost:5432/maintenance"), \
             patch.object(admin_module, "HUB_DB_URL", TEST_URL):
            backup = Path(directory) / "adulthub-test.dump.gz"
            backup.write_bytes(b"compressed backup placeholder")
            denied = self.request("POST", "/admin/api/v2/backups/restore-test", base_url="http://localhost",
                                  headers={"Origin": "http://localhost"}, json={"filename": backup.name})
            accepted = self.request("POST", "/admin/api/v2/backups/restore-test", base_url="http://localhost",
                                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                                    json={"filename": backup.name})
            job = get_job(TEST_URL, accepted.json()["job_id"])
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(job["action"], "backup_restore_test")
        self.assertEqual(job["payload"], {"filename": backup.name})


    def test_v2_manual_backup_queues_database_only_job_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            trigger = Path(directory) / "sync.trigger"
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "SYNC_TRIGGER_FILE", trigger):
                responses = [self.request(
                    "POST", "/admin/api/v2/backups/create", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                ) for _ in range(2)]
            for response in responses:
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(response.json()["state"], "queued")
            self.assertEqual(responses[0].json()["job_id"], responses[1].json()["job_id"])
            job = get_job(TEST_URL, responses[0].json()["job_id"])
            self.assertEqual((job["action"], job["state"], job["payload"]),
                             ("backup_create", "queued", {}))
            self.assertEqual(self._job_count(), 1)
            self.assertEqual(json.loads(trigger.read_text()), {"action": "jobs"})


    def test_v2_backup_restore_inventory_reports_disposable_target_availability(self):
        with self.admin_config(), patch.object(admin_module, "BACKUP_DIR", Path("/tmp/no-backups")), \
             patch.object(admin_module, "BACKUP_RESTORE_DATABASE_URL", "postgresql://user:secret@localhost:5432/maintenance"), \
             patch.object(admin_module, "HUB_DB_URL", "postgresql://app:secret@localhost:5432/catalog"):
            result = self.request("GET", "/admin/api/v2/backups", base_url="http://localhost")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["restore_test_available"])
        self.assertNotIn("secret", result.text)
        self.assertNotIn("maintenance", result.text)


    def test_v2_restore_test_rejects_malformed_or_primary_database_target(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            backup = Path(directory) / "adulthub-test.dump.gz"
            backup.write_bytes(b"compressed backup placeholder")
            for target in ("not-a-postgres-uri", TEST_URL):
                with self.subTest(target="primary" if target == TEST_URL else "malformed"), \
                     patch.object(admin_module, "BACKUP_DIR", Path(directory)), \
                     patch.object(admin_module, "BACKUP_RESTORE_DATABASE_URL", target), \
                     patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                    inventory = self.request("GET", "/admin/api/v2/backups", base_url="http://localhost")
                    rejected = self.request(
                        "POST", "/admin/api/v2/backups/restore-test", base_url="http://localhost",
                        headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                        json={"filename": backup.name},
                    )
                self.assertEqual(inventory.status_code, 200)
                self.assertFalse(inventory.json()["restore_test_available"])
                self.assertEqual(rejected.status_code, 503, rejected.text)
                self.assertNotIn("postgres", rejected.text.lower())
                self.assertEqual(self._job_count(), 0)


    def test_v2_schedules_use_shared_write_policy_and_revisioned_enable(self):
        with self.admin_config(), patch.object(admin_module, "HUB_DB_URL", TEST_URL):
            denied = self.request(
                "POST", "/admin/api/v2/schedules", base_url="http://localhost",
                headers={"Origin": "http://localhost"},
                json={"name": "Nightly audit", "action": "consistency_audit", "schedule_type": "daily", "daily_at": "03:00"},
            )
            created = self.request(
                "POST", "/admin/api/v2/schedules", base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={"name": "Nightly audit", "action": "consistency_audit", "schedule_type": "daily", "daily_at": "03:00", "timezone": "Europe/Warsaw"},
            )
            schedule_id = created.json()["id"]
            listing = self.request("GET", "/admin/api/v2/schedules", base_url="http://localhost")
            disabled = self.request(
                "POST", f"/admin/api/v2/schedules/{schedule_id}/enabled", base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={"enabled": False, "revision": 1},
            )
            conflict = self.request(
                "POST", f"/admin/api/v2/schedules/{schedule_id}/enabled", base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={"enabled": True, "revision": 1},
            )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["items"][0]["timezone"], "Europe/Warsaw")
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(conflict.status_code, 409, conflict.text)


    def test_v2_cache_clear_requires_write_access_and_reports_count(self):
        clear = Mock(return_value=6)
        with self.admin_config(), patch.object(resolver_app.app.state, "admin_cache_clear", clear):
            denied = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="http://localhost",
                                  headers={"Origin": "http://localhost"}, json={})
            clear.assert_not_called()
            allowed = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="http://localhost",
                                   headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["cleared"], 6)
        clear.assert_called_once_with()


    def test_v2_cache_clear_empties_real_persistent_resolver_cache(self):
        from resolver.stream_cache import PersistentLRUCache

        cache = PersistentLRUCache(db_url=TEST_URL, max_entries=4, ttl_seconds=60)
        cache.set("v2-clear-one", "https://cdn.example/one")
        cache.set("v2-clear-two", "https://cdn.example/two")
        headers = {
            "Origin": "http://localhost",
            "Authorization": "Bearer test-write-token",
        }
        try:
            with self.admin_config(), \
                 patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(resolver_app, "cache", cache), \
                 patch.object(resolver_app.app.state, "admin_cache_clear", resolver_app._admin_cache_clear):
                response = self.request(
                    "POST", "/admin/api/v2/maintenance/cache/clear",
                    base_url="http://localhost", headers=headers, json={},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"ok": True, "cleared": 2})
            self.assertEqual(len(cache), 0)
            self.assertIsNone(cache.get("v2-clear-one"))
            with db_session(TEST_URL) as conn:
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM stream_cache WHERE cache_key LIKE 'v2-clear-%'"
                ).fetchone()[0], 0)
        finally:
            cache.close()


    def test_authorized_cache_clear_executes_once_and_denied_request_has_no_effect(self):
        clear = Mock(return_value=7)
        with self.admin_config(), patch.object(resolver_app.app.state, "admin_cache_clear", clear):
            denied = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="http://localhost",
                                  headers={"Origin": "http://localhost"})
            clear.assert_not_called()
            allowed = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["cleared"], 7)
        clear.assert_called_once_with()


    def test_v2_sync_supports_global_scheduled_and_per_source_modes(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            config = Path(directory) / "config.yaml"
            config.write_text(
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Alpha'\n",
                encoding="utf-8",
            )
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "CONFIG_PATH", config), \
                 patch("resolver.video_duplicates.enqueue_job", side_effect=["job-global", "job-scheduled", "job-backfill"]) as enqueue, \
                 patch.object(admin_module, "_wake_worker") as wake:
                headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
                global_sync = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                           headers=headers, json={})
                scheduled = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                         headers=headers, json={"source": "-100111", "sync_mode": "scheduled"})
                backfill = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                        headers=headers, json={"source": "-100111", "sync_mode": "backfill"})
                invalid = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                       headers=headers, json={"source": "bad", "sync_mode": "scheduled"})
                empty_source = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                             headers=headers, json={"source": "", "sync_mode": "scheduled"})
                malformed = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                         headers=headers, content=b"{broken",)
        self.assertEqual([response.status_code for response in (global_sync, scheduled, backfill)], [202, 202, 202])
        self.assertEqual([call.args[1:] for call in enqueue.call_args_list], [
            ("sync", {"source": None, "sync_mode": "scheduled"}),
            ("sync", {"source": "-100111", "sync_mode": "scheduled"}),
            ("sync", {"source": "-100111", "sync_mode": "backfill"}),
        ])
        self.assertEqual([response.json()["job_id"] for response in (global_sync, scheduled, backfill)],
                         ["job-global", "job-scheduled", "job-backfill"])
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(empty_source.status_code, 400)
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(wake.call_count, 3)


    def test_v2_manual_sync_persists_and_deduplicates_real_queue_job(self):
        with self.admin_config():
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "_wake_worker") as wake:
                headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
                first = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                     headers=headers, json={})
                second = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                      headers=headers, json={})

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        first_job_id = first.json()["job_id"]
        self.assertEqual(second.json()["job_id"], first_job_id)
        job = get_job(TEST_URL, first_job_id)
        payload = job["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        self.assertEqual(job["action"], "sync")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(payload, {"source": None, "sync_mode": "scheduled"})
        self.assertEqual(self._job_count(), 1)
        self.assertEqual(wake.call_count, 2)
        self.assertEqual(wake.call_args.kwargs, {"source": None})


    def test_v2_per_source_sync_scopes_have_distinct_durable_jobs(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            config = Path(directory) / "config.yaml"
            config.write_text(
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Alpha'\n",
                encoding="utf-8",
            )
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "CONFIG_PATH", config), \
                 patch.object(admin_module, "_wake_worker") as wake:
                headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
                scheduled = self.request(
                    "POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                    headers=headers, json={"source": "-100111", "sync_mode": "scheduled"},
                )
                backfill = self.request(
                    "POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                    headers=headers, json={"source": "-100111", "sync_mode": "backfill"},
                )
                scheduled_replay = self.request(
                    "POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                    headers=headers, json={"source": "-100111", "sync_mode": "scheduled"},
                )

        self.assertEqual([response.status_code for response in (scheduled, backfill, scheduled_replay)],
                         [202, 202, 202])
        scheduled_id = scheduled.json()["job_id"]
        backfill_id = backfill.json()["job_id"]
        self.assertNotEqual(scheduled_id, backfill_id)
        self.assertEqual(scheduled_replay.json()["job_id"], scheduled_id)
        scheduled_job = get_job(TEST_URL, scheduled_id)
        backfill_job = get_job(TEST_URL, backfill_id)
        self.assertEqual(scheduled_job["payload"], {"source": "-100111", "sync_mode": "scheduled"})
        self.assertEqual(backfill_job["payload"], {"source": "-100111", "sync_mode": "backfill"})
        self.assertEqual(self._job_count(), 2)
        self.assertEqual(wake.call_count, 3)


    def test_v2_job_retry_and_cancel_use_durable_queue_lifecycle(self):
        from common.job_queue import claim_job, enqueue_job, finish_job, read_job
        sync = load_sync_module()

        failed_id = enqueue_job(TEST_URL, "collections_refresh", {})
        claimed = claim_job(TEST_URL, "collections_refresh")
        finish_job(TEST_URL, failed_id, "failed", {"error_code": "simulated_upstream_failure"},
                   lease_token=claimed["lease_token"])
        worker_result = {"collections": [{"name": "Adult Hub · Retry", "items": 1}],
                         "matched_videos": 1}
        with tempfile.TemporaryDirectory(prefix="adult_hub_v2_retry_flow_") as directory, self.admin_config():
            root = Path(directory)
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "_wake_worker") as wake:
                headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
                retried = self.request("POST", f"/admin/api/v2/jobs/{failed_id}/retry",
                                       base_url="http://localhost", headers=headers)
                self.assertEqual(retried.status_code, 200, retried.text)
                retry_id = retried.json()["job_id"]
                queued = read_job(TEST_URL, retry_id)
                queued_payload = queued["payload"]
                if isinstance(queued_payload, str):
                    queued_payload = json.loads(queued_payload)
                self.assertEqual((queued["action"], queued["state"], queued_payload),
                                 ("collections_refresh", "queued", {}))
                with patch.object(sync, "DATA_DIR", root / "data"), \
                     patch.dict("os.environ", {"JELLYFIN_URL": "http://jellyfin.invalid",
                                                 "JELLYFIN_API_KEY": "test-key"}), \
                     patch("common.managed_collections.refresh_managed_collections",
                           new=AsyncMock(return_value=worker_result)) as refresh:
                    with db_session(TEST_URL) as conn:
                        self.assertEqual(sync.process_video_duplicate_jobs(conn, root / "media"), 1)
                    refresh.assert_awaited_once()
                completed = read_job(TEST_URL, retry_id)
                completed_result = completed["result"]
                if isinstance(completed_result, str):
                    completed_result = json.loads(completed_result)
                self.assertEqual(completed["state"], "done")
                self.assertEqual(completed_result["matched_videos"], 1)

                cancel_id = enqueue_job(TEST_URL, "collections_refresh", {})
                completed_cancel = self.request("POST", f"/admin/api/v2/jobs/{retry_id}/cancel",
                                                base_url="http://localhost", headers=headers)
                cancelled = self.request("POST", f"/admin/api/v2/jobs/{cancel_id}/cancel",
                                         base_url="http://localhost", headers=headers)

        self.assertEqual(completed_cancel.status_code, 409)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["state"], "cancelled")
        self.assertEqual(read_job(TEST_URL, failed_id)["state"], "failed")
        self.assertEqual(read_job(TEST_URL, retry_id)["state"], "done")
        self.assertEqual(read_job(TEST_URL, cancel_id)["state"], "cancelled")
        self.assertEqual(wake.call_args_list[0].args, ("jobs",))


    def test_v2_collections_refresh_persists_real_queue_job(self):
        sync = load_sync_module()

        result = {"collections": [{"name": "Adult Hub · Test", "items": 2}], "matched_videos": 2}
        with tempfile.TemporaryDirectory(prefix="adult_hub_collections_flow_") as directory, self.admin_config(), \
             patch.dict("os.environ", {
                 "JELLYFIN_URL": "http://jellyfin.invalid", "JELLYFIN_API_KEY": "test-key"
             }):
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "_wake_worker") as wake:
                response = self.request("POST", "/admin/api/v2/maintenance/collections/refresh",
                                        base_url="http://localhost",
                                        headers={"Origin": "http://localhost",
                                                 "Authorization": "Bearer test-write-token"}, json={})

            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["job_id"]
            queued = get_job(TEST_URL, job_id)
            self.assertEqual((queued["action"], queued["state"], queued["payload"]),
                             ("collections_refresh", "queued", {}))
            self.assertEqual(self._job_count(), 1)
            wake.assert_called_once_with("jobs")

            with patch.object(sync, "DATA_DIR", root / "data"), \
                 patch.dict("os.environ", {"JELLYFIN_URL": "http://jellyfin.invalid",
                                             "JELLYFIN_API_KEY": "test-key"}), \
                 patch("common.managed_collections.refresh_managed_collections",
                       new=AsyncMock(return_value=result)) as refresh:
                with db_session(TEST_URL) as conn:
                    completed = sync.process_video_duplicate_jobs(conn, media)

            self.assertEqual(completed, 1)
            refresh.assert_awaited_once()
            self.assertEqual(refresh.await_args.args[1:],
                             ("http://jellyfin.invalid", "test-key", TEST_URL))
            finished = get_job(TEST_URL, job_id)
            self.assertEqual(finished["state"], "done")
            finished_result = finished["result"]
            if isinstance(finished_result, str):
                finished_result = json.loads(finished_result)
            self.assertEqual(finished_result["matched_videos"], 2)
            self.assertEqual(finished_result["collections"], result["collections"])


    def test_v2_jellyfin_refresh_is_authorized_and_does_not_expose_credentials(self):
        from unittest.mock import AsyncMock
        response_from_jellyfin = Mock(status_code=204)
        post = AsyncMock(return_value=response_from_jellyfin)
        with self.admin_config(), patch.dict("os.environ", {
            "JELLYFIN_URL": "http://jellyfin.local/", "JELLYFIN_API_KEY": "secret-jellyfin-key"
        }), patch("httpx.AsyncClient.post", new=post):
            denied = self.request("POST", "/admin/api/v2/maintenance/jellyfin/refresh", base_url="http://localhost",
                                  headers={"Origin": "http://localhost"}, json={})
            allowed = self.request("POST", "/admin/api/v2/maintenance/jellyfin/refresh", base_url="http://localhost",
                                   headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertTrue(allowed.json()["requested"])
        self.assertNotIn("secret-jellyfin-key", allowed.text)
        post.assert_awaited_once()
        self.assertEqual(post.await_args.args[0], "http://jellyfin.local/Library/Refresh")


    def test_v2_jellyfin_refresh_rejects_missing_configuration(self):
        with self.admin_config(), patch.dict("os.environ", {"JELLYFIN_URL": "", "JELLYFIN_API_KEY": ""}):
            response = self.request("POST", "/admin/api/v2/maintenance/jellyfin/refresh", base_url="http://localhost",
                                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("JELLYFIN_API_KEY", response.text)


    def test_v2_collections_refresh_is_durable_and_authorized(self):
        with self.admin_config(), patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
             patch("resolver.video_duplicates.enqueue_job", return_value="job-collections") as enqueue, \
             patch.object(admin_module, "_wake_worker") as wake:
            denied = self.request("POST", "/admin/api/v2/maintenance/collections/refresh", base_url="http://localhost",
                                  headers={"Origin": "http://localhost"}, json={})
            with patch.dict("os.environ", {"JELLYFIN_URL": "", "JELLYFIN_API_KEY": ""}):
                unavailable = self.request(
                    "POST", "/admin/api/v2/maintenance/collections/refresh", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={}
                )
            with patch.dict("os.environ", {
                "JELLYFIN_URL": "http://jellyfin.invalid", "JELLYFIN_API_KEY": "test-key"
            }):
                queued = self.request(
                    "POST", "/admin/api/v2/maintenance/collections/refresh", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={}
                )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(unavailable.status_code, 503, unavailable.text)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(queued.json()["job_id"], "job-collections")
        enqueue.assert_called_once_with(TEST_URL, "collections_refresh", {})
        wake.assert_called_once_with("jobs")



if __name__ == "__main__":
    unittest.main()
