import tempfile
import unittest
import httpx
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


from pg_helper import PgTestCase
from sync.dead_streams import (
    DeadStreamServices,
    deadlisted_ids,
    denylist_ids,
    hide_dead_streams,
    probe_stream_via_resolver_outcome,
)


class TestDeadStreams(PgTestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.media = self.root / "media"
        self.media.mkdir()

        self.mock_logger = MagicMock()
        self.mock_time = MagicMock()
        self.mock_time.time.return_value = 1700000000.0

        self.services = DeadStreamServices(
            stream_mode_for_provider=lambda _cid: "proxy",
            resolver_api_url="http://resolver:8090",
            logger=self.mock_logger,
            default_headers={"User-Agent": "TestUA"},
            time=self.mock_time,
            resolver_timeout=5.0,
            denylist_days=90,
        )

    def test_detailed_availability_probe_classifies_network_timeout_as_transient(self):
        with patch("sync.dead_streams.httpx.Client", side_effect=httpx.ConnectTimeout("test timeout")):
            result = probe_stream_via_resolver_outcome("telegram:sample", services=self.services)
        self.assertEqual(result, "transient_error")

    def test_detailed_availability_probe_classifies_rate_limits_as_transient_on_both_hops(self):
        class ResponseContext:
            def __init__(self, response):
                self.response = response

            def __enter__(self):
                return self.response

            def __exit__(self, *_args):
                return False

        class Client:
            def __init__(self, response):
                self.response = response

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return ResponseContext(self.response)

        def response(status, headers=None, body=b""):
            return SimpleNamespace(status_code=status, headers=headers or {},
                                   iter_bytes=lambda _size: iter([body] if body else []))

        resolver_limited = Client(response(429))
        with patch("sync.dead_streams.httpx.Client", return_value=resolver_limited):
            self.assertEqual(
                probe_stream_via_resolver_outcome("telegram:sample", services=self.services),
                "transient_error",
            )

        redirected_services = replace(
            self.services, stream_mode_for_provider=lambda _cid: "redirect",
        )
        resolver_redirect = Client(response(302, {"location": "https://cdn.example/video.m3u8"}))
        upstream_limited = Client(response(429))
        with patch("sync.dead_streams.httpx.Client", side_effect=[resolver_redirect, upstream_limited]):
            self.assertEqual(
                probe_stream_via_resolver_outcome("telegram:sample", services=redirected_services),
                "transient_error",
            )

    def test_denylist_and_deadlisted_ids(self):
        denylist_ids(self.conn, ["sample:vid1", "sample:vid2"], "test dead", time_fn=lambda: 1700000000.0)
        dead = deadlisted_ids(self.conn)
        self.assertEqual(dead, {"sample:vid1", "sample:vid2"})

        # Adding same ID again should ignore duplicate
        denylist_ids(self.conn, ["sample:vid1"], "duplicate reason", time_fn=lambda: 1700000000.0)
        self.assertEqual(deadlisted_ids(self.conn), {"sample:vid1", "sample:vid2"})

    def test_hide_dead_streams_filters_collected_corpses(self):
        denylist_ids(self.conn, ["sample:dead1"], "corpse", time_fn=lambda: 1700000000.0)
        collected = {
            "sample:dead1": {"title": "Dead"},
            "sample:live1": {"title": "Live"},
        }
        hide_dead_streams(collected, self.conn, services=self.services)
        self.assertNotIn("sample:dead1", collected)
        self.assertIn("sample:live1", collected)


if __name__ == "__main__":
    unittest.main()
