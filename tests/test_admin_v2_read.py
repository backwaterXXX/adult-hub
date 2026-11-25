"""Focused contracts for Admin v2 catalog and issue-center reads."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fastapi.responses import JSONResponse
from starlette.requests import Request
from tests.pg_helper import PgTestCase

import resolver.admin_library_v2 as admin_library_v2


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
            for suffix in (".strm", ".jpg"):
                (root / f"stable{suffix}").write_text("x", encoding="utf-8")
            self.assertEqual(admin_library_v2._local_file_status(["stable.strm"], root),
                             {"stream": "present", "artwork": "present"})
            (root / "stable.jpg").unlink()
            self.assertEqual(admin_library_v2._local_file_status(["stable.strm"], root),
                             {"stream": "present", "artwork": "missing"})
            (root / "stable.strm").unlink()
            (root / "stable.strm").symlink_to(root / "outside.strm")
            self.assertEqual(admin_library_v2._local_file_status(["stable.strm"], root)["stream"], "unknown")
            self.assertEqual(admin_library_v2._local_file_status(["../outside.strm"], root),
                             {"stream": "unknown", "artwork": "unknown"})
            self.assertEqual(admin_library_v2._local_file_status(["stable.strm"], root / "missing"),
                             {"stream": "unknown", "artwork": "unknown"})
            with patch.object(admin_library_v2.os, "scandir", side_effect=PermissionError):
                self.assertEqual(admin_library_v2._local_file_status(["stable.strm"], root),
                                 {"stream": "unknown", "artwork": "unknown"})

            # If artwork file is a black image, report artwork as missing
            from PIL import Image
            black_img = Image.new("RGB", (64, 64), color=(0, 0, 0))
            black_img.save(root / "stable.jpg", format="JPEG")
            (root / "stable.strm").unlink()
            (root / "stable.strm").write_text("x", encoding="utf-8")
            self.assertEqual(admin_library_v2._local_file_status(["stable.strm"], root),
                             {"stream": "present", "artwork": "missing"})

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

    def test_duration_bounds_reach_the_query_and_reject_bad_ranges(self):
        admin = admin_stub()
        endpoint = next(
            r.endpoint for r in admin_library_v2.build_router(admin).routes
            if r.endpoint.__name__ == "admin_v2_library_videos"
        )
        with patch.object(admin_library_v2, "_run_catalog_query",
                          return_value={"ok": True, "items": []}) as run:
            asyncio.run(endpoint(request(), min_duration="300", max_duration="900"))
        query = run.call_args.args[1]
        self.assertEqual((query["min_duration"], query["max_duration"]), (300, 900))

        for kwargs in ({"min_duration": "-1"}, {"max_duration": "abc"},
                       {"min_duration": "900", "max_duration": "300"}):
            with self.subTest(kwargs=kwargs):
                result = asyncio.run(endpoint(request(), **kwargs))
                self.assertEqual(result.status_code, 400)

    def test_serializer_marks_parked_artwork(self):
        video = admin_library_v2._serialize_video({
            "id": "telegram:-100123:42", "provider": "telegram", "title": "Example",
        }, detail=False, artwork_parked=True)
        self.assertTrue(video["artwork_parked"])

        default = admin_library_v2._serialize_video({"id": "telegram:-100123:43"}, detail=False)
        self.assertFalse(default["artwork_parked"])

    def test_duration_bounds_narrow_the_catalog_sql(self):
        class Result:
            def fetchone(self):
                return {"total": 0}

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
        conn = Connection()
        with patch.object(admin_library_v2.db, "connect", return_value=conn), \
             patch.object(admin_library_v2.db, "table_names", return_value={"videos"}), \
             patch.object(admin_library_v2.db, "table_columns", return_value=columns):
            admin_library_v2._run_catalog_query("unused", {
                "q": "", "channel_id": "", "studio": "", "performer": "", "tag": "",
                "availability": "all", "missing": [], "sort": "last_seen", "offset": 0, "limit": 50,
                "min_duration": 300, "max_duration": 900,
            })
        count_sql, count_params = conn.queries[0]
        self.assertIn("v.duration >= %s", count_sql)
        self.assertIn("v.duration <= %s", count_sql)
        self.assertEqual(count_params, [300, 900])

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
        self.assertEqual(video["local_files"], {"stream": "unknown", "artwork": "unknown"})
        self.assertNotIn("file_path", video)



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
