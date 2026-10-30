"""Database-free contracts for V2 job actions and duplicate review reads."""

import sys
import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.responses import JSONResponse
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resolver"))
sys.path.insert(0, str(ROOT / "sync"))

import admin_v2_jobs
import admin_v2_tasks
from common.job_queue import ALL_ACTIONS
from admin_services import _JOB_PROGRESS_STAGES, _public_job, _safe_job_progress
import duplicate_jobs


def request(path, method="GET", body=b""):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    headers = []
    if body:
        headers.extend([(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())])
    return Request({
        "type": "http", "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers,
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
    }, receive=receive)


class InlineAsyncio:
    @staticmethod
    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)


class JobActionOptions(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_filter = False
        self.values = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "select" and attrs.get("id") == "jobActionFilter":
            self.in_filter = True
        elif tag == "option" and self.in_filter:
            value = attrs.get("value")
            if value:
                self.values.append(value)

    def handle_endtag(self, tag):
        if tag == "select" and self.in_filter:
            self.in_filter = False


def make_admin(*, blocked=False, denied=None):
    return SimpleNamespace(
        _admin_blocked=lambda _request: blocked,
        _write_denied=lambda _request: denied,
        JSONResponse=JSONResponse,
        asyncio=InlineAsyncio,
        HUB_DB_URL="postgresql://test.invalid/catalog",
        _wake_worker=lambda *_args, **_kwargs: None,
    )


class TestV2DuplicateRoutes(unittest.IsolatedAsyncioTestCase):
    async def test_availability_job_summary_shows_only_bounded_result_counts(self):
        job = admin_v2_jobs._safe_job({
            "id": "availability-complete", "action": "availability_check", "state": "done",
            "result": {
                "local_state": "availability_checked",
                "counts": {"available": 3, "unavailable": 1, "transient_error": 2,
                           "unknown": 4, "skipped": 1},
                "evidence": "private URL and token must stay hidden",
            },
        })
        self.assertEqual(
            job["result_summary"],
            "Playback check: 3 available, 1 unavailable, 2 temporary error, 4 inconclusive, 1 skipped.",
        )
        self.assertNotIn("private URL", str(job))
        self.assertNotIn("token", str(job))

        malformed = admin_v2_jobs._safe_job({
            "action": "availability_check", "state": "done",
            "result": {"local_state": "availability_checked",
                       "counts": {"available": "token", "unknown": 1000000}},
        })
        self.assertEqual(
            malformed["result_summary"],
            "Playback check recorded. Review item details for the latest results.",
        )

    async def test_sync_job_summary_points_to_per_source_results(self):
        failed = admin_v2_jobs._safe_job({
            "id": "sync-failed", "action": "sync", "state": "failed",
            "result": {"local_state": "sync failed", "message": "private error text"},
        })
        completed = admin_v2_jobs._safe_job({
            "id": "sync-complete", "action": "sync", "state": "done",
            "result": {"local_state": "sync completed", "message": "private result text"},
        })
        self.assertEqual(
            failed["result_summary"],
            "Sync failed. Open Sources to inspect per-source results.",
        )
        self.assertEqual(
            completed["result_summary"],
            "Sync completed. Open Sources to review per-source counts.",
        )
        self.assertNotIn("private", str(failed))
        self.assertNotIn("private", str(completed))

        channel = admin_v2_jobs._safe_job({
            "action": "sync", "state": "done",
            "result": {"local_state": "sync completed", "source": "telegram:-100123"},
        })
        self.assertEqual(channel["source_id"], "telegram:-100123")
        unsafe = admin_v2_jobs._safe_job({
            "action": "sync", "state": "done",
            "result": {"source": "telegram:https://private-token@example.invalid"},
        })
        self.assertNotIn("source_id", unsafe, "URLs and credential-like values must not escape the job projection")
        unrelated = admin_v2_jobs._safe_job({
            "action": "backup_create", "state": "done",
            "result": {"source": "telegram:-100123"},
        })
        self.assertNotIn("source_id", unrelated, "source links are limited to sync jobs")

    async def test_sync_job_summary_sanitizes_per_source_failures(self):
        failed = admin_v2_jobs._safe_job({
            "id": "sync-failed-source", "action": "sync", "state": "failed",
            "result": {
                "local_state": "sync failed",
                "source_failures": [{
                    "label": "tg:[Test Channel]",
                    "error": "Sync failed (RuntimeError).",
                    "detail": "Telegram fetch/history operation failed.",
                }],
            },
        })
        self.assertEqual(
            failed["result_summary"],
            "Sync failed: tg:[Test Channel]: Telegram fetch/history operation failed.. "
            "Open Sources for per-source results.",
        )

    async def test_failure_detail_exposes_only_a_safe_classification(self):
        private_error = "Connection refused at https://private-token@example.invalid/private/path"
        row = {
            "id": "failed-task", "action": "performer_merge", "state": "failed",
            "payload": "{}", "result": json.dumps({"error": private_error}),
            "error": None,
        }
        legacy = _public_job(row)
        self.assertNotIn("failure_code", legacy)

        v2 = _public_job(row, include_failure_code=True)
        self.assertEqual(v2["failure_code"], "connection_failed")
        self.assertNotIn("private-token", str(v2))
        projected = admin_v2_jobs._safe_job(v2)
        self.assertEqual(projected["error"], "Could not connect to a required service.")
        self.assertNotIn("private-token", str(projected))

        unknown = admin_v2_jobs._safe_job({
            "state": "failed", "failure_code": "private-token", "error": "Job failed. Inspect local worker logs for details.",
        })
        self.assertEqual(unknown["error"], "Job failed. Inspect local worker logs for details.")
        self.assertNotIn("private-token", str(unknown))

    async def test_job_filter_accepts_every_action_advertised_by_the_queue(self):
        self.assertEqual(admin_v2_jobs.JOB_ACTIONS, frozenset(ALL_ACTIONS))
        template = (ROOT / "resolver/templates/admin_v2.html").read_text(encoding="utf-8")
        options = JobActionOptions()
        options.feed(template)
        self.assertEqual(set(options.values), set(ALL_ACTIONS) | {"phash_generation", "thumbnail_generation", "phash_thumbnail_legacy"})
        jobs_script = (ROOT / "resolver/static/admin-v2/jobs.js").read_text(encoding="utf-8")
        labels_block = re.search(r"const ACTION_LABELS = \{(.*?)\n  \};", jobs_script, re.S)
        self.assertIsNotNone(labels_block)
        labels = set(re.findall(r"\b([a-z_]+):\s*['\"]", labels_block.group(1)))
        self.assertEqual(labels, set(ALL_ACTIONS) | {"phash_generation", "thumbnail_generation", "phash_thumbnail_legacy"})
        routes = {route.path: route.endpoint for route in admin_v2_jobs.build_router(make_admin()).routes}
        endpoint = routes["/api/v2/jobs"]
        page = {"available": True, "jobs": [], "total": 0, "active_total": 0}
        with patch("admin_services.job_listing", return_value=page) as listing:
            response = await endpoint(
                request("/admin/api/v2/jobs?action=performer_refresh"),
                state="all", action="performer_refresh", offset=0, limit=50,
            )
        self.assertEqual(response["jobs"], [])
        listing.assert_called_once_with(
            "postgresql://test.invalid/catalog", 50, "", "performer_refresh", 0,
            include_failure_code=True,
        )

    def test_historical_telegram_worker_actions_have_operator_facing_labels(self):
        source = (ROOT / "resolver/static/admin-v2/jobs.js").read_text(encoding="utf-8")
        self.assertIn("lukayan_fingerprint: 'Scene fingerprint · legacy'", source)
        self.assertIn("telegram_fingerprint: 'Scene fingerprint'", source)
        self.assertIn("key.startsWith('lukayan_') ? 'Scene fingerprint · legacy'", source)

    async def test_unified_tasks_routes_project_both_queues_and_validate_filters(self):
        admin = make_admin()
        routes = {route.path: route.endpoint for route in admin_v2_tasks.build_router(admin).routes}
        listing = routes["/api/v2/tasks"]
        detail = routes["/api/v2/tasks/{queue}/{task_id}"]
        page = {
            "available": True, "tasks": [{"queue": "phash", "id": "p-1", "state": "processing"}],
            "total": 1, "active_total": 1, "offset": 0, "limit": 50, "truncated": False,
        }
        with patch("admin_v2_tasks._list_tasks", return_value=page) as list_tasks, \
             patch("common.worker_capabilities.read_worker_capabilities", return_value={"available": True}):
            response = await listing(
                request("/admin/api/v2/tasks?queue=all"), queue="all", state="", action="",
                offset=0, limit=50, job_id="",
            )
        self.assertEqual(response["tasks"][0]["queue"], "phash")
        self.assertEqual(response["workers"]["phash"], {"available": True})
        list_tasks.assert_called_once_with(
            "postgresql://test.invalid/catalog", queue="all", state="", action="",
            offset=0, limit=50, job_id=None,
        )

        invalid = await listing(
            request("/admin/api/v2/tasks?queue=unknown"), queue="unknown", state="", action="",
            offset=0, limit=50, job_id="",
        )
        self.assertEqual(invalid.status_code, 400)

        denied_listing = {route.path: route.endpoint for route in admin_v2_tasks.build_router(
            make_admin(blocked=True)).routes}["/api/v2/tasks"]
        with patch("admin_v2_tasks._list_tasks") as list_tasks:
            denied = await denied_listing(
                request("/admin/api/v2/tasks"), queue="all", state="", action="",
                offset=0, limit=50, job_id="",
            )
        self.assertEqual(denied.status_code, 403)
        list_tasks.assert_not_called()

        with patch("admin_v2_tasks._list_tasks", return_value=page) as list_tasks:
            detail_result = await detail(request("/admin/api/v2/tasks/phash/p-1"), "phash", "p-1")
        self.assertEqual(detail_result["task"]["id"], "p-1")
        list_tasks.assert_called_once_with(
            "postgresql://test.invalid/catalog", queue="phash", state="", action="",
            offset=0, limit=1, job_id="p-1",
        )

    def test_audit_job_labels_keep_legacy_full_scope_and_separate_new_modes(self):
        file_job = admin_v2_jobs._safe_job({
            "id": "file-job", "action": "consistency_audit", "state": "queued",
            "payload": {"mode": "file_consistency"},
        })
        legacy_job = admin_v2_jobs._safe_job({
            "id": "legacy-job", "action": "consistency_audit", "state": "queued", "payload": {},
        })
        metadata_job = admin_v2_jobs._safe_job({
            "id": "metadata-job", "action": "metadata_quality_audit", "state": "queued", "payload": {},
        })
        self.assertEqual(file_job["audit_mode"], "file_consistency")
        self.assertEqual(legacy_job["audit_mode"], "full")
        self.assertEqual(metadata_job["audit_mode"], "metadata_quality")
        source = (ROOT / "resolver/static/admin-v2/jobs.js").read_text(encoding="utf-8")
        self.assertIn("auditMode === 'file_consistency'", source)
        self.assertIn("auditMode === 'full'", source)

    async def test_worker_progress_stages_are_accepted_by_both_api_projections(self):
        unsupported_v1 = duplicate_jobs._JOB_STAGES - _JOB_PROGRESS_STAGES
        unsupported_v2 = duplicate_jobs._JOB_STAGES - admin_v2_jobs.JOB_STAGE_LABELS
        self.assertEqual(unsupported_v1, set())
        self.assertEqual(unsupported_v2, set())

        result = {"progress": {"stage": "metadata_tpdb", "completed": 2, "total": 5},
                  "events": [{"stage": "metadata_tpdb", "completed": 2, "total": 5}]}
        progress, events = _safe_job_progress(result)
        self.assertEqual(progress, {"stage": "metadata_tpdb", "completed": 2, "total": 5})
        self.assertEqual(events, [{"stage": "metadata_tpdb", "completed": 2, "total": 5}])
        self.assertEqual(admin_v2_jobs._safe_job({"state": "processing", "progress": progress})["stage"], "metadata_tpdb")

    def test_external_metadata_provider_stages_have_safe_operator_labels(self):
        javascript = (ROOT / "resolver/static/admin-v2/jobs.js").read_text(encoding="utf-8")
        labels = {
            "metadata_pbc": "Enriching actor metadata from PBC",
            "metadata_photos": "Checking actor portrait providers",
            "metadata_tpdb": "Enriching actor and video metadata from ThePornDB",
            "metadata_stashdb": "Enriching actor metadata from StashDB",
            "metadata_scenes": "Enriching video metadata from studio catalogs and ThePornDB",
        }
        for stage, label in labels.items():
            self.assertIn(f"{stage}: '{label}'", javascript)

        stale = admin_v2_jobs._safe_job(
            {"state": "processing", "updated_at": 2_000_000_000 - 91,
             "payload": {"secret": "must not escape"}},
            now=2_000_000_000,
        )
        self.assertTrue(stale["activity_stale"])
        self.assertNotIn("payload", stale)
        self.assertFalse(admin_v2_jobs._safe_job(
            {"state": "processing", "updated_at": 2_000_000_000 - 20},
            now=2_000_000_000,
        )["activity_stale"])
        self.assertFalse(admin_v2_jobs._safe_job(
            {"state": "processing"}, now=2_000_000_000,
        )["activity_stale"])

    async def test_retry_and_cancel_are_exposed_as_v2_write_routes(self):
        wakes = []
        stub = make_admin()
        stub._wake_worker = lambda action: wakes.append(action)
        routes = {route.path: route.endpoint for route in admin_v2_jobs.build_router(stub).routes}
        with patch("common.job_queue.read_job", return_value={"action": "sync"}), \
             patch("common.job_queue.retry_job", return_value="retry-2"):
            retried = await routes["/api/v2/jobs/{job_id}/retry"](
                request("/admin/api/v2/jobs/job-1/retry", "POST"), "job-1")
        self.assertEqual(retried["job_id"], "retry-2")
        self.assertEqual(wakes, ["sync"])

        with patch("common.job_queue.cancel_queued_job", return_value={"outcome": "cancelled"}):
            cancelled = await routes["/api/v2/jobs/{job_id}/cancel"](
                request("/admin/api/v2/jobs/job-1/cancel", "POST"), "job-1")
        self.assertEqual(cancelled["state"], "cancelled")

    async def test_duplicate_scan_uses_bounded_v2_contract(self):
        routes = {route.path: route.endpoint for route in admin_v2_jobs.build_router(make_admin()).routes}
        endpoint = routes["/api/v2/duplicates"]
        expected = {"pairs": [], "total": 0, "scanned": 0, "complete": True}
        with patch("video_duplicates.find_duplicate_candidates", return_value=expected) as scan:
            response = await endpoint(request("/admin/api/v2/duplicates"), 25, 0, "review")
        self.assertEqual(response["pairs"], [])
        scan.assert_called_once_with("postgresql://test.invalid/catalog", 25, 0, "review")

    async def test_duplicate_scan_rejects_bad_category_and_admin_block(self):
        routes = {route.path: route.endpoint for route in admin_v2_jobs.build_router(make_admin()).routes}
        endpoint = routes["/api/v2/duplicates"]
        invalid = await endpoint(request("/admin/api/v2/duplicates"), 25, 0, "unknown")
        self.assertEqual(invalid.status_code, 400)
        blocked_routes = {route.path: route.endpoint for route in admin_v2_jobs.build_router(make_admin(blocked=True)).routes}
        blocked = await blocked_routes["/api/v2/duplicates"](request("/admin/api/v2/duplicates"), 25, 0, "")
        self.assertEqual(blocked.status_code, 403)

    async def test_duplicate_action_obeys_write_policy_before_parsing(self):
        denied = JSONResponse({"error": "Read only"}, status_code=403)
        routes = {route.path: route.endpoint for route in admin_v2_jobs.build_router(make_admin(denied=denied)).routes}
        endpoint = routes["/api/v2/duplicates/actions"]
        response = await endpoint(request("/admin/api/v2/duplicates/actions", "POST"))
        self.assertEqual(response.status_code, 403)

    async def test_duplicate_action_bounds_body_and_requires_exact_payload(self):
        routes = {route.path: route.endpoint for route in admin_v2_jobs.build_router(make_admin()).routes}
        endpoint = routes["/api/v2/duplicates/actions"]
        oversized = await endpoint(request("/admin/api/v2/duplicates/actions", "POST", b" " * 8193))
        self.assertEqual(oversized.status_code, 413)
        malformed = await endpoint(request(
            "/admin/api/v2/duplicates/actions", "POST",
            b'{"action":"dismiss","left_id":"a","right_id":"b","extra":true}',
        ))
        self.assertEqual(malformed.status_code, 400)
        long_id = await endpoint(request(
            "/admin/api/v2/duplicates/actions", "POST",
            (('{"action":"dismiss","left_id":"' + ('x' * 513)
              + '","right_id":"b"}').encode()),
        ))
        self.assertEqual(long_id.status_code, 400)

    async def test_visual_pair_can_be_marked_different_without_scan_candidate(self):
        routes = {route.path: route.endpoint for route in admin_v2_jobs.build_router(make_admin()).routes}
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (1,)
        with patch('video_duplicates.scan_duplicate_index', return_value={'pairs': []}), \
             patch('video_duplicates.enqueue_job', return_value='job-1') as enqueue, \
             patch('common.db.connect', return_value=conn):
            response = await routes['/api/v2/duplicates/actions'](request(
                '/api/v2/duplicates/actions', 'POST',
                b'{"action":"dismiss","left_id":"b","right_id":"a"}'))
        self.assertEqual(response['job_id'], 'job-1')
        enqueue.assert_called_once_with('postgresql://test.invalid/catalog', 'dismiss',
                                        {'left_id': 'a', 'right_id': 'b'})
        conn.close.assert_called_once()


class TestArtworkResultSummary(unittest.TestCase):
    def _job(self, result):
        return {"action": "artwork_download", "state": "done", "result": result}

    def test_generated_artwork_survives_projection_and_reports_refresh(self):
        for refresh in ("updated", "failed"):
            job = admin_v2_jobs._safe_job({
                "action": "artwork_generate", "state": "done",
                "result": {"local_state": "artwork_generated", "jellyfin_refresh": refresh},
            })
            self.assertEqual(job["local_state"], "artwork_generated")
            self.assertTrue(job["result_summary"].startswith("Artwork generated from a video frame."))
            self.assertEqual("Jellyfin refresh failed" in job["result_summary"], refresh == "failed")

    def test_downloaded_with_failed_refresh_mentions_refresh_failure(self):
        summary = admin_v2_jobs._result_summary(self._job({
            "downloaded": True, "provider": "StashDB",
            "local_state": "artwork_downloaded", "jellyfin_refresh": "failed",
        }))
        self.assertEqual(summary, "Artwork downloaded from StashDB. Local result saved; Jellyfin refresh failed.")

    def test_downloaded_with_successful_refresh_reports_success(self):
        summary = admin_v2_jobs._result_summary(self._job({
            "downloaded": True, "provider": "ThePornDB",
            "local_state": "artwork_downloaded", "jellyfin_refresh": "updated",
        }))
        self.assertEqual(summary, "Artwork downloaded from ThePornDB.")

    def test_lookup_failed_state_has_own_message(self):
        summary = admin_v2_jobs._result_summary(self._job({
            "downloaded": False, "local_state": "artwork_lookup_failed",
        }))
        self.assertEqual(summary, "The artwork search did not complete; metadata providers reported errors.")

    def test_lookup_failed_state_survives_safe_job_projection(self):
        job = admin_v2_jobs._safe_job(self._job({
            "downloaded": False, "local_state": "artwork_lookup_failed",
        }))
        self.assertEqual(job["local_state"], "artwork_lookup_failed")


if __name__ == "__main__":
    unittest.main()
