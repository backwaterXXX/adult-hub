"""Focused contracts for Admin v2 catalog and issue-center reads."""

import asyncio
import json
import os
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.responses import JSONResponse
from starlette.requests import Request
from tests.pg_helper import PgTestCase
from common import db


import resolver.admin_library_v2 as admin_library_v2
import resolver.admin_v2_issues as admin_v2_issues
import resolver.admin_v2_jobs as admin_v2_jobs

import sync.duplicate_jobs as duplicate_jobs


def request(query=b""):
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET",
        "scheme": "http", "path": "/admin/api/v2/test", "raw_path": b"/admin/api/v2/test",
        "query_string": query, "headers": [], "client": ("127.0.0.1", 1),
        "server": ("test", 80), "root_path": "",
    })


def admin_stub():
    class InlineAsyncio:
        @staticmethod
        async def to_thread(function, *args, **kwargs):
            return function(*args, **kwargs)

    return SimpleNamespace(
        _admin_blocked=lambda _request: False,
        _write_denied=lambda _request: None,
        JSONResponse=JSONResponse,
        asyncio=InlineAsyncio,
        HUB_DB_URL="postgresql://test.invalid/catalog",
        MEDIA_DIR=Path("/unused/media"),
        _wake_worker=Mock(),
    )


class TestCatalogReadContract(unittest.TestCase):
    def test_channel_identity_is_not_inferred_from_studio(self):
        self.assertEqual(admin_library_v2._channel_id("telegram:-100123:message"), "-100123")
        self.assertIsNone(admin_library_v2._channel_id("secondweb:slug"))

    def test_video_detail_is_safe_and_uses_iso_utc_dates(self):
        video = admin_library_v2._serialize_video({
            "id": "telegram:-100123:message", "provider": "telegram", "title": "Example",
            "duration": 90, "thumbnail": "https://resolver.invalid/admin/api/thumb?provider=telegram&ref=-100123%3A42%3Aunique",
            "performers": '["Performer"]', "categories": "[]", "tags": '["Drama"]',
            "playable": None, "studio": "Studio", "plot": "Summary", "first_seen": 1,
            "last_seen": None, "file_count": 1, "file_categories": ["video"],
            "has_local_files": True, "dead_stream": False,
        }, detail=True)
        self.assertEqual(video["channel_id"], "-100123")
        self.assertEqual(video["first_seen"], "1970-01-01T00:00:01Z")
        self.assertEqual(video["availability"], "unknown")
        self.assertNotIn("local_path", video)
        self.assertEqual(video["performers"], ["Performer"])
        self.assertEqual(video["missing_fields"], [])

    def test_unavailable_reason_is_safe_and_detection_time_is_preserved(self):
        probe = admin_library_v2._serialize_video({
            "id": "telegram:-100123:42", "provider": "telegram", "playable": 0,
            "dead_stream": True, "unavailable_reason": "dead CDN stream (probe)",
            "unavailable_detected_at": 1,
        }, detail=False)
        self.assertEqual(probe["unavailable_reason"], "The CDN stream probe failed.")
        self.assertEqual(probe["unavailable_detected_at"], "1970-01-01T00:00:01Z")

        unknown = admin_library_v2._serialize_video({
            "id": "telegram:-100123:43", "playable": 0, "dead_stream": True,
            "unavailable_reason": "Timeout https://internal.invalid token=secret",
        }, detail=False)
        self.assertEqual(unknown["unavailable_reason"], "A dead-stream record is present.")
        self.assertNotIn("internal.invalid", str(unknown))
        self.assertNotIn("secret", str(unknown))

        playable_only = admin_library_v2._serialize_video({"id": "legacy", "playable": 0}, detail=False)
        self.assertEqual(playable_only["unavailable_reason"], "Marked unplayable.")

    def test_telegram_post_link_is_derived_only_from_valid_video_identity(self):
        valid = admin_library_v2._serialize_video({
            "id": "telegram:-1001234567890:42:unique-file-ref", "provider": "telegram",
        }, detail=True)
        self.assertEqual(valid["telegram_url"], "https://t.me/c/1234567890/42")
        invalid = [
            {"id": "telegram:-100123:../42:unique", "provider": "telegram"},
            {"id": "telegram:-100123:0:unique", "provider": "telegram"},
            {"id": "https://t.me/c/123/42", "provider": "telegram"},
            {"id": "telegram:-100123:42:unique", "provider": "other"},
        ]
        for row in invalid:
            with self.subTest(row=row):
                self.assertIsNone(admin_library_v2._serialize_video(row, detail=True)["telegram_url"])

    def test_thumbnail_proxy_is_normalized_and_secret_query_values_are_rejected(self):
        self.assertEqual(
            admin_library_v2._safe_thumbnail(
                "https://resolver.invalid/admin/api/thumb?provider=telegram&ref=-1007%3A42%3Aunique"
            ),
            "/admin/api/thumb?provider=telegram&ref=-1007%3A42%3Aunique",
        )
        self.assertIsNone(admin_library_v2._safe_thumbnail(
            "https://resolver.invalid/admin/api/thumb?provider=telegram&ref=42&token=secret"
        ))
        self.assertIsNone(admin_library_v2._safe_thumbnail("https://images.invalid/private.jpg?key=secret"))

    def test_detail_reports_missing_metadata_fields(self):
        video = admin_library_v2._serialize_video({
            "id": "telegram:-1007:42", "title": "", "performers": [], "studio": None,
            "plot": "  ", "thumbnail": None,
        }, detail=True)
        self.assertEqual(video["missing_fields"], ["title", "performers", "studio", "plot", "thumbnail"])

    def test_local_file_status_checks_registered_roles_and_never_follows_symlinks(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix in (".strm", ".nfo", ".jpg"):
                (root / f"stable{suffix}").write_text("x", encoding="utf-8")
            self.assertEqual(admin_library_v2._local_file_status(["stable.strm"], root),
                             {"stream": "present", "nfo": "present", "artwork": "present"})
            (root / "stable.nfo").unlink()
            self.assertEqual(admin_library_v2._local_file_status(["stable.strm"], root),
                             {"stream": "present", "nfo": "missing", "artwork": "present"})
            (root / "stable.strm").unlink()
            (root / "stable.strm").symlink_to(root / "outside.strm")
            self.assertEqual(admin_library_v2._local_file_status(["stable.strm"], root)["stream"], "unknown")
            self.assertEqual(admin_library_v2._local_file_status(["../outside.strm"], root),
                             {"stream": "unknown", "nfo": "unknown", "artwork": "unknown"})
            self.assertEqual(admin_library_v2._local_file_status(["stable.strm"], root / "missing"),
                             {"stream": "unknown", "nfo": "unknown", "artwork": "unknown"})
            with patch.object(admin_library_v2.os, "scandir", side_effect=PermissionError):
                self.assertEqual(admin_library_v2._local_file_status(["stable.strm"], root),
                                 {"stream": "unknown", "nfo": "unknown", "artwork": "unknown"})

            # If artwork file is a black image, report artwork as missing
            from PIL import Image
            black_img = Image.new("RGB", (64, 64), color=(0, 0, 0))
            black_img.save(root / "stable.jpg", format="JPEG")
            (root / "stable.strm").unlink()
            (root / "stable.strm").write_text("x", encoding="utf-8")
            (root / "stable.nfo").write_text("x", encoding="utf-8")
            self.assertEqual(admin_library_v2._local_file_status(["stable.strm"], root),
                             {"stream": "present", "nfo": "present", "artwork": "missing"})

    def test_generate_artwork_endpoint_queues_job_and_wakes_worker(self):
        admin = admin_stub()
        endpoint = next(r.endpoint for r in admin_library_v2.build_router(admin).routes if r.endpoint.__name__ == "admin_v2_library_video_generate_artwork")
        with patch.object(admin_library_v2, "_get_target_artwork_path", return_value=Path("/tmp/sample.jpg")), \
             patch("common.job_queue.enqueue_job", return_value="artwork-job") as enqueue_mock, \
             patch("common.image_utils.extract_video_frame") as extract_mock:
            req = Request({
                "type": "http", "http_version": "1.1", "method": "POST",
                "scheme": "http", "path": "/admin/api/v2/library/videos/telegram:123/generate-artwork",
                "raw_path": b"/admin/api/v2/library/videos/telegram:123/generate-artwork",
                "query_string": b"", "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 1),
                "server": ("test", 80), "root_path": "",
            })
            # Provide empty json body
            async def json_body():
                return {"offset_seconds": 60}
            req.json = json_body

            result = asyncio.run(endpoint(req, "telegram:123"))
            self.assertEqual(result.status_code, 202)
            self.assertEqual(json.loads(result.body), {"ok": True, "job_id": "artwork-job", "state": "queued"})
            enqueue_mock.assert_called_once_with(admin.HUB_DB_URL, "artwork_generate",
                                                 {"video_id": "telegram:123", "offset_seconds": 60})
            admin._wake_worker.assert_called_once_with("jobs")
            extract_mock.assert_not_called()

    def test_video_size_serialization(self):
        for size in (None, 123456789, 2500000000):
            video = admin_library_v2._serialize_video(
                {"id": "telegram:123", "file_size_bytes": size}, detail=True)
            self.assertEqual(video["file_size_bytes"], size)

    def test_list_caps_limit_and_rejects_invalid_filter_before_database_access(self):
        admin = admin_stub()
        router = admin_library_v2.build_router(admin)
        endpoint = router.routes[0].endpoint
        with patch.object(admin_library_v2, "_run_catalog_query", return_value={"ok": True, "items": [], "page": {"limit": 100}}) as run:
            result = asyncio.run(endpoint(request(), limit="999", offset="5"))
        self.assertEqual(result["page"]["limit"], 100)
        self.assertEqual(run.call_args.args[1]["offset"], 5)
        invalid = asyncio.run(endpoint(request(), availability="maybe"))
        self.assertEqual(invalid.status_code, 400)

    def test_detail_returns_404_for_unknown_id(self):
        admin = admin_stub()
        endpoint = next(r.endpoint for r in admin_library_v2.build_router(admin).routes if r.endpoint.__name__ == "admin_v2_library_video")
        with patch.object(admin_library_v2, "_get_video", return_value=None):
            result = asyncio.run(endpoint(request(), "telegram:historical:id"))
        self.assertEqual(result.status_code, 404)

    def test_detail_preserves_legacy_and_colon_rich_ids_exactly(self):
        admin = admin_stub()
        endpoint = next(r.endpoint for r in admin_library_v2.build_router(admin).routes if r.endpoint.__name__ == "admin_v2_library_video")
        for video_id in ("AQADabc123unique", "telegram:legacy-channel:42:stable-ref"):
            with self.subTest(video_id=video_id), patch.object(
                admin_library_v2, "_get_video", return_value={"id": video_id}
            ) as get_video:
                result = asyncio.run(endpoint(request(), video_id))
            self.assertEqual(result, {"ok": True, "video": {"id": video_id}})
            self.assertEqual(get_video.call_args.args[1], video_id)

    def test_sort_queries_keep_an_id_tie_breaker_and_apply_page_offset(self):
        class Result:
            def fetchone(self):
                return {"total": 2}

            def fetchall(self):
                return []

        class Connection:
            row_factory = None

            def __init__(self):
                self.queries = []

            def execute(self, sql, params=()):
                self.queries.append((sql, params))
                return Result()

            def close(self):
                pass

        columns = set(admin_library_v2._VIDEO_OPTIONAL_COLUMNS) | {"id"}
        for sort, expected_order in admin_library_v2._SORTS.items():
            with self.subTest(sort=sort):
                conn = Connection()
                with patch.object(admin_library_v2.db, "connect", return_value=conn), \
                     patch.object(admin_library_v2.db, "table_names", return_value={"videos"}), \
                     patch.object(admin_library_v2.db, "table_columns", return_value=columns):
                    result = admin_library_v2._run_catalog_query("unused", {
                        "q": "", "channel_id": "", "studio": "", "performer": "", "tag": "",
                        "availability": "all", "missing": [], "sort": sort, "offset": 50, "limit": 50,
                    })
                list_sql, list_params = conn.queries[1]
                self.assertIn(f"ORDER BY {expected_order}", list_sql)
                self.assertEqual(result["page"], {
                    "limit": 50, "offset": 50, "total": 2, "total_exact": True,
                    "has_more": False, "has_previous": False, "next_cursor": None, "previous_cursor": None,
                })

    def test_unavailable_filter_selects_reason_and_detection_timestamp(self):
        class Result:
            def fetchone(self):
                return {"total": 1}

            def fetchall(self):
                return []

        class Connection:
            row_factory = None

            def __init__(self):
                self.queries = []

            def execute(self, sql, params=()):
                self.queries.append((sql, params))
                return Result()

            def close(self):
                pass

        conn = Connection()
        columns = set(admin_library_v2._VIDEO_OPTIONAL_COLUMNS) | {"id"}
        with patch.object(admin_library_v2.db, "connect", return_value=conn), \
             patch.object(admin_library_v2.db, "table_names", return_value={"videos", "dead_streams"}), \
             patch.object(admin_library_v2.db, "table_columns", return_value=columns):
            admin_library_v2._run_catalog_query("unused", {
                "q": "", "channel_id": "", "studio": "", "performer": "", "tag": "",
                "availability": "unavailable", "missing": [], "sort": "title", "offset": 0, "limit": 50,
            })
        list_sql = conn.queries[1][0]
        self.assertIn("d.reason AS unavailable_reason", list_sql)
        self.assertIn("d.detected_at AS unavailable_detected_at", list_sql)
        self.assertIn("LEFT JOIN dead_streams d ON d.video_id = v.id", list_sql)
        self.assertIn("v.playable = 0 OR (d.video_id IS NOT NULL)", list_sql)

    def test_detail_includes_safe_source_records_without_paths_or_source_urls(self):
        class Result:
            def __init__(self, one=None, many=None):
                self.one, self.many = one, many or []

            def fetchone(self):
                return self.one

            def fetchall(self):
                return self.many

        class Connection:
            row_factory = None

            def __init__(self):
                self.sql = []

            def execute(self, query, params=()):
                self.sql.append(query)
                if "FROM video_sources" in query:
                    return Result(many=[{"provider": "telegram", "provider_ref": "42",
                                         "channel_id": "-1007", "priority": 1, "is_active": 1}])
                if "SELECT file_path FROM synced_files" in query:
                    return Result(many=[{"file_path": "stable.strm"}])
                if "WHERE v.id" in query:
                    row = dict.fromkeys(admin_library_v2._VIDEO_OPTIONAL_COLUMNS)
                    row.update({"id": "telegram:-1007:42", "provider": "telegram", "title": "Safe",
                                "duration": 120, "performers": [], "categories": [], "tags": [],
                                "playable": 1, "first_seen": 1, "last_seen": 2, "file_count": 1,
                                "file_categories": ["video"], "has_local_files": True, "dead_stream": False,
                                "scene_match_source": "stashdb", "scene_match_external_id": "abc-uuid",
                                "scene_match_url": "https://stashdb.org/scenes/abc-uuid"})
                    return Result(one=row)
                raise AssertionError(query)

            def close(self):
                pass

        conn = Connection()
        with patch.object(admin_library_v2.db, "connect", return_value=conn), \
             patch.object(admin_library_v2.db, "table_names", return_value={"videos", "video_sources", "synced_files"}), \
             patch.object(admin_library_v2.db, "table_columns", return_value=set(admin_library_v2._VIDEO_OPTIONAL_COLUMNS) | {"id"}):
            video = admin_library_v2._get_video("unused", "telegram:-1007:42")
        self.assertEqual(video["sources"], [{"provider": "telegram", "provider_ref": "42",
                                             "channel_id": "-1007", "priority": 1, "active": True}])
        self.assertEqual(video["telegram_url"], "https://t.me/c/7/42")
        self.assertEqual(video["scene_match_source"], "stashdb")
        self.assertEqual(video["scene_match_external_id"], "abc-uuid")
        self.assertEqual(video["scene_match_url"], "https://stashdb.org/scenes/abc-uuid")
        source_query = next(sql for sql in conn.sql if "FROM video_sources" in sql)
        self.assertNotIn("source_url", source_query)
        self.assertEqual(video["local_files"], {"stream": "unknown", "nfo": "unknown", "artwork": "unknown"})
        self.assertNotIn("file_path", video)


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
            "result": {
            "local_state": "hidden", "jellyfin_refresh": "pending",
            "jellyfin_import": "unexpected-secret-state",
                "source_url": "https://secret.invalid/key", "path": "/srv/private/movie.strm",
            },
        })
        self.assertEqual(safe["stage"], "jellyfin_refresh")
        self.assertEqual(safe["local_state"], "hidden")
        self.assertEqual(safe["jellyfin_refresh"], "pending")
        self.assertIsNone(safe["jellyfin_import"])
        self.assertNotIn("source_url", safe)
        self.assertNotIn("path", safe)
        self.assertNotIn("result", safe)

    def test_legacy_processing_job_has_no_invented_progress(self):
        safe = admin_v2_jobs._safe_job({"id": "old", "state": "processing", "result": None})
        self.assertEqual(safe["stage"], "processing")
        self.assertIsNone(safe["local_state"])
        self.assertIsNone(safe["jellyfin_refresh"])

    def test_job_projection_exposes_safe_local_file_repair_state(self):
        safe = admin_v2_jobs._safe_job({
            "id": "audit-repair", "action": "audit_repair_nfo", "state": "done",
            "result": {"local_state": "files_repaired", "restored_streams": 1,
                       "path": "/private/library/video.strm"},
        })
        self.assertEqual(safe["local_state"], "files_repaired")
        self.assertNotIn("restored_streams", safe)
        self.assertNotIn("path", safe)

    def test_job_projection_explains_unconfirmed_jellyfin_import(self):
        safe = admin_v2_jobs._safe_job({
            "id": "metadata-1", "action": "metadata_update", "state": "done",
            "result": {"jellyfin_refresh": "updated", "jellyfin_import": "not_confirmed"},
        })
        self.assertEqual(safe["jellyfin_refresh"], "updated")
        self.assertEqual(safe["jellyfin_import"], "not_confirmed")
        self.assertIn("import is not confirmed", safe["result_summary"])

    def test_job_projection_exposes_positive_jellyfin_import_confirmation(self):
        safe = admin_v2_jobs._safe_job({
            "id": "job", "action": "metadata_update", "state": "done",
            "result": {"local_state": "metadata_updated", "jellyfin_import": "confirmed",
                       "jellyfin_import_confirmed_at": 1234567890},
        })
        self.assertEqual(safe["jellyfin_import"], "confirmed")
        self.assertEqual(safe["result_summary"], "Jellyfin imported the current local metadata file.")

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

    def test_audit_reimport_projection_does_not_claim_local_write_or_import_completion(self):
        safe = admin_v2_jobs._safe_job({
            "id": "audit-refresh", "action": "audit_import_refresh", "state": "done",
            "result": {"refresh_requested": True, "jellyfin_import": "not_confirmed"},
        })
        self.assertEqual(safe["action"], "audit_import_refresh")
        self.assertEqual(safe["result_summary"],
                         "Jellyfin accepted the refresh request; check the library after its next refresh.")
        self.assertNotIn("jellyfin_import_confirmed_at", safe)

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

    def test_jellyfin_checkpoint_is_persisted_before_remote_request(self):
        class Connection:
            def __init__(self):
                self.statements = []
                self.result = {"operation_id": "operation-2", "undo_snapshots": [{"video": {"id": "one"}}]}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params):
                if sql.startswith("SELECT result"):
                    return SimpleNamespace(fetchone=lambda: (json.dumps(self.result),))
                self.statements.append((sql, params))
                self.result = json.loads(params[0])
                return SimpleNamespace(rowcount=1)

        conn = Connection()
        observed = []
        services = SimpleNamespace(trigger_jellyfin_refresh=lambda: observed.append(list(conn.statements)) or True)
        with patch.dict(os.environ, {"JELLYFIN_URL": "http://jellyfin:8096", "JELLYFIN_API_KEY": "test-key"}):
            result = duplicate_jobs._refresh_jellyfin_with_progress(conn, "job-2", "hidden", services)
        self.assertTrue(result)
        self.assertEqual(len(observed), 1)
        sql, params = observed[0][0]
        self.assertIn("state='processing'", sql)
        progress = json.loads(params[0])
        self.assertEqual(progress["local_state"], "hidden")
        self.assertEqual(progress["jellyfin_refresh"], "pending")
        self.assertEqual(progress["undo_snapshots"], [{"video": {"id": "one"}}])
        self.assertEqual(progress["progress"]["stage"], "refreshing_jellyfin")
        self.assertEqual(progress["events"][-1]["stage"], "refreshing_jellyfin")


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


class TestCatalogMetadataOverrides(PgTestCase):
    def setUp(self):
        super().setUp()
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id,provider,title,plot,performers,categories,tags) "
                "VALUES ('telegram:10:override','telegram','Source title','Source plot','[]','[]','[]')"
            )
            from common.video_metadata import update_metadata_state
            update_metadata_state(self.conn, "telegram:10:override", 0,
                                  {"title": "Manual title", "plot": "", "studio": "Manual studio",
                                   "performers": ["Manual actor"], "tags": [],
                                   "date_released": "2026-02-03"})

    def test_catalog_search_missing_filter_and_detail_use_effective_values(self):
        query = {
            "q": "Manual title", "channel_id": "", "studio": "", "performer": "", "tag": "",
            "availability": "all", "missing": ["plot"], "sort": "title", "offset": 0, "limit": 50,
        }
        page = admin_library_v2._run_catalog_query(self.db_url, query)
        self.assertEqual(page["page"]["total"], 1)
        item = page["items"][0]
        self.assertEqual(item["title"], "Manual title")
        self.assertEqual(item["plot"], "")
        self.assertEqual(item["metadata_fields"]["title"], {
            "source": "Source title", "effective": "Manual title", "overridden": True,
        })
        detail = admin_library_v2._get_video(self.db_url, "telegram:10:override")
        self.assertEqual(detail["metadata_revision"], 1)
        self.assertEqual(detail["title"], "Manual title")
        self.assertEqual(detail["plot"], "")
        self.assertEqual(detail["studio"], "Manual studio")
        self.assertEqual(detail["performers"], ["Manual actor"])
        self.assertEqual(detail["tags"], [])
        self.assertEqual(detail["date_released"], "2026-02-03")

    def test_catalog_pages_do_not_repeat_equal_titles_across_database_offsets(self):
        with self.conn:
            self.conn.executemany(
                "INSERT INTO videos (id,provider,title,performers,categories,tags) "
                "VALUES (%s,'telegram','Tie title','[]','[]','[]')",
                [("telegram:page:c",), ("telegram:page:a",), ("telegram:page:b",)],
            )
        query = {
            "q": "Tie title", "channel_id": "", "studio": "", "performer": "", "tag": "",
            "availability": "all", "missing": [], "sort": "title", "offset": 0, "limit": 2,
        }
        first = admin_library_v2._run_catalog_query(self.db_url, query)
        second = admin_library_v2._run_catalog_query(self.db_url, {**query, "offset": 2})
        first_ids = [item["id"] for item in first["items"]]
        second_ids = [item["id"] for item in second["items"]]
        self.assertEqual(first_ids, ["telegram:page:a", "telegram:page:b"])
        self.assertEqual(second_ids, ["telegram:page:c"])
        self.assertTrue(set(first_ids).isdisjoint(second_ids))
        self.assertEqual(first["page"]["total"], 3)


class TestLibraryGeneratePhash(PgTestCase):
    def setUp(self):
        super().setUp()
        self.admin = admin_stub()
        self.admin.HUB_DB_URL = self.db_url
        self.router = admin_library_v2.build_router(self.admin)
        self.endpoint = next(
            r.endpoint for r in self.router.routes
            if getattr(r, "path", None) == "/api/v2/library/videos/{video_id:path}/generate-phash"
        )

    def _post_request(self, body=None):
        raw = json.dumps(body or {}).encode("utf-8")

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        return Request({
            "type": "http", "http_version": "1.1", "method": "POST",
            "scheme": "http", "path": "/admin/api/v2/library/videos/test/generate-phash",
            "raw_path": b"/admin/api/v2/library/videos/test/generate-phash",
            "headers": [(b"content-type", b"application/json")],
            "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
        }, receive)

    def test_enqueues_phash_job_for_valid_telegram_video(self):
        video_id = "telegram:-100123:42:101"
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, duration) VALUES (%s, 'telegram', 'Test Movie', 600)",
                (video_id,),
            )
        req = self._post_request()
        res = asyncio.run(self.endpoint(req, video_id))
        self.assertTrue(res.get("ok"))
        self.assertEqual(res.get("state"), "queued")
        self.assertFalse(res.get("already_computed"))
        job_id = res.get("job_id")
        self.assertTrue(job_id.startswith("p-"))

        row = self.conn.execute("SELECT video_id, duration_us, state FROM video_phash_jobs WHERE id=%s", (job_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], video_id)
        self.assertEqual(row[1], 600_000_000)
        self.assertEqual(row[2], "queued")

    def test_force_regeneration_clears_fingerprint_and_requeues(self):
        video_id = "telegram:-100123:42:102"
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, duration) VALUES (%s, 'telegram', 'Test Movie', 600)",
                (video_id,),
            )
        from resolver.admin_v2_phash import PHASH_ALGORITHM_VERSION, _SERVER_PROFILE
        from common import phash_jobs
        first = phash_jobs.enqueue(
            self.db_url, video_id=video_id, duration_us=600_000_000,
            algorithm_version=PHASH_ALGORITHM_VERSION, decoder_profile=_SERVER_PROFILE,
            user_id="admin",
        )
        self.assertEqual(first["outcome"], "queued")

        with self.conn:
            self.conn.execute(
                "INSERT INTO video_fingerprints (video_id, algorithm, fingerprint) VALUES (%s, 'PHASH', '0123456789abcdef')",
                (video_id,),
            )
            self.conn.execute(
                "INSERT INTO video_phash_provenance (video_id, fingerprint, generator_version, decoder_profile, submitted_by_user_id) "
                "VALUES (%s, '0123456789abcdef', %s, %s, 'admin')",
                (video_id, f"{PHASH_ALGORITHM_VERSION}:{_SERVER_PROFILE}", _SERVER_PROFILE),
            )
            self.conn.execute(
                "INSERT INTO video_phash_thumbnails (video_id, image_bytes) VALUES (%s, %s)",
                (video_id, b"fake-jpg"),
            )
            self.conn.execute("UPDATE video_phash_jobs SET state='done' WHERE id=%s", (first["job"]["id"],))

        res_no_force = asyncio.run(self.endpoint(self._post_request(), video_id))
        self.assertTrue(res_no_force.get("ok"))
        self.assertTrue(res_no_force.get("already_computed"))

        res_force = asyncio.run(self.endpoint(self._post_request({"force": True}), video_id))
        self.assertTrue(res_force.get("ok"))
        self.assertFalse(res_force.get("already_computed"))
        self.assertEqual(res_force.get("state"), "queued")

        fp = self.conn.execute("SELECT fingerprint FROM video_fingerprints WHERE video_id=%s AND algorithm='PHASH'", (video_id,)).fetchone()
        self.assertIsNone(fp)

    def test_fails_when_video_not_found(self):
        req = self._post_request()
        res = asyncio.run(self.endpoint(req, "telegram:-100123:42:999"))
        self.assertEqual(res.status_code, 404)

    def test_fails_when_duration_is_missing(self):
        video_id = "telegram:-100123:42:103"
        with self.conn:
            # Model a legacy row created before migration 0044 installed its
            # insert guard. New inserts with no duration are now rejected.
            self.conn.execute("ALTER TABLE videos DISABLE TRIGGER videos_minimum_duration_insert")
            self.conn.execute(
                "INSERT INTO videos (id, provider, title, duration) VALUES (%s, 'telegram', 'No Duration', NULL)",
                (video_id,),
            )
            self.conn.execute("ALTER TABLE videos ENABLE TRIGGER videos_minimum_duration_insert")
        req = self._post_request()
        res = asyncio.run(self.endpoint(req, video_id))
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
