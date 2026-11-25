"""Tests for resolver HTTP Telegram HEAD requests and byte-range streaming."""

import os
from pathlib import Path
import unittest

from pg_helper import TEST_URL

if TEST_URL:
    os.environ["DATABASE_URL"] = TEST_URL
else:
    os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault(
    "CONFIG_PATH",
    str(Path(__file__).parent / "fixtures" / "config_telegram_only.yaml"))

from tests.test_telegram_stream import _require_resolver, resolver_app


class FakeRefProvider:
    """Minimal telegram provider double for app.py HEAD/range tests."""

    _USE_PAYLOAD = object()

    def __init__(self, payload, size=_USE_PAYLOAD):
        self.payload = payload
        # size=None simulates unknown file size (file_size -> None).
        self.size = len(payload) if size is FakeRefProvider._USE_PAYLOAD else size
        self.download_calls = []

    async def file_size(self, ref):
        return self.size

    async def download_range(self, ref, offset, length):
        self.download_calls.append((ref, offset, length))

        async def chunks():
            end = len(self.payload) if length is None else offset + length
            yield self.payload[offset:end]

        return chunks()


def _fake_request(method="GET", range_header=None):
    class Req:
        pass
    r = Req()
    r.method = method
    r.headers = {}
    if range_header is not None:
        r.headers["range"] = range_header
    return r


async def response_body(resp):
    iterator = getattr(resp, "body_iterator", None)
    if iterator is None:
        body = getattr(resp, "body", b"")
        return bytes(body) if body else b""
    out = bytearray()
    async for chunk in iterator:
        if isinstance(chunk, (bytes, bytearray)):
            out += chunk
        elif isinstance(chunk, str):
            out += chunk.encode()
    return bytes(out)


class TestTelegramHeadAndRange(unittest.IsolatedAsyncioTestCase):
    """Focused app.py Telegram HEAD/range tests (new file only)."""

    async def test_unauthorized_channel_range_fails_before_streaming(self):
        _require_resolver(self)
        from fastapi import HTTPException
        from unittest.mock import patch

        class UnauthorizedProvider(FakeRefProvider):
            def is_channel_authorized(self, channel_ref):
                return False

            async def file_size(self, ref):
                raise AssertionError("must reject before metadata lookup")

            async def download_range(self, ref, offset, length):
                raise AssertionError("must not start a media stream")

        provider = UnauthorizedProvider(b"payload")
        with patch.object(resolver_app, "get_provider", return_value=provider):
            with self.assertRaises(HTTPException) as raised:
                await resolver_app._proxy_telegram_response(
                    _fake_request("GET", "bytes=0-65535"),
                    "tg://-1001234567890:42:999",
                )
        self.assertEqual(raised.exception.status_code, 403)

    async def test_head_matches_get_headers_without_consuming_iterator(self):
        _require_resolver(self)
        payload = bytes(i % 251 for i in range(100_000))
        for method, rng, want_status in [
            ("GET", None, 200),
            ("HEAD", None, 200),
            ("GET", "bytes=10-99", 206),
            ("HEAD", "bytes=10-99", 206),
        ]:
            prov = FakeRefProvider(payload)
            from unittest.mock import patch
            with patch.object(resolver_app, "get_provider", return_value=prov):
                resp = await resolver_app._proxy_telegram_response(
                    _fake_request(method, rng), "tg://ref")
            self.assertEqual(resp.status_code, want_status)
            if method == "HEAD":
                # must not create/consume the download iterator
                self.assertEqual(prov.download_calls, [],
                                 "HEAD must not call download_range")
                body = await response_body(resp)
                self.assertEqual(body, b"")
                # full HEAD advertises the known size truthfully
                if rng is None:
                    self.assertEqual(dict(resp.headers).get("content-length"),
                                     str(len(payload)))

    async def test_head_unknown_size_omits_content_length(self):
        _require_resolver(self)
        payload = bytes(i % 251 for i in range(10_000))
        prov = FakeRefProvider(payload, size=None)  # file_size -> None
        from unittest.mock import patch
        with patch.object(resolver_app, "get_provider", return_value=prov):
            resp = await resolver_app._proxy_telegram_response(
                _fake_request("HEAD", None), "tg://ref")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(prov.download_calls, [])
        # Unknown size: no Content-Length at all, never a synthetic 0.
        self.assertNotIn("content-length", dict(resp.headers))
        body = await response_body(resp)
        self.assertEqual(body, b"")

    async def test_head_range_parity_with_get(self):
        _require_resolver(self)
        payload = bytes((i * 7) % 256 for i in range(50_000))
        from unittest.mock import patch
        prov_get = FakeRefProvider(payload)
        with patch.object(resolver_app, "get_provider", return_value=prov_get):
            resp_get = await resolver_app._proxy_telegram_response(
                _fake_request("GET", "bytes=100-199"), "tg://ref")
        prov_head = FakeRefProvider(payload)
        with patch.object(resolver_app, "get_provider", return_value=prov_head):
            resp_head = await resolver_app._proxy_telegram_response(
                _fake_request("HEAD", "bytes=100-199"), "tg://ref")
        self.assertEqual(resp_head.status_code, resp_get.status_code)
        get_h = dict(resp_get.headers)
        head_h = dict(resp_head.headers)
        for key in ("content-range", "content-length", "accept-ranges"):
            self.assertEqual(head_h.get(key), get_h.get(key),
                             f"HEAD/GET header mismatch for {key}")

    async def test_suffix_range_serves_last_n_bytes(self):
        _require_resolver(self)
        payload = bytes(i % 256 for i in range(10_000))
        prov = FakeRefProvider(payload)
        from unittest.mock import patch
        with patch.object(resolver_app, "get_provider", return_value=prov):
            resp = await resolver_app._proxy_telegram_response(
                _fake_request("GET", "bytes=-500"), "tg://ref")
        self.assertEqual(resp.status_code, 206)
        self.assertEqual(dict(resp.headers).get("content-range"),
                         f"bytes {len(payload) - 500}-{len(payload) - 1}/{len(payload)}")
        body = await response_body(resp)
        self.assertEqual(body, payload[-500:])
        self.assertEqual(prov.download_calls[0][1:], (len(payload) - 500, 500))

    async def test_suffix_range_head_parity(self):
        _require_resolver(self)
        payload = bytes(i % 256 for i in range(10_000))
        from unittest.mock import patch
        with patch.object(resolver_app, "get_provider",
                          return_value=FakeRefProvider(payload)):
            resp_get = await resolver_app._proxy_telegram_response(
                _fake_request("GET", "bytes=-100"), "tg://ref")
        prov_head = FakeRefProvider(payload)
        with patch.object(resolver_app, "get_provider", return_value=prov_head):
            resp_head = await resolver_app._proxy_telegram_response(
                _fake_request("HEAD", "bytes=-100"), "tg://ref")
        self.assertEqual(resp_head.status_code, 206)
        self.assertEqual(dict(resp_head.headers).get("content-range"),
                         dict(resp_get.headers).get("content-range"))
        self.assertEqual(prov_head.download_calls, [])

    async def test_unsatisfiable_range_416_with_content_range(self):
        _require_resolver(self)
        payload = bytes(i % 256 for i in range(1000))
        prov = FakeRefProvider(payload)
        from unittest.mock import patch
        from fastapi import HTTPException
        with patch.object(resolver_app, "get_provider", return_value=prov):
            with self.assertRaises(HTTPException) as ctx:
                await resolver_app._proxy_telegram_response(
                    _fake_request("GET", "bytes=5000-6000"), "tg://ref")
        self.assertEqual(ctx.exception.status_code, 416)
        headers = {k.lower(): v for k, v in
                   dict(getattr(ctx.exception, "headers", {}) or {}).items()}
        self.assertEqual(headers.get("content-range"), "bytes */1000")

    async def test_no_multipart_response(self):
        _require_resolver(self)
        payload = bytes(i % 256 for i in range(5000))
        prov = FakeRefProvider(payload)
        from unittest.mock import patch
        with patch.object(resolver_app, "get_provider", return_value=prov):
            resp = await resolver_app._proxy_telegram_response(
                _fake_request("GET", "bytes=0-99, 200-299"), "tg://ref")
        # multi-range is not supported: honest 200 full response, never
        # multipart/byteranges
        self.assertEqual(resp.status_code, 200)
        ctype = dict(resp.headers).get("content-type", "")
        self.assertNotIn("multipart", ctype.lower())

    async def test_range_with_unknown_file_size_preserves_206_and_offset(self):
        """Round 2 Finding 10: Range requests with unknown file size must preserve 206,

        Content-Range bytes start-end/*, Content-Length, and exact requested offset
        instead of downgrading to 200 and restarting from byte 0.
        """
        _require_resolver(self)
        payload = bytes((i * 3 + 7) % 256 for i in range(20_000))
        prov = FakeRefProvider(payload, size=None)  # unknown file size
        from unittest.mock import patch
        with patch.object(resolver_app, "get_provider", return_value=prov):
            resp = await resolver_app._proxy_telegram_response(
                _fake_request("GET", "bytes=500-999"), "tg://ref")

        # Must be 206 Partial Content, NEVER downgraded to 200
        self.assertEqual(resp.status_code, 206)
        headers = dict(resp.headers)
        self.assertEqual(headers.get("content-range"), "bytes 500-999/*")
        self.assertEqual(headers.get("content-length"), "500")

        # Must request exact partial range from provider, NEVER restart from 0
        self.assertEqual(prov.download_calls, [("ref", 500, 500)])
        body = await response_body(resp)
        self.assertEqual(body, payload[500:1000])

    async def test_range_with_file_size_error_fails_loudly_with_503(self):
        """Range requests requiring file size must fail loudly with 503 when size probe fails."""
        _require_resolver(self)
        payload = bytes((i * 5 + 13) % 256 for i in range(20_000))

        class ErrorSizeProvider(FakeRefProvider):
            async def file_size(self, ref):
                raise RuntimeError("Temporary Telegram metadata failure")

        prov = ErrorSizeProvider(payload)
        from unittest.mock import patch
        from fastapi import HTTPException
        with patch.object(resolver_app, "get_provider", return_value=prov):
            with self.assertRaises(HTTPException) as cm:
                await resolver_app._proxy_telegram_response(
                    _fake_request("GET", "bytes=1000-1499"), "tg://ref")

        self.assertEqual(cm.exception.status_code, 503)
        self.assertEqual(prov.download_calls, [])

    async def test_normal_get_without_range_proceeds_when_file_size_fails(self):
        """Normal full GET requests stream successfully even when file_size probe fails."""
        _require_resolver(self)
        payload = bytes((i * 5 + 13) % 256 for i in range(10_000))

        class ErrorSizeProvider(FakeRefProvider):
            async def file_size(self, ref):
                raise RuntimeError("Temporary Telegram metadata failure")

        prov = ErrorSizeProvider(payload)
        from unittest.mock import patch
        with patch.object(resolver_app, "get_provider", return_value=prov):
            resp = await resolver_app._proxy_telegram_response(
                _fake_request("GET", None), "tg://ref")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(prov.download_calls, [("ref", 0, None)])
        body = await response_body(resp)
        self.assertEqual(body, payload)

    async def test_normal_head_without_range_proceeds_when_file_size_fails(self):
        """Normal full HEAD requests succeed with 200 even when file_size probe fails."""
        _require_resolver(self)
        payload = bytes((i * 5 + 13) % 256 for i in range(10_000))

        class ErrorSizeProvider(FakeRefProvider):
            async def file_size(self, ref):
                raise RuntimeError("Temporary Telegram metadata failure")

        prov = ErrorSizeProvider(payload)
        from unittest.mock import patch
        with patch.object(resolver_app, "get_provider", return_value=prov):
            resp = await resolver_app._proxy_telegram_response(
                _fake_request("HEAD", None), "tg://ref")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(prov.download_calls, [])
        self.assertNotIn("content-length", dict(resp.headers))



class TestStreamStartFailures(unittest.IsolatedAsyncioTestCase):
    """A stream that cannot produce one byte must answer with a status, not a truncated body."""

    async def test_missing_message_answers_404_before_any_bytes(self):
        _require_resolver(self)
        from fastapi import HTTPException
        from unittest.mock import patch
        from resolver.providers.telegram_errors import TelegramMessageNotFound

        class GoneProvider(FakeRefProvider):
            async def download_range(self, ref, offset, length):
                self.download_calls.append((ref, offset, length))

                async def chunks():
                    raise TelegramMessageNotFound(f"Telegram message not found for ref {ref!r}")
                    yield b""  # pragma: no cover - keeps chunks() an async generator

                return chunks()

        prov = GoneProvider(b"payload")
        with patch.object(resolver_app, "get_provider", return_value=prov):
            with self.assertRaises(HTTPException) as raised:
                await resolver_app._proxy_telegram_response(
                    _fake_request("GET", None), "tg://-1001234567890:5:99")

        self.assertEqual(raised.exception.status_code, 404)
        self.assertIn("not found", raised.exception.detail)

    async def test_missing_message_answers_404_for_a_range_request_too(self):
        _require_resolver(self)
        from fastapi import HTTPException
        from unittest.mock import patch
        from resolver.providers.telegram_errors import TelegramMessageNotFound

        class GoneProvider(FakeRefProvider):
            async def download_range(self, ref, offset, length):
                async def chunks():
                    raise TelegramMessageNotFound("gone")
                    yield b""  # pragma: no cover

                return chunks()

        prov = GoneProvider(b"payload")
        with patch.object(resolver_app, "get_provider", return_value=prov):
            with self.assertRaises(HTTPException) as raised:
                await resolver_app._proxy_telegram_response(
                    _fake_request("GET", "bytes=0-1023"), "tg://-1001234567890:5:99")

        self.assertEqual(raised.exception.status_code, 404)

    async def test_first_chunk_failure_answers_502(self):
        _require_resolver(self)
        from fastapi import HTTPException
        from unittest.mock import patch

        class BrokenProvider(FakeRefProvider):
            async def download_range(self, ref, offset, length):
                async def chunks():
                    raise RuntimeError("Telegram download blew up")
                    yield b""  # pragma: no cover

                return chunks()

        prov = BrokenProvider(b"payload")
        with patch.object(resolver_app, "get_provider", return_value=prov):
            with self.assertRaises(HTTPException) as raised:
                await resolver_app._proxy_telegram_response(_fake_request("GET", None), "tg://ref")

        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("Telegram download failed", raised.exception.detail)

    async def test_peeked_first_chunk_is_served_whole(self):
        _require_resolver(self)
        from unittest.mock import patch

        payload = bytes(i % 253 for i in range(30_000))
        prov = FakeRefProvider(payload)
        with patch.object(resolver_app, "get_provider", return_value=prov):
            resp = await resolver_app._proxy_telegram_response(_fake_request("GET", None), "tg://ref")

        self.assertEqual(resp.status_code, 200)
        body = await response_body(resp)
        self.assertEqual(body, payload)

    async def test_peeked_first_chunk_keeps_range_offsets_exact(self):
        _require_resolver(self)
        from unittest.mock import patch

        payload = bytes(i % 251 for i in range(20_000))
        prov = FakeRefProvider(payload)
        with patch.object(resolver_app, "get_provider", return_value=prov):
            resp = await resolver_app._proxy_telegram_response(
                _fake_request("GET", "bytes=5000-5999"), "tg://ref")

        self.assertEqual(resp.status_code, 206)
        body = await response_body(resp)
        self.assertEqual(body, payload[5000:6000])


if __name__ == "__main__":
    unittest.main()
