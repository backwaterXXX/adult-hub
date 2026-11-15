"""Request-level coverage for the resolver stream proxying and URL validation."""

import asyncio
import types
import unittest

from fastapi import HTTPException
from fastapi.responses import Response
import httpx

try:
    from tests.test_resolver_http import (
        FakeResponse,
        FakeSocket,
        Headers,
        PRIVATE_IP,
        ResolverHarness,
        _drain,
    )
except ImportError:
    from test_resolver_http import (
        FakeResponse,
        FakeSocket,
        Headers,
        PRIVATE_IP,
        ResolverHarness,
        _drain,
    )


class TestStreamUrlValidation(ResolverHarness):
    def test_a_public_https_address_is_accepted(self):
        module = self.load()

        module.validate_proxy_url("https://CDN.Example.test:8443/video.mp4")

        self.assertEqual(module.socket.lookups[-1], ("cdn.example.test", 8443))

    def test_the_default_port_follows_the_scheme(self):
        module = self.load()

        module.validate_proxy_url("https://cdn.example.test/video.mp4")
        module.validate_proxy_url("http://cdn.example.test/video.mp4")

        self.assertEqual([port for _host, port in module.socket.lookups], [443, 80])

    def test_the_host_is_normalised_before_the_lookup(self):
        module = self.load()

        module.validate_proxy_url("https://cdn.example.test./video.mp4")

        self.assertEqual(module.socket.lookups[-1][0], "cdn.example.test")

    def test_a_local_name_is_refused_without_asking_dns(self):
        module = self.load()

        for refused in ("https://localhost/video.mp4", "https://api.localhost/x",
                        "https://localhost.localdomain/x"):
            with self.subTest(refused=refused):
                with self.assertRaises(HTTPException) as raised:
                    module.validate_proxy_url(refused)
                self.assertEqual(raised.exception.status_code, 400)
                self.assertEqual(raised.exception.detail, "Local stream URLs are not allowed")
            self.assertEqual(module.socket.lookups, [])

    def test_a_non_http_scheme_or_host_is_invalid(self):
        module = self.load()

        for refused in ("", "ftp://cdn.example.test/x", "https:///x",
                        "https://user:pass@cdn.example.test/x"):
            with self.subTest(refused=refused):
                with self.assertRaises(HTTPException) as raised:
                    module.validate_proxy_url(refused)
                self.assertEqual(raised.exception.detail, "Invalid stream URL")

    def test_a_private_or_reserved_address_is_refused(self):
        module = self.load()

        for addresses in ([PRIVATE_IP], ["127.0.0.1"], ["::1"], ["169.254.169.254"], []):
            with self.subTest(addresses=addresses):
                module.socket = FakeSocket(addresses=addresses)
                with self.assertRaises(HTTPException) as raised:
                    module.validate_proxy_url("https://cdn.example.test/x")
                self.assertEqual(raised.exception.detail,
                                 "Non-public stream addresses are not allowed")

    def test_a_link_local_scope_suffix_still_counts_as_private(self):
        module = self.load()
        module.socket = FakeSocket(addresses=["fe80::1%eth0"])

        with self.assertRaises(HTTPException) as raised:
            module.validate_proxy_url("https://cdn.example.test/x")

        self.assertEqual(raised.exception.detail, "Non-public stream addresses are not allowed")

    def test_an_unresolvable_host_is_reported_as_unvalidated(self):
        module = self.load()
        module.socket = FakeSocket(error=OSError("name or service not known"))

        with self.assertRaises(HTTPException) as raised:
            module.validate_proxy_url("https://gone.example/x")

        self.assertEqual(raised.exception.detail, "Could not validate stream host")

    def test_a_nonsense_address_is_reported_as_unvalidated(self):
        module = self.load()
        module.socket = FakeSocket(addresses=["not-an-ip"])

        with self.assertRaises(HTTPException) as raised:
            module.validate_proxy_url("https://cdn.example.test/x")

        self.assertEqual(raised.exception.detail, "Could not validate stream host")

    def test_the_check_runs_off_the_event_loop(self):
        module = self.load()
        module.socket = FakeSocket(addresses=[])

        with self.assertRaises(HTTPException):
            self.complete(module._validate_proxy_url_async("https://cdn.example.test/x"))

        self.assertEqual(module.socket.lookups, [("cdn.example.test", 443)])


class TestSafeProxyStream(ResolverHarness):
    def test_a_plain_answer_is_handed_back_unclosed(self):
        module = self.load()
        upstream = self.use_upstream(module, FakeResponse(200, {"content-type": "video/mp4"}))
        client = self.complete(module.get_shared_client())

        context, response = self.complete(module._safe_proxy_stream(
            client, "https://cdn.example.test/v.mp4", headers={}, timeout=None))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(context.exits, 0)
        self.assertEqual(upstream.kwargs()["follow_redirects"], False)
        self.assertEqual(upstream.method(), "GET")

    def test_a_redirect_target_is_validated_before_it_is_followed(self):
        module = self.load()
        upstream = self.use_upstream(
            module,
            FakeResponse(302, {"location": "/next.mp4"}),
            FakeResponse(200, {"content-type": "video/mp4"}))
        client = self.complete(module.get_shared_client())

        _context, response = self.complete(module._safe_proxy_stream(
            client, "https://cdn.example.test/v.mp4", headers={}, timeout=None))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.urls(), ["https://cdn.example.test/v.mp4",
                                           "https://cdn.example.test/next.mp4"])
        self.assertEqual(upstream.contexts[0].exits, 1)
        self.assertEqual(module.socket.lookups, [("cdn.example.test", 443)] * 2)

    def test_a_redirect_without_a_target_is_refused(self):
        module = self.load()
        upstream = self.use_upstream(module, FakeResponse(301, {}))
        client = self.complete(module.get_shared_client())

        with self.assertRaises(HTTPException) as raised:
            self.complete(module._safe_proxy_stream(client, "https://cdn.example.test/v.mp4",
                                               headers={}, timeout=None))

        self.assertEqual(raised.exception.detail, "Invalid upstream redirect")
        self.assertEqual(upstream.contexts[0].exits, 1)

    def test_a_redirect_loop_is_cut_after_six_hops(self):
        module = self.load()
        upstream = self.use_upstream(module,
                                     FakeResponse(307, {"location": "https://cdn.example.test/v.mp4"}))
        client = self.complete(module.get_shared_client())

        with self.assertRaises(HTTPException) as raised:
            self.complete(module._safe_proxy_stream(client, "https://cdn.example.test/v.mp4",
                                               headers={}, timeout=None))

        self.assertEqual(raised.exception.detail, "Too many upstream redirects")
        self.assertEqual(len(upstream.requests), 6)

    def test_a_redirect_into_a_private_host_is_refused_before_connecting(self):
        module = self.load()
        upstream = self.use_upstream(module, FakeResponse(302, {"location": "/second.mp4"}),
                                     FakeResponse(200))
        client = self.complete(module.get_shared_client())
        original = module.validate_proxy_url

        def refuse_second(url):
            if url.endswith("/second.mp4"):
                raise HTTPException(status_code=400,
                                    detail="Non-public stream addresses are not allowed")
            return original(url)

        module.validate_proxy_url = refuse_second

        with self.assertRaises(HTTPException) as raised:
            self.complete(module._safe_proxy_stream(client, "https://cdn.example.test/v.mp4",
                                               headers={}, timeout=None))

        self.assertEqual(len(upstream.requests), 1)
        self.assertEqual(raised.exception.status_code, 400)


class TestUpstreamLiveness(ResolverHarness):
    def probe(self, module, status_code):
        self.use_upstream(module, FakeResponse(status_code))
        return self.complete(module.upstream_url_alive("https://cdn.example.test/x.mp4"))

    def test_a_serving_status_counts_as_alive(self):
        module = self.load()

        for status_code in (200, 206, 301, 302, 307, 308):
            with self.subTest(status_code=status_code):
                self.assertIs(self.probe(module, status_code), True)

    def test_a_gone_upstream_counts_as_dead(self):
        module = self.load()

        for status_code in (401, 403, 404, 410, 502, 504):
            with self.subTest(status_code=status_code):
                self.assertIs(self.probe(module, status_code), False)

    def test_an_unexpected_status_leaves_the_answer_unknown(self):
        module = self.load()

        self.assertIsNone(self.probe(module, 500))

    def test_the_probe_reads_one_byte_with_a_browser_agent(self):
        module = self.load()
        upstream = self.use_upstream(module, FakeResponse(206))

        self.assertIs(self.complete(module.upstream_url_alive("https://cdn.example.test/x.mp4")), True)

        kwargs = upstream.kwargs()
        self.assertEqual(kwargs["headers"]["Range"], "bytes=0-0")
        self.assertEqual(kwargs["headers"]["Referer"], "https://cdn.example.test/")
        self.assertTrue(kwargs["headers"]["User-Agent"].startswith("Mozilla/5.0"))
        self.assertEqual(kwargs["follow_redirects"], False)
        self.assertEqual(kwargs["timeout"], 5.0)

    def test_a_network_failure_leaves_the_answer_unknown(self):
        module = self.load()
        self.use_upstream(module, RuntimeError("connection reset"))

        self.assertIsNone(self.complete(module.upstream_url_alive("https://cdn.example.test/x.mp4")))
        self.assertIn("Stream liveness check failed", self.logs.blob("DEBUG"))

    def test_an_unvalidated_address_is_reported_as_unknown(self):
        module = self.load()
        module.socket = FakeSocket(addresses=[PRIVATE_IP])

        self.assertIsNone(self.complete(module.upstream_url_alive("https://cdn.example.test/x.mp4")))


class TestProxyFetch(ResolverHarness):
    def request(self, method="GET", headers=None):
        return types.SimpleNamespace(method=method, headers=Headers(headers))

    def fetch(self, module, url="https://cdn.example.test/x.mp4", cache_key=None, method="GET"):
        return self.complete(module.proxy_fetch_response(self.request(method), url, cache_key))

    def test_a_telegram_reference_uses_the_range_proxy(self):
        module = self.load()
        seen = []

        async def telegram(request, url, cache_key=None, services=None):
            seen.append((url, cache_key))
            return Response(content="telegram", media_type="video/mp4")

        module._proxy_telegram_response_impl = telegram

        response = self.fetch(module, "tg://-1001:42", "k")

        self.assertEqual(response.body, b"telegram")
        self.assertEqual(seen, [("tg://-1001:42", "k")])

    def test_a_playlist_is_refused_before_anything_is_fetched(self):
        module = self.load()
        upstream = self.use_upstream(module)
        module.looks_like_playlist = lambda url: True

        with self.assertRaises(HTTPException) as raised:
            self.fetch(module, "https://cdn.example.test/x.m3u8")

        self.assertEqual(raised.exception.detail, "HLS proxying is not supported")
        self.assertEqual(upstream.requests, [])

    def test_an_upstream_error_becomes_a_bad_gateway(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(404))
        cache = self.use_cache(module)

        with self.assertRaises(HTTPException) as raised:
            self.fetch(module, cache_key="youtube:abc")

        self.assertEqual(raised.exception.detail, "Upstream fetch failed (HTTP 404)")
        self.assertEqual(cache.invalidated, [])
        self.assertEqual(module.upstream.contexts[0].exits, 1)

    def test_a_gateway_error_at_the_edge_drops_the_cached_url(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(504))
        cache = self.use_cache(module)

        with self.assertRaises(HTTPException) as raised:
            self.fetch(module, cache_key="youtube:abc")

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(cache.invalidated, ["youtube:abc"])

    def test_a_transport_failure_is_reported_as_bad_gateway(self):
        module = self.load()
        self.use_upstream(module, httpx.ConnectError("no route to host"))

        with self.assertRaises(HTTPException) as raised:
            self.fetch(module)

        self.assertIn("Upstream fetch failed", raised.exception.detail)

    def test_a_get_streams_the_body_and_keeps_the_upstream_framing(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(
            206, {"Content-Range": "bytes 0-1/2", "Accept-Ranges": "bytes",
                  "Content-Length": "2", "Content-Type": "video/mp4"},
            chunks=[b"ab", b"cd"]))

        response = self.fetch(module)
        body = self.complete(_drain(response))

        self.assertEqual(body, b"abcd")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-1/2")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["content-length"], "2")
        self.assertEqual(response.media_type, "video/mp4")
        self.assertEqual(module.upstream.method(), "GET")
        self.assertEqual(module.upstream.contexts[0].exits, 1)

    def test_a_missing_media_type_falls_back_to_octet_stream(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(200))

        response = self.fetch(module)

        self.assertEqual(response.media_type, "application/octet-stream")

    def test_the_client_range_is_passed_to_the_upstream(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(206, {"Content-Range": "bytes 0-0/1"}))

        self.complete(module.proxy_fetch_response(self.request(headers={"range": "bytes=0-0"}),
                                             "https://cdn.example.test/x.mp4"))

        self.assertEqual(module.upstream.header("Range"), "bytes=0-0")

    def test_a_request_without_a_range_asks_for_the_whole_stream(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(200))

        self.fetch(module)

        self.assertIsNone(module.upstream.header("Range"))
        self.assertEqual(module.upstream.header("Referer"), "https://cdn.example.test/")

    def test_a_head_request_closes_the_upstream_and_answers_alone(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(
            200, {"Content-Length": "1234", "Content-Type": "video/mp4"}))

        response = self.fetch(module, method="HEAD")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"")
        self.assertEqual(response.headers["content-length"], "1234")
        self.assertEqual(module.upstream.method(), "HEAD")
        self.assertEqual(module.upstream.contexts[0].exits, 1)

    def test_an_upstream_that_refuses_to_close_is_still_a_bad_gateway(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(502), exit_error=OSError("socket is stuck"))
        cache = self.use_cache(module)

        with self.assertRaises(HTTPException) as raised:
            self.fetch(module, cache_key="youtube:abc")

        self.assertEqual(raised.exception.detail, "Upstream fetch failed (HTTP 502)")
        self.assertEqual(cache.invalidated, ["youtube:abc"])
        self.assertEqual(module.upstream.contexts[0].exits, 1)

    def test_a_head_answer_survives_a_stuck_upstream_close(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(200, {"Content-Length": "7"}),
                          exit_error=OSError("socket is stuck"))

        response = self.fetch(module, method="HEAD")

        self.assertEqual(response.headers["content-length"], "7")
        self.assertEqual(module.upstream.contexts[0].exits, 1)

    def test_a_cancelled_download_still_closes_the_upstream(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(200, chunks=[b"ab"],
                                                error=asyncio.CancelledError()))

        response = self.fetch(module)

        with self.assertRaises(asyncio.CancelledError):
            self.complete(_drain(response))
        self.assertEqual(module.upstream.contexts[0].exits, 1)

    def test_a_finished_stream_survives_a_stuck_upstream_close(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(200, chunks=[b"ab", b"cd"]),
                          exit_error=OSError("socket is stuck"))

        response = self.fetch(module)

        self.assertEqual(self.complete(_drain(response)), b"abcd")
        self.assertEqual(module.upstream.contexts[0].exits, 1)


if __name__ == "__main__":
    unittest.main()
