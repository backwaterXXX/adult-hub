"""Request-level coverage for the resolver stream playback and candidate resolution."""

import unittest

from fastapi import HTTPException
from fastapi.responses import Response

try:
    from tests.test_resolver_http import (
        FakeResponse,
        Provider,
        RecordingLiveness,
        ResolverHarness,
    )
except ImportError:
    from test_resolver_http import (
        FakeResponse,
        Provider,
        RecordingLiveness,
        ResolverHarness,
    )


class TestStreamCandidates(ResolverHarness):
    def test_a_lone_video_is_played_from_itself(self):
        module = self.load()
        self.use_database(module)

        self.assertEqual(module._get_stream_candidate_ids("telegram:-100:1"), ["telegram:-100:1"])

    def test_the_source_table_lists_the_active_sources_in_order(self):
        module = self.load()
        self.use_database(module, tables={"video_sources"}, answers=[
            ("WHERE video_id=%s AND is_active=1",
             [("telegram:-100:1",), ("youtube:abc",), ("telegram:-100:1",), ("",)]),
        ])

        candidates = module._get_stream_candidate_ids("telegram:-100:1")

        self.assertEqual(candidates, ["telegram:-100:1", "youtube:abc"])

    def test_a_source_id_redirects_to_the_canonical_video(self):
        module = self.load()
        database = self.use_database(module, tables={"video_sources", "video_duplicate_decisions"},
                                     answers=[
                ("SELECT video_id FROM video_sources WHERE source_id", [("telegram:-100:9",)]),
                ("WHERE video_id=%s AND is_active=1",
                 [("telegram:-100:9",), ("telegram:-100:8",)]),
            ])

        candidates = module._get_stream_candidate_ids("telegram:-100:1:42")

        self.assertEqual(candidates, ["telegram:-100:9", "telegram:-100:8", "telegram:-100:1:42"])
        self.assertEqual(database.connections[0].statements[1][1], ("telegram:-100:9",))

    def test_a_duplicate_decision_names_the_keeper(self):
        module = self.load()
        self.use_database(module, tables={"video_duplicate_decisions"}, answers=[
            ("SELECT keeper_id FROM video_duplicate_decisions", [("telegram:-100:9",)]),
        ])

        self.assertEqual(module._get_stream_candidate_ids("telegram:-100:1"),
                         ["telegram:-100:9", "telegram:-100:1"])

    def test_an_empty_keeper_row_leaves_the_requested_id(self):
        module = self.load()
        self.use_database(module, tables={"video_duplicate_decisions"}, answers=[
            ("SELECT keeper_id FROM video_duplicate_decisions", [("",)]),
        ])

        self.assertEqual(module._get_stream_candidate_ids("telegram:-100:1"), ["telegram:-100:1"])

    def test_a_source_alias_skips_the_duplicate_ledger(self):
        module = self.load()
        database = self.use_database(module, tables={"video_sources", "video_duplicate_decisions"},
                                     answers=[
                ("SELECT video_id FROM video_sources WHERE source_id", [("telegram:-100:9",)]),
            ])

        module._get_stream_candidate_ids("telegram:-100:1")

        statements = [statement for statement, _ in database.connections[0].statements]
        self.assertEqual(statements[0],
                         "SELECT video_id FROM video_sources "
                         "WHERE source_id=%s AND video_id!=%s AND is_active=1")
        self.assertNotIn("video_duplicate_decisions", "\n".join(statements))

    def test_a_broken_catalog_leaves_the_requested_id(self):
        module = self.load()
        self.use_database(module, error=RuntimeError("catalog is down"))

        self.assertEqual(module._get_stream_candidate_ids("telegram:-100:1"), ["telegram:-100:1"])
        self.assertIn("Source candidate lookup error", self.logs.blob("DEBUG"))


class TestPlayIdParsing(ResolverHarness):
    def test_a_bare_id_is_a_telegram_id(self):
        module = self.load()

        self.assertEqual(module.parse_composite_id(""), ("telegram", ""))
        self.assertEqual(module.parse_composite_id(" plain_id "), ("telegram", "plain_id"))
        self.assertEqual(module.parse_composite_id("telegram:ch:42:uniq"),
                         ("telegram", "ch:42:uniq"))

    def test_an_unknown_prefix_is_a_bad_request(self):
        module = self.load()
        module.list_providers = lambda: {"telegram": None}

        with self.assertRaises(HTTPException) as raised:
            module._parse_play_id_or_400("bogus:1")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Unknown provider prefix 'bogus'", raised.exception.detail)


class TestPlayStream(ResolverHarness):
    def setUp(self):
        super().setUp()
        self.proxied = []

    def record_proxy(self, module, body="streamed"):
        async def proxy(request, url, cache_key=None):
            self.proxied.append((url, cache_key))
            return Response(content=body, media_type="video/mp4")

        module.proxy_fetch_response = proxy

    def test_a_telegram_reference_is_handed_to_the_range_proxy(self):
        module = self.load()
        provider = Provider(stream="tg://-1001:42")
        self.use_providers(module, telegram=provider)
        self.use_cache(module)
        self.single_candidate(module)
        self.record_proxy(module)

        response = self.get(module, "/admin/play/telegram:-1001:42/stream")

        self.assertEqual(response.content, b"streamed")
        self.assertEqual(self.proxied, [("tg://-1001:42", "telegram:-1001:42")])
        self.assertEqual(provider.authorisation_checks, ["-1001"])
        self.assertEqual(module._stream_semaphore.high_water, 1)

    def test_an_unauthorized_channel_is_forbidden(self):
        module = self.load()
        self.use_providers(module, telegram=Provider(stream="tg://x", authorized=False))
        self.use_cache(module)
        self.single_candidate(module)

        response = self.get(module, "/admin/play/telegram:-1001:42/stream")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Channel not authorized")

    def test_a_missing_telegram_provider_leaves_no_working_source(self):
        module = self.load()

        def missing(name):
            raise KeyError(name)

        module.get_provider = missing
        module.list_providers = lambda: {}
        self.use_cache(module)
        self.single_candidate(module)

        response = self.get(module, "/admin/play/plain-id/stream")

        self.assertEqual(response.status_code, 502)
        self.assertIn("Failed to resolve stream", response.json()["detail"])

    def test_a_cached_direct_stream_redirects_without_resolving(self):
        module = self.load()
        provider = Provider(stream="https://never.example/x")
        self.use_providers(module, youtube=provider)
        cache = self.use_cache(module, entries={"youtube:abc": ("https://cached.example/x", 0, 0)})
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(True)

        response = self.get(module, "/admin/play/youtube:abc/stream", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://cached.example/x")
        self.assertEqual(module.upstream_url_alive.checked, ["https://cached.example/x"])
        self.assertEqual(cache.reads, ["youtube:abc"])
        self.assertEqual(provider.resolved, [])

    def test_a_cached_telegram_reference_skips_the_liveness_probe(self):
        module = self.load()
        provider = Provider(stream="tg://x")
        self.use_providers(module, telegram=provider)
        self.use_cache(module)
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(False)
        self.record_proxy(module)

        response = self.get(module, "/admin/play/telegram:ch:1/stream")

        self.assertEqual(response.content, b"streamed")
        self.assertEqual(provider.resolved, ["ch:1"])
        self.assertEqual(module.upstream_url_alive.checked, [])
        self.assertEqual(provider.authorisation_checks, ["ch"])
        self.assertIn("Resolved new telegram stream", self.logs.blob("INFO"))

    def test_a_cached_telegram_reference_is_played_without_a_probe(self):
        module = self.load()
        provider = Provider(stream="https://never.example/x")
        self.use_providers(module, youtube=provider)
        self.use_cache(module, entries={"youtube:abc": "tg://-1001:9"})
        self.single_candidate(module)
        liveness = RecordingLiveness(False)
        module.upstream_url_alive = liveness

        async def proxy(_request, url, cache_key=None):
            self.proxied.append((url, cache_key))
            return Response(content="streamed", media_type="video/mp4")

        module.proxy_fetch_response = proxy
        response = self.get(module, "/admin/play/youtube:abc/stream")

        self.assertEqual(response.content, b"streamed")
        self.assertEqual(self.proxied, [("tg://-1001:9", "youtube:abc")])
        self.assertEqual(liveness.checked, [])
        self.assertEqual(provider.resolved, [])

    def test_a_dead_cached_stream_is_dropped_and_re_resolved(self):
        module = self.load()
        provider = Provider(stream="https://fresh.example/x")
        self.use_providers(module, youtube=provider)
        cache = self.use_cache(module, entries={"youtube:abc": ("https://dead.example/x", 0, 0)})
        self.single_candidate(module)
        liveness = RecordingLiveness(False, True)
        module.upstream_url_alive = liveness

        response = self.get(module, "/admin/play/youtube:abc/stream", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://fresh.example/x")
        self.assertEqual(liveness.checked, ["https://dead.example/x", "https://fresh.example/x"])
        self.assertEqual(cache.invalidated, ["youtube:abc"])
        self.assertEqual(cache.writes, [("youtube:abc", "https://fresh.example/x",
                                         module.cache_ttl_for_provider("youtube"))])
        self.assertIn("Cached stream for youtube:abc is dead", self.logs.blob("INFO"))

    def test_an_unanswered_liveness_probe_keeps_the_cached_stream(self):
        module = self.load()
        provider = Provider(stream="https://never.example/x")
        self.use_providers(module, youtube=provider)
        self.use_cache(module, entries={"youtube:abc": ("https://maybe.example/x", 0, 0)})
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(None)

        response = self.get(module, "/admin/play/youtube:abc/stream", follow_redirects=False)

        self.assertEqual(response.headers["location"], "https://maybe.example/x")
        self.assertEqual(provider.resolved, [])

    def test_a_second_look_inside_the_lock_can_find_the_stream(self):
        module = self.load()
        provider = Provider(stream="https://never.example/x")
        self.use_providers(module, youtube=provider)
        cache = self.use_cache(module, gets=[None, "https://raced.example/x"])
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(True)

        response = self.get(module, "/admin/play/youtube:abc/stream", follow_redirects=False)

        self.assertEqual(response.headers["location"], "https://raced.example/x")
        self.assertEqual(cache.reads, ["youtube:abc", "youtube:abc"])
        self.assertEqual(provider.resolved, [])

    def test_a_refused_resolution_is_retried_on_the_next_source(self):
        module = self.load()
        broken = Provider(error=TimeoutError("telegram timed out"))
        working = Provider(stream="https://backup.example/x")
        self.use_providers(module, telegram=broken, youtube=working)
        self.use_cache(module)
        module._get_stream_candidate_ids = lambda video_id: ["telegram:1", "youtube:abc"]
        module.upstream_url_alive = RecordingLiveness(True)

        response = self.get(module, "/admin/play/telegram:1/stream", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://backup.example/x")
        self.assertEqual(broken.resolved, ["1"])
        self.assertIn("Multi-source failover active", self.logs.blob("INFO"))
        self.assertIn("Failed to resolve telegram:1", self.logs.blob("WARNING"))

    def test_an_empty_resolution_is_a_failure_not_a_redirect(self):
        module = self.load()
        self.use_providers(module, telegram=Provider(stream=None))
        self.use_cache(module)
        self.single_candidate(module)

        response = self.get(module, "/admin/play/telegram:1/stream")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Failed to resolve stream")
        self.assertIn("Empty stream URL resolved for telegram:1", self.logs.blob("WARNING"))

    def test_a_freshly_resolved_dead_url_is_refused(self):
        module = self.load()
        self.use_providers(module, telegram=Provider(stream="https://gone.example/x"))
        self.use_cache(module)
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(False)

        response = self.get(module, "/admin/play/telegram:1/stream")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Failed to resolve stream")
        self.assertIn("Freshly resolved URL is dead", self.logs.blob("WARNING"))

    def test_an_unparsable_candidate_id_is_skipped(self):
        module = self.load()
        provider = Provider(stream="https://second.example/x")
        self.use_providers(module, telegram=provider)
        self.use_cache(module)
        module._get_stream_candidate_ids = lambda video_id: ["bogus:1", "plain"]
        module.upstream_url_alive = RecordingLiveness(True)

        response = self.get(module, "/admin/play/plain/stream", follow_redirects=False)

        self.assertEqual(response.headers["location"], "https://second.example/x")

    def test_no_candidates_at_all_end_in_a_bad_gateway(self):
        module = self.load()
        self.use_providers(module)
        self.use_cache(module)
        module._get_stream_candidate_ids = lambda video_id: []

        response = self.get(module, "/admin/play/telegram:1/stream")

        self.assertEqual(response.status_code, 502)
        self.assertIn("Failed to resolve stream for telegram:1", self.logs.blob("ERROR"))

    def test_proxy_mode_fetches_the_resolved_url_through_the_app(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(200, {"Content-Type": "video/mp4"}, chunks=[b"video"]))
        provider = Provider(stream="https://cdn.example.test/x.mp4")
        self.use_providers(module, telegram=provider)
        self.use_cache(module)
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(True)

        response = self.get(module, "/admin/play/telegram:1/stream?mode=proxy")

        self.assertEqual(response.content, b"video")
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertEqual(module.upstream.urls(), ["https://cdn.example.test/x.mp4"])

    def test_proxy_mode_refuses_an_unvalidated_resolution(self):
        module = self.load()
        provider = Provider(stream="https://localhost/x.mp4")
        self.use_providers(module, telegram=provider)
        self.use_cache(module)
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(True)

        response = self.get(module, "/admin/play/telegram:1/stream?mode=proxy")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Local stream URLs are not allowed")

    def test_a_stream_head_request_probes_the_resolution_path(self):
        module = self.load()
        provider = Provider(stream="tg://-1001:42")
        self.use_providers(module, telegram=provider)
        self.use_cache(module)
        self.single_candidate(module)
        self.record_proxy(module)

        response = self.head(module, "/admin/play/telegram:-1001:42/stream")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider.resolved, ["-1001:42"])


if __name__ == "__main__":
    unittest.main()
