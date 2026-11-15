"""Request-level coverage for the resolver admin, health and metrics HTTP surface."""

import json
import types
import unittest

from fastapi import HTTPException
from fastapi.responses import Response

try:
    from tests.test_resolver_http import (
        ASSET,
        Provider,
        ResolverHarness,
        validation_error,
    )
except ImportError:
    from test_resolver_http import (
        ASSET,
        Provider,
        ResolverHarness,
        validation_error,
    )


class TestAdminSurface(ResolverHarness):
    def test_admin_assets_are_never_cached(self):
        module = self.load()

        response = self.get(module, f"/admin/assets-v2/{ASSET}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_a_v2_api_answer_is_marked_uncached(self):
        module = self.load()

        response = self.get(module, "/admin/api/v2/not-here")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_a_v1_answer_keeps_its_own_caching(self):
        module = self.load()
        self.use_cache(module)

        response = self.get(module, "/admin/health")

        self.assertNotIn("no-store", response.headers.get("cache-control", ""))

    def test_a_v2_parameter_error_uses_the_v2_envelope(self):
        module = self.load()
        request = types.SimpleNamespace(url=types.SimpleNamespace(path="/admin/api/v2/videos"))

        response = self.complete(module.handle_request_validation_error(request, validation_error()))

        self.assertEqual(response.status_code, 400)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["message"], "Invalid request parameters.")
        self.assertFalse(body["ok"])

    def test_a_v1_parameter_error_keeps_the_standard_answer(self):
        module = self.load()
        request = types.SimpleNamespace(url=types.SimpleNamespace(path="/admin/play/x/stream"))

        response = self.complete(module.handle_request_validation_error(request, validation_error()))

        self.assertEqual(response.status_code, 422)

    def test_an_unknown_playback_mode_is_rejected_over_http(self):
        module = self.load()

        response = self.get(module, "/admin/play/telegram:1/stream?mode=tee")

        self.assertEqual(response.status_code, 422)

    def test_every_request_is_recorded_for_prometheus(self):
        module = self.load()
        metrics = self.use_metrics(module)
        self.use_cache(module)

        self.get(module, "/admin/health")
        self.get(module, "/not-a-route")

        self.assertEqual(metrics.recorded, [("GET", "/admin/health", 200, False),
                                            ("GET", "unmatched", 404, False)])

    def test_a_playback_request_is_recorded_as_a_stream(self):
        module = self.load()
        metrics = self.use_metrics(module)
        self.use_cache(module)
        self.use_providers(module, telegram=Provider(stream="tg://-1001:42"))
        self.single_candidate(module)

        async def proxy(_request, url, cache_key=None):
            return Response(content="streamed", media_type="video/mp4")

        module.proxy_fetch_response = proxy
        self.get(module, "/admin/play/telegram:-1001:42/stream")

        self.assertEqual(metrics.recorded,
                         [("GET", "/admin/play/{video_id:path}/stream", 200, True)])

    def test_a_failing_route_is_recorded_as_a_server_error(self):
        module = self.load()
        metrics = self.use_metrics(module)

        def explode():
            raise ZeroDivisionError("no health for you")

        module._provider_health = explode
        response = self.client(module, raise_server_exceptions=False).get("/admin/health")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(metrics.recorded, [("GET", "/admin/health", 500, False)])

    def test_the_admin_hooks_report_resolver_state(self):
        module = self.load()
        cache = self.use_cache(module, entries={"telegram:1": "tg://x"}, length=4)

        self.assertEqual(module._admin_extra()["cache_entries"], 4)
        self.assertEqual(module._admin_cache_clear(), 1)
        self.assertEqual(cache.closes, 1)


class TestHealthEndpoints(ResolverHarness):
    def test_health_describes_the_running_resolver(self):
        module = self.load()
        self.use_cache(module, length=2)

        response = self.get(module, "/admin/health")

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["cache_entries"], 2)
        self.assertIn("telegram", body["providers"])

    def test_liveness_never_opens_the_database(self):
        module = self.load()
        cache_database = self.use_persistent_cache(module)
        self.use_database(module, error=RuntimeError("database is down"))

        response = self.get(module, "/admin/health/live")

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(cache_database.urls, [])
        self.assertNotIn("cache_entries", body)

    def test_the_persisted_cache_size_is_still_reported_elsewhere(self):
        module = self.load()
        cache_database = self.use_persistent_cache(module, entries=7)
        self.use_database(module, answers=[("SELECT 1", [(1,)])])
        self.use_admin(module)

        health = self.get(module, "/admin/health").json()
        ready = self.get(module, "/admin/health/ready").json()

        self.assertEqual(health["cache_entries"], 7)
        self.assertEqual(ready["cache_entries"], 7)
        self.assertEqual(ready["database"], "ok")
        # H-1: len(cache) is now cached for 30 s, so only the first health call
        # queries the database; the second hits the in-memory counter.
        self.assertEqual(len(cache_database.urls), 1)

    def test_readiness_checks_the_shared_catalog(self):
        module = self.load()
        self.use_cache(module)
        database = self.use_database(module, answers=[("SELECT 1", [(1,)])])
        admin = self.use_admin(module)

        response = self.get(module, "/admin/health/ready")

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["database"], "ok")
        self.assertEqual(database.urls, [admin.HUB_DB_URL])
        self.assertEqual(database.connections[0].statements[0][0], "SELECT 1")
        self.assertTrue(database.connections[0].closed)

    def test_readiness_stops_when_the_catalog_is_unreachable(self):
        module = self.load()
        self.use_cache(module)
        self.use_database(module, error=RuntimeError("connection refused"))
        self.use_admin(module)

        response = self.get(module, "/admin/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "not_ready", "database": "unavailable"})


class TestMetricsEndpoint(ResolverHarness):
    def test_metrics_are_hidden_from_a_blocked_client(self):
        module = self.load()
        self.use_admin(module, blocked=True)

        response = self.get(module, "/metrics")

        self.assertEqual(response.status_code, 404)

    def test_metrics_carry_the_sync_and_job_gauges(self):
        module = self.load()
        self.use_metrics(module)
        self.use_admin(module, sync_status={"running": True, "last_success": False,
                                            "updated_at": 1700000000.5},
                       history=[{"duration_seconds": 12.5}])
        database = self.use_database(module, answers=[(
            "FROM video_duplicate_jobs",
            [("metadata_update", "done", 3), ("hide", "queued", "2"),
             ("not_an_action", "done", 5), ("sync", "exploded", 1)])])
        admin = module.admin_module
        response = self.client(module).get("/metrics")

        self.assertEqual(response.headers["content-type"], "text/plain; version=0.0.4; charset=utf-8")
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("# HELP adult_hub_http requests", body)
        self.assertIn("adult_hub_sync_running 1", body)
        self.assertIn("adult_hub_sync_last_success 0", body)
        self.assertIn("adult_hub_sync_status_updated_timestamp_seconds 1700000000.500", body)
        self.assertIn("adult_hub_sync_last_duration_seconds 12.500", body)
        self.assertIn('adult_hub_jobs{action="metadata_update",state="done"} 3', body)
        self.assertIn('adult_hub_jobs{action="hide",state="queued"} 2', body)
        self.assertNotIn("not_an_action", body)
        self.assertNotIn('action="sync",state="exploded"', body)
        self.assertIn("adult_hub_jobs_database_available 1", body)
        self.assertEqual(admin.history_limits, [1])
        self.assertEqual(database.urls, [admin.HUB_DB_URL])

    def test_optional_sync_gauges_appear_only_when_meaningful(self):
        module = self.load()
        self.use_metrics(module)
        self.use_admin(module, sync_status={"running": False, "last_success": None,
                                            "updated_at": "never"},
                       history_error=RuntimeError("no history"))
        self.use_database(module, answers=[("FROM video_duplicate_jobs", [])])

        body = self.get(module, "/metrics").text

        self.assertIn("adult_hub_sync_running 0", body)
        self.assertNotIn("adult_hub_sync_last_success", body)
        self.assertNotIn("adult_hub_sync_status_updated_timestamp_seconds", body)
        self.assertNotIn("adult_hub_sync_last_duration_seconds", body)
        self.assertIn("adult_hub_jobs_database_available 1", body)

    def test_a_run_without_a_duration_has_no_duration_gauge(self):
        module = self.load()
        self.use_metrics(module)
        self.use_admin(module, sync_status={"running": True}, history=[{"state": "failed"}])
        self.use_database(module, answers=[("FROM video_duplicate_jobs", [])])

        body = self.get(module, "/metrics").text

        self.assertNotIn("adult_hub_sync_last_duration_seconds", body)
        self.assertNotIn("adult_hub_sync_last_success", body)
        self.assertNotIn("adult_hub_sync_status_updated_timestamp_seconds", body)

    def test_a_broken_job_table_is_reported_instead_of_raising(self):
        module = self.load()
        self.use_metrics(module)
        self.use_admin(module)
        self.use_database(module, error=RuntimeError("jobs table is gone"))

        body = self.get(module, "/metrics").text

        self.assertIn("adult_hub_jobs_database_available 0", body)
        self.assertNotIn("adult_hub_jobs{", body)


class TestPerformerBio(ResolverHarness):
    def performer(self, module, name="Bunny"):
        return self.get(module, f"/admin/api/performer-bio?name={name}")

    def test_a_blank_name_has_no_bio(self):
        module = self.load()

        response = self.performer(module, name="")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Missing performer name")

    def test_a_stored_performer_answers_with_every_source(self):
        module = self.load()
        database = self.use_database(
            module,
            tables={"performer_bios", "performer_aliases", "performer_photos"},
            columns={"performer_bios": ["name", "bio", "birth", "aliases", "updated_at",
                                        "nationality"]},
            answers=[
                ("SELECT canonical FROM performer_aliases", []),
                ("SELECT bio, birth, aliases",
                 [("A long bio", "1990-01-01", '["Old"]', 1700000000, "PL")]),
                ("SELECT alias FROM performer_aliases", [("Bunny Rae",)]),
                ("SELECT thumb FROM performer_photos", [("https://photos.example/bunny.jpg",)]),
            ])

        bio = self.performer(module).json()

        self.assertEqual(bio["name"], "Bunny")
        self.assertEqual(bio["bio"], "A long bio")
        self.assertEqual(bio["birth"], "1990-01-01")
        self.assertEqual(bio["nationality"], "PL")
        self.assertEqual(bio["aliases"], ["Bunny Rae", "Old"])
        self.assertEqual(bio["photo_url"], "https://photos.example/bunny.jpg")
        self.assertEqual(bio["updated_at"], 1700000000)
        self.assertTrue(all(connection.closed for connection in database.connections))

    def test_an_alias_resolves_to_the_canonical_record(self):
        module = self.load()
        self.use_database(
            module,
            tables={"performer_bios", "performer_aliases"},
            answers=[
                ("SELECT canonical FROM performer_aliases", [("Bunny Rae",)]),
                ("SELECT alias FROM performer_aliases", []),
                ("SELECT bio, birth, aliases", [("Canonised", "", None, None, "")]),
            ])

        bio = self.performer(module, name="bunny").json()

        self.assertEqual(bio["name"], "Bunny Rae")
        self.assertEqual(bio["nationality"], "")
        self.assertEqual(bio["aliases"], [])
        self.assertEqual(bio["photo_url"], "")
        self.assertEqual(bio["updated_at"], 0)

    def test_unreadable_stored_aliases_are_ignored(self):
        module = self.load()
        self.use_database(
            module,
            tables={"performer_bios"},
            answers=[("SELECT bio, birth, aliases", [("bio", "1990", "not json", 1, "PL")])])

        self.assertEqual(self.performer(module).json()["aliases"], [])

    def test_a_number_where_the_alias_list_should_be_is_ignored(self):
        module = self.load()
        self.use_database(
            module,
            tables={"performer_bios"},
            answers=[("SELECT bio, birth, aliases", [("bio", "", "42", 1, "")])])

        self.assertEqual(self.performer(module).json()["aliases"], [])

    def test_a_photo_without_a_bio_row_still_answers(self):
        module = self.load()
        self.use_database(
            module,
            tables={"performer_photos"},
            answers=[("SELECT thumb FROM performer_photos", [("https://photos.example/x.jpg",)])])

        bio = self.performer(module).json()

        self.assertEqual(bio["bio"], "")
        self.assertEqual(bio["photo_url"], "https://photos.example/x.jpg")

    def test_a_performer_without_any_record_is_not_found(self):
        module = self.load()
        self.use_database(module, tables={"performer_photos"},
                          answers=[("SELECT thumb FROM performer_photos", [("",)])])

        response = self.performer(module, name="Nobody")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "No info for performer 'Nobody'")

    def test_a_refused_lookup_stays_the_refusal_it_was(self):
        module = self.load()
        self.use_database(module, error=HTTPException(status_code=404, detail="catalog is gone"))

        response = self.performer(module)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "catalog is gone")

    def test_a_broken_catalog_is_a_bad_gateway(self):
        module = self.load()
        self.use_database(module, error=RuntimeError("password=supersecret host=db.internal"))

        response = self.performer(module)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "HUB DB lookup failed.")
        self.assertNotIn("supersecret", response.text)
        self.assertNotIn("db.internal", response.text)

    def test_admin_profile_data_is_hidden_from_a_blocked_client(self):
        module = self.load()
        self.use_admin(module, blocked=True)
        database = self.use_database(module)

        response = self.performer(module)

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Bunny", response.text)
        self.assertEqual(database.urls, [])


if __name__ == "__main__":
    unittest.main()
