"""Behavior tests for the Admin V2 issue center's operator-facing signals."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.responses import JSONResponse
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resolver"))

import admin_v2_issue_builder
import admin_v2_issues
import admin_services


NOW = 2_000_000_000


class InlineAsyncio:
    @staticmethod
    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)


def request(path):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    return Request({"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
                    "path": path, "raw_path": path.encode(), "query_string": b"", "headers": [],
                    "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": ""}, receive=receive)


def make_admin(*, configured=None, history=None, library=None, backup=None, quality=None):
    return SimpleNamespace(
        admin_data=SimpleNamespace(
            configured_sources=lambda _path: configured if configured is not None else {
                "available": True,
                "sources": [{"provider": "telegram", "enabled": True, "channels": [
                    {"id": "1", "studio": "Studio One", "enabled": True},
                    {"id": "2", "studio": "Studio Two", "enabled": True},
                    {"id": "3", "studio": "Never Run", "enabled": True},
                    {"id": "4", "studio": "Disabled", "enabled": False},
                ]}],
            },
            unavailable_videos=lambda *_args, **_kwargs: library if library is not None else {
                "available": True, "total": 4,
            },
        ),
        CONFIG_PATH="/private/config.yaml",
        HUB_DB_URL="postgresql://test.invalid/db",
        _sync_history=lambda _limit: history if history is not None else {
            "runs": [
                {"ended_at": NOW - 10, "sources": [
                    {"source": "telegram:1", "success": False, "error": "private token"},
                    {"source": "telegram:2", "success": True},
                ]},
                {"ended_at": NOW - 500, "sources": [
                    {"source": "telegram:1", "success": True},
                    {"source": "telegram:2", "success": False, "error": "stale"},
                ]},
            ],
        },
        _backup_status=lambda: backup if backup is not None else {
            "inventory_available": True,
            "available": True,
            "latest": {"name": "adulthub-latest.dump.gz", "modified_at": NOW - 60},
        },
        _quality_summary=lambda _url: quality if quality is not None else {
            "available": True, "status": "ready", "counts": {
                "missing_metadata": 0, "missing_nfo": 0, "invalid_nfo": 0,
                "nfo_metadata_mismatch": 0, "missing_artwork": 0,
            }, "latest_audit": {"created_at": NOW - 30},
        },
        _admin_blocked=lambda _request: False,
        JSONResponse=JSONResponse,
        asyncio=InlineAsyncio,
    )


class TestAdminV2IssueSemantics(unittest.TestCase):
    def test_dashboard_quality_type_filters_select_one_finding_category(self):
        endpoint = {route.path: route.endpoint for route in admin_v2_issues.build_router(make_admin()).routes}["/api/v2/issues"]
        issue_rows = [
            {"kind": "quality_finding", "object_id": "missing_metadata", "severity": "medium", "status": "open", "last_seen": NOW},
            {"kind": "quality_finding", "object_id": "missing_nfo", "severity": "medium", "status": "open", "last_seen": NOW},
            {"kind": "quality_finding", "object_id": "invalid_nfo", "severity": "medium", "status": "open", "last_seen": NOW},
            {"kind": "quality_finding", "object_id": "nfo_metadata_mismatch", "severity": "medium", "status": "open", "last_seen": NOW},
            {"kind": "quality_finding", "object_id": "missing_artwork", "severity": "medium", "status": "open", "last_seen": NOW},
        ]
        with patch("admin_v2_issues._build_issues", return_value=(issue_rows, {})):
            result = self._run(endpoint(
                request("/admin/api/v2/issues?kind=missing_nfo"), severity="", kind="missing_nfo",
                status="open", offset=0, limit=50,
            ))
        self.assertEqual([item["object_id"] for item in result["issues"]], ["missing_nfo"])

    @staticmethod
    def _run(awaitable):
        import asyncio
        return asyncio.run(awaitable)

    def test_quality_findings_are_summarized_and_deep_link_to_the_category(self):
        admin = make_admin(quality={
            "available": True, "status": "ready", "counts": {
                "missing_metadata": 1, "missing_nfo": 2, "invalid_nfo": 0,
                "nfo_metadata_mismatch": 7, "missing_artwork": 0,
            }, "latest_audit": {"created_at": NOW - 120},
        })
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, health = admin_v2_issues._build_issues(admin, NOW)

        quality = [issue for issue in issues if issue["kind"] == "quality_finding"]
        mismatch = next(issue for issue in quality if issue["object_id"] == "nfo_metadata_mismatch")
        self.assertEqual(len(quality), 3)
        self.assertEqual(mismatch["count"], 7)
        self.assertIn("7 open item(s)", mismatch["message"])
        self.assertEqual(mismatch["action"]["route"], "/admin/v2/#system")
        self.assertEqual(mismatch["list_route"], "/admin/v2/#system")
        self.assertEqual(mismatch["first_seen"], admin_v2_issues._iso_time(NOW - 120))
        self.assertTrue(health["available"]["quality_audit"])
        self.assertFalse(health["incomplete"])

    def test_missing_complete_quality_audit_is_actionable_without_claiming_findings(self):
        admin = make_admin(quality={"available": True, "status": "no_complete_audit", "counts": {}})
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, health = admin_v2_issues._build_issues(admin, NOW)

        audit = next(issue for issue in issues if issue["kind"] == "quality_audit")
        self.assertEqual(audit["severity"], "low")
        self.assertEqual(audit["action"]["route"], "/admin/v2/#system")
        self.assertNotIn("count", audit)
        self.assertTrue(health["available"]["quality_audit"])

    def test_missing_stale_fresh_and_unavailable_backup_states_are_distinct(self):
        with patch.dict("os.environ", {
            "ADMIN_V2_BACKUP_WARNING_SECONDS": "3600",
            "ADMIN_V2_MONITOR_BACKUPS": "true",
        }):
            missing, missing_health = admin_v2_issues._build_issues(make_admin(backup={
                "inventory_available": True, "available": False, "latest": None,
            }), NOW)
            stale, stale_health = admin_v2_issues._build_issues(make_admin(backup={
                "inventory_available": True, "available": True,
                "latest": {"name": "old.dump.gz", "modified_at": NOW - 7200},
            }), NOW)
            fresh, fresh_health = admin_v2_issues._build_issues(make_admin(backup={
                "inventory_available": True, "available": True,
                "latest": {"name": "recent.dump.gz", "modified_at": NOW - 1200},
            }), NOW)
            unavailable, unavailable_health = admin_v2_issues._build_issues(make_admin(backup={
                "inventory_available": False, "available": False, "latest": None,
            }), NOW)

        missing_issue = next(issue for issue in missing if issue["kind"] == "backup")
        self.assertEqual(missing_issue["title"], "No database backup is available")
        self.assertEqual(missing_issue["severity"], "high")
        self.assertEqual(missing_issue["action"]["route"], "/admin/v2/#system?tab=backups")
        self.assertTrue(missing_health["available"]["backup_inventory"])

        stale_issue = next(issue for issue in stale if issue["kind"] == "backup")
        self.assertEqual(stale_issue["title"], "Database backup is overdue")
        self.assertIn("2 hours old", stale_issue["message"])
        self.assertEqual(stale_issue["action"]["route"], "/admin/v2/#system?tab=backups")
        self.assertEqual(stale_health["freshness"]["backup_at"], admin_v2_issues._iso_time(NOW - 7200))

        self.assertFalse(any(issue["kind"] == "backup" for issue in fresh))
        self.assertTrue(fresh_health["available"]["backup_inventory"])

        unavailable_issue = next(issue for issue in unavailable if issue["kind"] == "backup")
        self.assertEqual(unavailable_issue["title"], "Backup inventory unavailable")
        self.assertEqual(unavailable_issue["action"]["route"], "/admin/v2/#system?tab=backups")
        self.assertFalse(unavailable_health["available"]["backup_inventory"])
        self.assertTrue(unavailable_health["incomplete"])

    def test_system_managed_backups_do_not_generate_inventory_alerts(self):
        with patch.dict("os.environ", {"ADMIN_V2_MONITOR_BACKUPS": "false"}):
            issues, health = admin_v2_issues._build_issues(make_admin(backup={
                "inventory_available": False, "available": False, "latest": None,
            }), NOW)
        self.assertFalse(any(issue["kind"] == "backup" for issue in issues))
        self.assertFalse(health["available"]["backup_inventory"])

    def test_latest_source_failure_and_never_run_are_distinct_and_disabled_sources_are_ignored(self):
        admin = make_admin()
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, health = admin_v2_issues._build_issues(admin, NOW)

        source_issues = [item for item in issues if item["object_type"] == "source"]
        self.assertEqual([(item["kind"], item["object_id"]) for item in source_issues], [
            ("sync_failure", "telegram:1"), ("sync_never_run", "telegram:3"),
        ])
        self.assertIn("Studio One", source_issues[0]["title"])
        self.assertEqual(source_issues[0]["action"]["route"], "/admin/v2/?source_id=telegram%3A1#sources")
        self.assertEqual(source_issues[1]["action"]["route"], "/admin/v2/?source_id=telegram%3A3#sources")
        self.assertNotIn("private token", str(issues))
        self.assertTrue(health["available"]["sync_history"])
        self.assertFalse(health["incomplete"])

    def test_removed_and_disabled_telegram_sources_do_not_leave_open_failure_issues(self):
        admin = make_admin(history={"runs": [{"ended_at": NOW - 10, "sources": [
            {"source": "telegram:removed", "success": False, "error": "old failure"},
            {"source": "telegram:4", "success": False, "error": "disabled failure"},
        ]}]})
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, _health = admin_v2_issues._build_issues(admin, NOW)

        self.assertFalse(any(issue["kind"] == "sync_failure" for issue in issues))

    def test_removed_or_disabled_non_telegram_sources_do_not_leave_open_failures(self):
        admin = make_admin(
            configured={"available": True, "sources": [
                {"provider": "onlygays", "enabled": False, "address": "https://example.test"},
                {"provider": "knownsite", "enabled": True, "address": "https://example.test"},
            ]},
            history={"runs": [{"ended_at": NOW - 10, "sources": [
                {"source": "onlygays", "success": False, "error": "disabled failure"},
                {"source": "removedsite", "success": False, "error": "removed failure"},
                {"source": "telegram:removed", "success": False, "error": "removed channel"},
                {"source": "knownsite", "success": False, "error": "current failure"},
            ]}]},
        )
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, _health = admin_v2_issues._build_issues(admin, NOW)

        source_failures = [issue for issue in issues if issue["kind"] == "sync_failure"]
        self.assertEqual([issue["object_id"] for issue in source_failures], ["knownsite"])

    def test_failed_source_remains_visible_when_source_config_is_unavailable(self):
        admin = make_admin(
            configured={"available": False, "sources": []},
            history={"runs": [{"ended_at": NOW - 10, "sources": [
                {"source": "telegram:unknown", "success": False, "error": "old failure"},
            ]}]},
        )
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, _health = admin_v2_issues._build_issues(admin, NOW)

        self.assertTrue(any(
            issue["kind"] == "sync_failure" and issue["object_id"] == "telegram:unknown"
            for issue in issues
        ))

    def test_unavailable_sync_history_has_explicit_issue_and_stable_health_shape(self):
        admin = make_admin(history={"error": "private backend detail"})
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, health = admin_v2_issues._build_issues(admin, NOW)

        history_issue = next(issue for issue in issues if issue.get("object_id") == "sync-history")
        self.assertEqual(history_issue["kind"], "system")
        self.assertEqual(history_issue["action"]["route"], "/admin/v2/#sources")
        self.assertNotIn("private backend detail", str(issues))
        self.assertFalse(health["truncated"]["sync_history"])
        self.assertTrue(health["incomplete"])

    def test_full_history_failure_listing_returns_bounded_safe_rows(self):
        class Result:
            def fetchall(self):
                return [("job-1", "sync", NOW - 200, NOW - 100, 3, 1001)]

        class Connection:
            def __init__(self):
                self.sql = ""
                self.params = None
                self.closed = False

            def execute(self, sql, params):
                self.sql, self.params = sql, params
                return Result()

            def close(self):
                self.closed = True

        conn = Connection()
        with patch.object(admin_services, "_jobs_connection", return_value=conn):
            result = admin_services.unresolved_failed_task_listing("configured", limit=5000)

        self.assertEqual(conn.params, (1000,))
        self.assertIn("row_number() OVER", conn.sql)
        self.assertIn("completed.updated_at > groups.failed_at", conn.sql)
        self.assertIn("payload::jsonb - ARRAY['fingerprints', '_resume']::text[]", conn.sql)
        self.assertTrue(conn.closed)
        self.assertEqual(result, {
            "available": True,
            "jobs": [{"id": "job-1", "action": "sync", "created_at": NOW - 200,
                      "updated_at": NOW - 100, "attempts": 3}],
            "total": 1001,
            "truncated": True,
        })

    def test_truncated_history_does_not_create_false_never_run_issue(self):
        admin = make_admin(history={"runs": [], "truncated": True})
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, health = admin_v2_issues._build_issues(admin, NOW)

        self.assertFalse(any(item["kind"] == "sync_never_run" for item in issues))
        self.assertTrue(health["truncated"]["sync_history"])
        self.assertTrue(health["incomplete"])

    def test_old_queue_is_high_severity_and_reports_age_and_count(self):
        admin = make_admin()
        with patch("admin_services.sync_backlog", return_value={
            "available": True, "queued": 3, "oldest_queued_age_seconds": 7200,
        }), patch("admin_services.unresolved_failed_task_listing", return_value={
            "available": True, "jobs": [], "total": 0, "truncated": False,
        }), patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, _ = admin_v2_issues._build_issues(admin, NOW)

        issue = next(item for item in issues if item["kind"] == "sync_backlog")
        self.assertEqual(issue["severity"], "high")
        self.assertIn("3 synchronization tasks", issue["message"])
        self.assertIn("120 minutes", issue["message"])
        self.assertEqual(issue["first_seen"], admin_v2_issues._iso_time(NOW - 7200))
        self.assertEqual(issue["action"]["route"], "/admin/v2/?job_state=queued&job_action=sync#jobs")

    def test_failed_retry_groups_suppressing_newer_success_are_listed(self):
        admin = make_admin()
        only_open = {"id": "only-b", "action": "backup_create", "created_at": NOW - 100,
                     "updated_at": NOW - 100, "attempts": 1}
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [only_open], "total": 1, "truncated": False,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, _ = admin_v2_issues._build_issues(admin, NOW)

        failures = [item for item in issues if item["kind"] == "job_failure"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["object_id"], "only-b")
        self.assertEqual(failures[0]["message"], "The “backup_create” operation failed. Review the task and retry it if appropriate.")
        self.assertEqual(failures[0]["action"]["route"], "/admin/v2/?job_id=only-b#jobs")

    def test_full_history_listing_keeps_feed_complete_past_done_page_cap(self):
        admin = make_admin()
        failure = {"id": "latest-failure", "action": "sync", "created_at": NOW - 200,
                   "updated_at": NOW - 100, "attempts": 2}
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [failure], "total": 1, "truncated": False,
             }) as reconcile:
            with patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
                issues, health = admin_v2_issues._build_issues(admin, NOW)

        reconcile.assert_called_once_with(admin.HUB_DB_URL, limit=1000)
        issue = next(item for item in issues if item["kind"] == "job_failure")
        self.assertEqual(issue["object_id"], "latest-failure")
        self.assertEqual(issue["count"], 2)
        self.assertIn("“sync” operation failed on 2 matching attempts", issue["message"])
        self.assertFalse(health["truncated"]["done_jobs"])
        self.assertFalse(health["incomplete"])

    def test_grouped_failure_message_uses_only_known_action_names(self):
        admin = make_admin()
        row = {"id": "failure-1", "action": "private-token-injected-action",
               "created_at": NOW - 100, "updated_at": NOW - 50, "attempts": 2}
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [row], "total": 1, "truncated": False,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, _ = admin_v2_issues._build_issues(admin, NOW)

        issue = next(item for item in issues if item["kind"] == "job_failure")
        self.assertIn("The “task” operation failed on 2 matching attempts", issue["message"])
        self.assertNotIn("private-token", issue["message"])

    def test_more_than_one_thousand_failure_groups_are_explicitly_truncated(self):
        admin = make_admin()
        row = {"id": "failure-1", "action": "sync", "created_at": NOW - 100,
               "updated_at": NOW - 100, "attempts": 1}
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [row], "total": 1001, "truncated": True,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            _, health = admin_v2_issues._build_issues(admin, NOW)
        self.assertTrue(health["truncated"]["failed_jobs"])
        self.assertTrue(health["incomplete"])

    def test_unavailable_full_failure_history_is_never_reported_as_empty(self):
        admin = make_admin()
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": False, "jobs": [], "total": None, "truncated": False,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, health = admin_v2_issues._build_issues(admin, NOW)

        self.assertFalse(any(issue["kind"] == "job_failure" for issue in issues))
        self.assertTrue(any(issue["kind"] == "system" for issue in issues))
        self.assertFalse(health["available"]["database"])
        self.assertTrue(health["truncated"]["failed_jobs"])
        self.assertTrue(health["incomplete"])

    def test_identical_failed_attempts_count_once_and_database_failure_is_explicit(self):
        admin = make_admin(library={"available": False, "total": 0})
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True,
                 "jobs": [{"id": "retry-2", "action": "sync", "created_at": NOW - 100,
                           "updated_at": NOW - 50, "attempts": 2}],
                 "total": 1, "truncated": False,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": [], "truncated": False}):
            issues, health = admin_v2_issues._build_issues(admin, NOW)

        job_issue = next(item for item in issues if item["kind"] == "job_failure")
        self.assertEqual(job_issue["object_id"], "retry-2")
        self.assertEqual(job_issue["count"], 2)
        system_issue = next(item for item in issues if item["kind"] == "system")
        self.assertEqual(system_issue["object_id"], "admin-data")
        self.assertFalse(health["available"]["database"])
        self.assertTrue(health["incomplete"])

    def test_stale_processing_task_is_actionable_but_fresh_or_unknown_heartbeat_is_ignored(self):
        admin = make_admin()
        processing = [
            {"id": "stalled-1", "action": "consistency_audit", "state": "processing",
             "updated_at": NOW - 95},
            {"id": "active-2", "action": "backup_create", "state": "processing",
             "updated_at": NOW - 20},
            {"id": "legacy-3", "action": "sync", "state": "processing"},
        ]
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": processing, "truncated": False}):
            issues, health = admin_v2_issues._build_issues(admin, NOW)

        stalled = [item for item in issues if item["kind"] == "job_stalled"]
        self.assertEqual(len(stalled), 1)
        self.assertEqual(stalled[0]["severity"], "high")
        self.assertEqual(stalled[0]["object_id"], "stalled-1")
        self.assertIn("more than 90 seconds", stalled[0]["message"])
        self.assertEqual(stalled[0]["first_seen"], admin_v2_issues._iso_time(NOW - 5))
        self.assertEqual(stalled[0]["last_seen"], admin_v2_issues._iso_time(NOW))
        self.assertEqual(stalled[0]["action"]["route"], "/admin/v2/?job_id=stalled-1#jobs")
        self.assertEqual(health["thresholds"]["processing_stale_warning_seconds"], 90)
        self.assertFalse(health["incomplete"])

    def test_truncated_processing_history_marks_issue_feed_incomplete(self):
        with patch("admin_services.sync_backlog", return_value={"available": True, "queued": 0}), \
             patch("admin_services.unresolved_failed_task_listing", return_value={
                 "available": True, "jobs": [], "total": 0, "truncated": False,
             }), \
             patch("admin_services.job_listing", return_value={"jobs": [], "truncated": True}):
            _, health = admin_v2_issues._build_issues(make_admin(), NOW)
        self.assertTrue(health["truncated"]["processing_jobs"])
        self.assertTrue(health["incomplete"])


class TestAdminV2ProblemGroups(unittest.TestCase):
    """Overview groups the complete open set by problem kind and operation."""

    @staticmethod
    def summarize(issues, health=None):
        return admin_v2_issues._issue_summary(issues, {"incomplete": False} if health is None else health)

    def test_same_kind_and_operation_merge_and_sum_recorded_occurrences(self):
        summary = self.summarize([
            {"kind": "job_failure", "object_id": "sync", "severity": "medium", "status": "open",
             "last_seen": NOW - 100, "count": 8, "title": "Synchronization failed"},
            {"kind": "job_failure", "object_id": "sync", "severity": "high", "status": "open",
             "last_seen": NOW, "count": 4, "title": "Synchronization failed", "operation_label": "sync",
             "action": {"label": "Review failures", "route": "/admin/v2/?job_state=failed&job_action=sync#jobs"}},
            {"kind": "job_failure", "object_id": "linkcheck", "severity": "high", "status": "open",
             "last_seen": NOW - 50, "title": "Link check failed"},
            {"kind": "sync_backlog", "severity": "low", "status": "open", "last_seen": NOW - 10, "title": "Sync queue is old"},
            {"kind": "sync_backlog", "severity": "low", "status": "open", "last_seen": NOW - 20, "title": "Sync queue is old"},
            {"kind": "backup", "severity": "high", "status": "resolved", "last_seen": NOW, "title": "Backup restored"},
        ])
        self.assertEqual(summary["total_open"], 5, "resolved problems leave the counter")
        self.assertEqual(summary["groups_total"], 3, "the same kind for another operation stays a separate group")
        first = summary["groups"][0]
        self.assertEqual((first["kind"], first["operation_id"]), ("job_failure", "sync"))
        self.assertEqual(first["occurrences"], 12, "a group adds the occurrences recorded on its rows")
        self.assertEqual(first["severity"], "high", "a group keeps the most urgent severity of its rows")
        self.assertEqual(first["label"], "Synchronization failed — sync")
        self.assertEqual(first["action"], {"label": "Review failures",
                                           "route": "/admin/v2/?job_state=failed&job_action=sync#jobs"})
        backlog = next(group for group in summary["groups"] if group["kind"] == "sync_backlog")
        self.assertEqual(backlog["occurrences"], 2, "a row without a count is one occurrence")
        self.assertEqual([group["kind"] for group in summary["groups"]],
                         ["job_failure", "job_failure", "sync_backlog"],
                         "groups sort by urgency, then by the newest occurrence")

    def test_operations_are_never_parsed_from_message_text(self):
        summary = self.summarize([
            {"kind": "system", "severity": "low", "status": "open", "last_seen": NOW,
             "title": "Channel telegram:7 failed in op-77", "message": "job 44 for channel telegram:7"},
            {"kind": "system", "severity": "low", "status": "open", "last_seen": NOW - 1,
             "title": "Channel telegram:8 failed", "message": "job 45 for channel telegram:8"},
        ])
        self.assertEqual(summary["groups_total"], 1, "unrelated problems of one kind share a group")
        self.assertEqual(summary["groups"][0]["operation_id"], "")
        self.assertEqual(summary["groups"][0]["objects"], 2)
        self.assertEqual(summary["groups"][0]["last_seen"], admin_v2_issues._iso_time(NOW))

    def test_groups_are_capped_and_reported_for_the_full_list(self):
        summary = self.summarize([
            {"kind": "job_failure", "object_id": f"task-{index}", "severity": "medium",
             "status": "open", "last_seen": NOW - index, "title": "Task failed"}
            for index in range(7)
        ])
        self.assertEqual(len(summary["groups"]), admin_v2_issue_builder.ISSUE_SUMMARY_GROUP_LIMIT)
        self.assertEqual(summary["groups_total"], 7, "the full list still owns the hidden groups")
        self.assertEqual([group["operation_id"] for group in summary["groups"]],
                         ["task-0", "task-1", "task-2", "task-3", "task-4"],
                         "equal urgency is broken by the newest occurrence, then the operation")

    def test_multi_object_group_links_to_the_category_not_one_arbitrary_object(self):
        rows = [
            {"kind": "quality_finding", "object_id": "nfo_metadata_mismatch", "severity": "medium", "status": "open",
             "last_seen": NOW, "count": 7, "title": "NFO metadata differs",
             "action": {"label": "Review video", "route": "/admin/v2/?video_id=1#library"},
             "list_route": "/admin/v2/#system"},
            {"kind": "quality_finding", "object_id": "nfo_metadata_mismatch", "severity": "medium", "status": "open",
             "last_seen": NOW - 5, "title": "NFO metadata differs",
             "action": {"label": "Review video", "route": "/admin/v2/?video_id=2#library"},
             "list_route": "/admin/v2/#system"},
        ]
        self.assertEqual(self.summarize(rows)["groups"][0]["action"]["route"],
                         "/admin/v2/#system")
        self.assertEqual(self.summarize(rows[:1])["groups"][0]["action"]["route"],
                         "/admin/v2/?video_id=1#library",
                         "a single-object group keeps its own precise link")

    def test_unreadable_sources_keep_the_summary_incomplete(self):
        row = [{"kind": "system", "severity": "low", "status": "open", "last_seen": NOW}]
        self.assertTrue(self.summarize(row, {"incomplete": True})["incomplete"])
        self.assertTrue(self.summarize(row, {})["incomplete"], "a health block without an answer fails closed")
        self.assertTrue(admin_v2_issues._issue_summary(row, None)["incomplete"])
        self.assertFalse(self.summarize(row)["incomplete"])


class TestAdminV2IssueRoute(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_request():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        return Request({
            "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
            "path": "/admin/api/v2/issues", "raw_path": b"/admin/api/v2/issues",
            "query_string": b"", "headers": [], "client": ("127.0.0.1", 1),
            "server": ("test", 80), "root_path": "",
        }, receive=receive)

    @staticmethod
    def make_admin(blocked=False):
        class InlineAsyncio:
            @staticmethod
            async def to_thread(function, *args):
                return function(*args)
        return SimpleNamespace(
            _admin_blocked=lambda _request: blocked,
            JSONResponse=JSONResponse,
            asyncio=InlineAsyncio,
        )

    async def test_route_filters_sorts_and_paginates_issues(self):
        admin = self.make_admin()
        endpoint = admin_v2_issues.build_router(admin).routes[0].endpoint
        sample = [
            {"kind": "job_failure", "severity": "high", "status": "open", "last_seen": "2033-05-18T03:33:20Z", "object_id": "a"},
            {"kind": "sync_backlog", "severity": "medium", "status": "open", "last_seen": "2033-07-09T17:20:00Z", "object_id": "b"},
            {"kind": "job_failure", "severity": "high", "status": "open", "last_seen": "2033-05-18T03:31:40Z", "object_id": "c"},
        ]
        with patch.object(admin_v2_issues, "_build_issues", return_value=(sample, {"available": {}})):
            response = await endpoint(self.make_request(), "high", "", "open", 1, 1)

        self.assertEqual(response["total"], 2)
        self.assertEqual([item["object_id"] for item in response["issues"]], ["c"])
        self.assertEqual((response["offset"], response["limit"], response["truncated"]), (1, 1, False))

    async def test_route_summarizes_the_open_set_independently_of_filters(self):
        admin = self.make_admin()
        endpoint = admin_v2_issues.build_router(admin).routes[0].endpoint
        sample = [
            {"kind": "job_failure", "object_id": "a", "severity": "high", "status": "open",
             "last_seen": NOW, "title": "Task failed"},
            {"kind": "sync_backlog", "object_id": "b", "severity": "low", "status": "open",
             "last_seen": NOW, "title": "Sync queue is old"},
            {"kind": "sync_backlog", "object_id": "c", "severity": "low", "status": "open",
             "last_seen": NOW, "title": "Sync queue is old"},
        ]
        with patch.object(admin_v2_issues, "_build_issues", return_value=(sample, {"incomplete": False})):
            narrowed = await endpoint(self.make_request(), "high", "", "open", 0, 1)
            paged = await endpoint(self.make_request(), "", "", "open", 1, 1)

        self.assertEqual(narrowed["total"], 1, "the filtered total keeps its list meaning")
        self.assertEqual([item["object_id"] for item in narrowed["issues"]], ["a"])
        self.assertEqual(narrowed["summary"]["total_open"], 3,
                         "the Problems tile counts every unresolved problem, not the filtered page")
        self.assertEqual([group["operation_id"] for group in narrowed["summary"]["groups"]], ["a", "b", "c"])
        self.assertEqual([item["object_id"] for item in paged["issues"]], ["b"])
        self.assertEqual(paged["summary"], narrowed["summary"], "pagination must not move the group summary")

    async def test_route_accepts_quality_issue_filter(self):
        admin = self.make_admin()
        endpoint = admin_v2_issues.build_router(admin).routes[0].endpoint
        issue = {"kind": "quality_finding", "severity": "medium", "status": "open",
                 "last_seen": None, "object_id": "nfo_metadata_mismatch"}
        with patch.object(admin_v2_issues, "_build_issues", return_value=([issue], {"available": {}})):
            response = await endpoint(self.make_request(), "", "quality_finding", "open", 0, 50)

        self.assertEqual(response["total"], 1)
        self.assertEqual(response["issues"], [issue])

    async def test_route_accepts_backup_health_filter(self):
        admin = self.make_admin()
        endpoint = admin_v2_issues.build_router(admin).routes[0].endpoint
        with patch.object(admin_v2_issues, "_build_issues", return_value=([
            {"kind": "backup", "severity": "high", "status": "open", "last_seen": None, "object_id": None},
        ], {"available": {}})):
            response = await endpoint(self.make_request(), "", "backup", "open", 0, 50)
        self.assertEqual(response["total"], 1)
        self.assertEqual(response["issues"][0]["kind"], "backup")

    async def test_route_accepts_stalled_job_filter(self):
        admin = self.make_admin()
        endpoint = admin_v2_issues.build_router(admin).routes[0].endpoint
        with patch.object(admin_v2_issues, "_build_issues", return_value=([
            {"kind": "job_stalled", "severity": "high", "status": "open", "last_seen": None, "object_id": "job-7"},
        ], {"available": {}})):
            response = await endpoint(self.make_request(), "", "job_stalled", "open", 0, 50)
        self.assertEqual(response["total"], 1)
        self.assertEqual(response["issues"][0]["kind"], "job_stalled")

    async def test_route_checks_read_access_before_building_issues(self):
        admin = self.make_admin(blocked=True)
        endpoint = admin_v2_issues.build_router(admin).routes[0].endpoint
        with patch.object(admin_v2_issues, "_build_issues") as build:
            response = await endpoint(self.make_request())
        self.assertEqual(response.status_code, 403)
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
