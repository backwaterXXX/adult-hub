"""Focused contracts for Admin v2 issue-center rules, stalled jobs, and progress projections."""

import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests.pg_helper import PgTestCase
from common import db

import resolver.admin_v2_issues as admin_v2_issues
import resolver.admin_v2_jobs as admin_v2_jobs


class TestIssueCenterRules(unittest.TestCase):
    def make_admin(self, history, sources):
        data = SimpleNamespace(
            configured_sources=lambda _path: {"available": True, "sources": sources},
            unavailable_videos=lambda *_a, **_k: {"available": True, "total": 0},
        )
        return SimpleNamespace(
            admin_data=data, CONFIG_PATH="/unused/config.yaml", HUB_DB_URL="/unused/db",
            _sync_history=lambda _limit: history,
            _quality_summary=lambda _db: {"available": True, "status": "no_complete_audit"},
            _backup_status=lambda: {"inventory_available": True, "available": True,
                                    "latest": {"name": "recent.dump.gz", "modified_at": 90}},
        )

    def test_reports_never_run_enabled_source_but_skips_disabled_sources(self):
        sources = [{"provider": "telegram", "enabled": True, "channels": [
            {"id": "1", "studio": "First", "enabled": True},
            {"id": "2", "studio": "Paused", "enabled": False},
        ]}]
        admin = self.make_admin({"runs": []}, sources)
        with patch("resolver.admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("resolver.admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), \
             patch("resolver.admin_services.job_listing", return_value={"available": True, "jobs": [], "truncated": False}):
            issues, health = admin_v2_issues._build_issues(admin, 100)
        self.assertEqual([i["object_id"] for i in issues if i["kind"] == "sync_never_run"], ["telegram:1"])
        self.assertFalse(health["incomplete"])

    def test_latest_success_resolves_prior_failure_and_truncation_is_visible(self):
        sources = [{"provider": "telegram", "enabled": True, "channels": [{"id": "1", "enabled": True}]}]
        history = {"runs": [
            {"ended_at": 20, "sources": [{"source": "telegram:1", "success": True}]},
            {"ended_at": 10, "sources": [{"source": "telegram:1", "success": False, "error": "old"}]},
        ], "truncated": True}
        admin = self.make_admin(history, sources)
        with patch("resolver.admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("resolver.admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), \
             patch("resolver.admin_services.job_listing", return_value={"available": True, "jobs": [], "truncated": False}):
            issues, health = admin_v2_issues._build_issues(admin, 100)
        self.assertFalse(any(i["kind"] == "sync_failure" for i in issues))
        self.assertFalse(any(i["kind"] == "sync_never_run" for i in issues))
        self.assertTrue(health["truncated"]["sync_history"])
        self.assertTrue(health["incomplete"])
        self.assertEqual(health["freshness"]["sync_history_at"], "1970-01-01T00:00:20Z")

    def test_unavailable_issue_links_to_matching_library_filter(self):
        admin = self.make_admin({"runs": []}, [])
        admin.admin_data.unavailable_videos = lambda *_a, **_k: {"available": True, "total": 3}
        with patch("resolver.admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("resolver.admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), \
             patch("resolver.admin_services.job_listing", return_value={"available": True, "jobs": [], "truncated": False}):
            issues, _health = admin_v2_issues._build_issues(admin, 100)
        issue = next(i for i in issues if i["kind"] == "unavailable_video")
        self.assertEqual(issue["action"]["route"], "/admin/v2/#library?availability=unavailable")
        self.assertEqual(issue["last_seen"], "1970-01-01T00:01:40Z")

    def test_failed_job_stays_open_until_a_matching_retry_succeeds(self):
        admin = self.make_admin({"runs": []}, [])
        failed = {"id": "failed-1", "action": "sync", "updated_at": 10,
                  "payload": {"channel_id": "-1007", "fingerprints": {"request": "old"}}}
        def run(unresolved_jobs):
            feed = {"available": True, "jobs": unresolved_jobs,
                    "total": len(unresolved_jobs), "truncated": False}
            with patch("resolver.admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
                 patch("resolver.admin_services.unresolved_failed_task_listing", return_value=feed), \
                 patch("resolver.admin_services.job_listing", return_value={
                     "available": True, "jobs": [], "truncated": False,
                 }):
                return admin_v2_issues._build_issues(admin, 100)[0]

        before_retry = run([{**failed, "attempts": 1}])
        self.assertEqual([issue["object_id"] for issue in before_retry if issue["kind"] == "job_failure"], ["failed-1"])
        after_retry = run([])
        self.assertFalse(any(issue["kind"] == "job_failure" for issue in after_retry))

    def test_repeated_failures_for_the_same_task_are_grouped(self):
        admin = self.make_admin({"runs": []}, [])
        failed = [
            {"id": "failed-1", "action": "sync", "updated_at": 10,
             "payload": {"channel_id": "-1007"}},
            {"id": "failed-2", "action": "sync", "updated_at": 20,
             "payload": {"channel_id": "-1007"}},
            {"id": "failed-3", "action": "sync", "updated_at": 30,
             "payload": {"channel_id": "-1008"}},
        ]

        feed = {"available": True, "jobs": [
            {**failed[1], "attempts": 2}, {**failed[2], "attempts": 1},
        ], "total": 2, "truncated": False}
        with patch("resolver.admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("resolver.admin_services.unresolved_failed_task_listing", return_value=feed), \
             patch("resolver.admin_services.job_listing", return_value={
                 "available": True, "jobs": [], "truncated": False,
             }):
            issues, _health = admin_v2_issues._build_issues(admin, 100)

        failures = [issue for issue in issues if issue["kind"] == "job_failure"]
        self.assertEqual(len(failures), 2)
        repeated = next(issue for issue in failures if issue.get("count") == 2)
        self.assertEqual(repeated["object_id"], "failed-2")



class TestJobProgressProjection(unittest.TestCase):
    def test_job_detail_reports_real_checkpoint_without_exposing_result_payload(self):
        safe = admin_v2_jobs._safe_job({
            "id": "job-1", "action": "hide", "state": "processing", "updated_at": 123,
            "progress": {"stage": "moving_files", "updated_at": 123},
            "result": {
                "local_state": "hidden",
                "source_url": "https://secret.invalid/key", "path": "/srv/private/movie.strm",
            },
        })
        self.assertEqual(safe["stage"], "moving_files")
        self.assertEqual(safe["local_state"], "hidden")
        self.assertNotIn("source_url", safe)
        self.assertNotIn("path", safe)
        self.assertNotIn("result", safe)

    def test_legacy_processing_job_has_no_invented_progress(self):
        safe = admin_v2_jobs._safe_job({"id": "old", "state": "processing", "result": None})
        self.assertEqual(safe["stage"], "processing")
        self.assertIsNone(safe["local_state"])

    def test_job_projection_exposes_safe_local_file_repair_state(self):
        safe = admin_v2_jobs._safe_job({
            "id": "audit-repair", "action": "audit_repair_nfo", "state": "done",
            "result": {"local_state": "files_repaired", "restored_streams": 1,
                       "path": "/private/library/video.strm"},
        })
        self.assertEqual(safe["local_state"], "files_repaired")
        self.assertNotIn("restored_streams", safe)
        self.assertNotIn("path", safe)

    def test_job_projection_summarizes_external_metadata_scan_without_provider_details(self):
        safe = admin_v2_jobs._safe_job({
            "id": "metadata-scan", "action": "metadata_backfill", "state": "done",
            "result": {"local_state": "metadata_scan_completed", "entities_checked": 500,
                       "entities_enriched": 23, "providers_completed": 5, "providers_failed": 0,
                       "private_provider_error": "secret token=hidden"},
        })
        self.assertEqual(safe["local_state"], "metadata_scan_completed")
        self.assertEqual(
            safe["result_summary"],
            "External metadata scan checked 500 entities, enriched 23 entities, 5 provider steps completed. Open System to review cumulative progress.",
        )
        self.assertNotIn("private_provider_error", str(safe))
        self.assertNotIn("secret", str(safe))
        partial = admin_v2_jobs._safe_job({
            "id": "metadata-scan-partial", "action": "metadata_backfill", "state": "done",
            "result": {"local_state": "metadata_scan_completed", "entities_checked": 45,
                       "entities_enriched": 20, "providers_completed": 4, "providers_failed": 1},
        })
        self.assertEqual(
            partial["result_summary"],
            "External metadata scan checked 45 entities, enriched 20 entities, 4 provider steps completed, 1 provider step reported an error. Open System to review cumulative progress.",
        )

    def test_metadata_scan_summary_ignores_invalid_or_unbounded_counters(self):
        safe = admin_v2_jobs._safe_job({
            "id": "metadata-scan-invalid-counts", "action": "metadata_backfill", "state": "done",
            "result": {"local_state": "metadata_scan_completed", "entities_checked": True,
                       "entities_enriched": 1_000_000_001, "providers_completed": 6,
                       "providers_failed": 99},
        })
        self.assertEqual(
            safe["result_summary"],
            "External metadata scan completed. Open System to review cumulative progress.",
        )

    def test_cancelled_job_has_safe_terminal_summary(self):
        safe = admin_v2_jobs._safe_job({"id": "cancelled-1", "state": "cancelled", "result": {}})
        self.assertEqual(safe["stage"], "cancelled")
        self.assertEqual(safe["result_summary"], "Cancelled before the worker started.")
        self.assertNotIn("result", safe)

    def test_progress_and_event_history_are_whitelisted(self):
        from resolver.admin_services import _safe_job_progress

        progress, events = _safe_job_progress({
            "progress": {"stage": "moving_files", "completed": 2, "total": 5, "path": "/private/file"},
            "events": [
                {"stage": "moving_files", "updated_at": 100, "completed": 2, "total": 5, "url": "https://private.invalid"},
                {"stage": "../../etc/passwd", "updated_at": 101},
            ],
        })
        self.assertEqual(progress, {"stage": "moving_files", "completed": 2, "total": 5})
        self.assertEqual(events, [{"stage": "moving_files", "updated_at": 100, "completed": 2, "total": 5}])

    def test_job_api_exposes_only_safe_stage_and_known_counters(self):
        safe = admin_v2_jobs._safe_job({
            "id": "job", "state": "processing",
            "progress": {"stage": "moving_files", "updated_at": 100, "completed": 2, "total": 5,
                         "message": "https://private.invalid/token"},
            "events": [{"stage": "moving_files", "updated_at": 100, "completed": 2, "total": 5,
                        "path": "/private/file"}],
        })
        self.assertEqual(safe["stage"], "moving_files")
        self.assertEqual(safe["progress"], {"stage": "moving_files", "updated_at": 100, "completed": 2, "total": 5})
        self.assertEqual(safe["events"], [{"stage": "moving_files", "updated_at": 100, "completed": 2, "total": 5}])

class TestStalledJobIssuePostgresIntegration(PgTestCase):
    def test_real_processing_job_generates_a_redacted_stalled_issue(self):
        from common.job_queue import enqueue_job

        job_id = enqueue_job(self.db_url, "consistency_audit", {"private_marker": "secret-token"})
        now = int(time.time())
        conn = db.connect(self.db_url)
        try:
            with conn:
                conn.execute(
                    "UPDATE video_duplicate_jobs SET state='processing',updated_at=%s WHERE id=%s",
                    (now - 120, job_id),
                )
        finally:
            conn.close()

        stub = SimpleNamespace(
            admin_data=SimpleNamespace(
                configured_sources=lambda _path: {"available": True, "sources": []},
                unavailable_videos=lambda *_args, **_kwargs: {"available": True, "total": 0},
            ),
            CONFIG_PATH="/unused/config.yaml",
            HUB_DB_URL=self.db_url,
            _sync_history=lambda _limit: {"runs": [], "truncated": False},
            _backup_status=lambda: {
                "inventory_available": True,
                "available": True,
                "latest": {"name": "recent.dump.gz", "modified_at": now - 60},
            },
        )
        issues, health = admin_v2_issues._build_issues(stub, now)

        stalled = [issue for issue in issues if issue["kind"] == "job_stalled"]
        self.assertEqual(len(stalled), 1)
        self.assertEqual(stalled[0]["object_id"], job_id)
        self.assertEqual(stalled[0]["action"]["route"], f"/admin/v2/?job_id={job_id}#jobs")
        self.assertNotIn("secret-token", json.dumps(stalled))
        self.assertNotIn("private_marker", json.dumps(stalled))
        self.assertFalse(health["incomplete"])



if __name__ == "__main__":
    unittest.main()
