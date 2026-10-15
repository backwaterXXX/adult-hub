import sys
import tempfile
import unittest
import httpx
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pg_helper import PgTestCase
from dead_streams import (
    DeadStreamServices,
    deadlisted_ids,
    denylist_ids,
    extract_embed_host,
    hide_dead_streams,
    is_unsupported_embed_host,
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
            dead_stream_providers=lambda: ("sample",),
            is_cleanup_allowed=lambda *a, **k: True,
            purge_videos=MagicMock(return_value=0),
            probe_stream_via_resolver=MagicMock(return_value=True),
            resolver_api_url="http://resolver:8090",
            logger=self.mock_logger,
            default_headers={"User-Agent": "TestUA"},
            time=self.mock_time,
            resolver_timeout=5.0,
            sleep_seconds=0.0,
            denylist_days=90,
            recheck_days=7,
            check_per_run=10,
            strikes_to_purge=2,
            max_purge_per_run=5,
        )

    def test_extract_embed_host(self):
        self.assertEqual(extract_embed_host(""), "")
        self.assertEqual(extract_embed_host("<div>no player</div>"), "")
        html_iframe = '<iframe src="https://player.example.org/embed/123"></iframe>'
        self.assertEqual(extract_embed_host(html_iframe), "player.example.org")
        html_prop = '<span itemprop="embedUrl" content="//embed.example.net/v/abc"></span>'
        self.assertEqual(extract_embed_host(html_prop), "embed.example.net")

    def test_detailed_availability_probe_classifies_network_timeout_as_transient(self):
        with patch("dead_streams.httpx.Client", side_effect=httpx.ConnectTimeout("test timeout")):
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
        with patch("dead_streams.httpx.Client", return_value=resolver_limited):
            self.assertEqual(
                probe_stream_via_resolver_outcome("telegram:sample", services=self.services),
                "transient_error",
            )

        redirected_services = replace(
            self.services, stream_mode_for_provider=lambda _cid: "redirect",
        )
        resolver_redirect = Client(response(302, {"location": "https://cdn.example/video.m3u8"}))
        upstream_limited = Client(response(429))
        with patch("dead_streams.httpx.Client", side_effect=[resolver_redirect, upstream_limited]):
            self.assertEqual(
                probe_stream_via_resolver_outcome("telegram:sample", services=redirected_services),
                "transient_error",
            )

    def test_is_unsupported_embed_host(self):
        deny = ("badembed.com", "deadplayer.net")
        self.assertTrue(is_unsupported_embed_host("cdn.badembed.com", deny))
        self.assertTrue(is_unsupported_embed_host("sub.deadplayer.net", deny))
        self.assertFalse(is_unsupported_embed_host("goodplayer.com", deny))
        self.assertFalse(is_unsupported_embed_host("", deny))

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
        hide_dead_streams(collected, self.conn, self.media, services=self.services)
        self.assertNotIn("sample:dead1", collected)
        self.assertIn("sample:live1", collected)

    def test_hide_dead_streams_probes_and_purges_after_two_strikes(self):
        # Insert video with playable = 1 and stream_fails = 1
        with self.conn:
            self.conn.execute(
                """INSERT INTO videos (id, provider, title, playable, stream_fails, stream_checked)
                   VALUES ('sample:vid_strike', 'sample', 'Test Title', 1, 1, 1600000000)"""
            )

        # Mock probe returning False (dead)
        mock_purge = MagicMock(return_value=1)
        services = DeadStreamServices(
            stream_mode_for_provider=lambda _cid: "proxy",
            dead_stream_providers=lambda: ("sample",),
            is_cleanup_allowed=lambda *a, **k: True,
            purge_videos=mock_purge,
            probe_stream_via_resolver=lambda cid: False,
            resolver_api_url="http://resolver:8090",
            logger=self.mock_logger,
            default_headers={"User-Agent": "TestUA"},
            time=self.mock_time,
            resolver_timeout=5.0,
            sleep_seconds=0.0,
            denylist_days=90,
            recheck_days=7,
            check_per_run=10,
            strikes_to_purge=2,
            max_purge_per_run=5,
        )

        hide_dead_streams({}, self.conn, self.media, services=services)

        # Should have called purge_videos with sample:vid_strike
        mock_purge.assert_called_once()
        args = mock_purge.call_args[0]
        self.assertEqual(args[2], ["sample:vid_strike"])
        # Should be denylisted
        self.assertIn("sample:vid_strike", deadlisted_ids(self.conn))


if __name__ == "__main__":
    unittest.main()
