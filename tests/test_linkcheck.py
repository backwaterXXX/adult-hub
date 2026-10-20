"""The link health monitor: what it asks the resolver for and what it reports."""

import contextlib
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

RUNNER_PATH = Path(__file__).resolve().parents[1] / "sync" / "linkcheck.py"


class LogCapture:
    def __init__(self):
        self.entries = []

    def _record(self, level, message, *args):
        self.entries.append((level, message % args if args else str(message)))

    def info(self, message, *args):
        self._record("info", message, *args)

    def error(self, message, *args):
        self._record("error", message, *args)

    def text(self, level=None):
        return [message for entry_level, message in self.entries if level is None or entry_level == level]

    def has(self, needle, level=None):
        return any(needle in message for message in self.text(level))


class FakeTime:
    """A clock that stamps one fixed moment and counts the pauses instead of taking them."""

    def __init__(self):
        self.stamps = 0
        self.sleeps = []

    def strftime(self, _format):
        self.stamps += 1
        return "2026-01-01 00:00:00"

    def sleep(self, seconds):
        self.sleeps.append(seconds)


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class FakeConnection:
    """Answers the two queries the monitor runs and records every statement."""

    def __init__(self, providers=(), samples=None):
        self.providers = list(providers)
        self.samples = samples or {}
        self.statements = []
        self.closed = False

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))
        if "DISTINCT provider" in sql:
            return Cursor([(provider,) for provider in self.providers])
        return Cursor([(video_id,) for video_id in self.samples.get(params[0], [])])

    def close(self):
        self.closed = True


class FakeStream:
    """One httpx streaming response: a status, headers and the chunks it yields."""

    def __init__(self, status_code, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = list(chunks)
        self.requested_chunk_size = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def iter_bytes(self, size):
        self.requested_chunk_size = size
        yield from self.chunks


class FakeClient:
    """Hands out scripted responses and records the request each one was made for."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def stream(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        outcome = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def url(self, index=0):
        return self.requests[index][1]

    def request(self, index=0):
        return self.requests[index][2]


class LinkcheckHarness(unittest.TestCase):
    """One freshly executed copy of ``sync/linkcheck.py`` per test, with its log in a temp dir."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.logs = LogCapture()
        self.logging_names = []
        self.connection = FakeConnection()

    def record_logging(self, name):
        self.logging_names.append(name)
        return self.logs

    def load(self, *, resolver_url="http://resolver:8090", telegram_mode="proxy",
             db_url="postgresql://linkcheck.invalid/adulthub", sample_default=None,
             max_fail_rate=None):
        env = {
            "RESOLVER_API_URL": resolver_url,
            "DATABASE_URL": db_url,
            "STREAM_MODE_TELEGRAM": telegram_mode,
        }
        if sample_default is not None:
            env["LINKCHECK_SAMPLE_PER_PROVIDER"] = str(sample_default)
        if max_fail_rate is not None:
            env["LINKCHECK_MAX_FAIL_RATE"] = str(max_fail_rate)
        temp = tempfile.TemporaryDirectory(prefix="adult_hub_linkcheck_")
        self.addCleanup(temp.cleanup)
        self.log_path = Path(temp.name) / "linkcheck.log"
        env["LINKCHECK_LOG_PATH"] = str(self.log_path)
        self.stack.enter_context(patch.dict(os.environ, env))
        self.stack.enter_context(patch("common.logging_setup.configure_logging", self.record_logging))
        spec = importlib.util.spec_from_file_location("linkcheck_under_test", RUNNER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.time = FakeTime()
        module.db = types.SimpleNamespace(connect=lambda _url: self.connection)
        self.module = module
        return module

    def running(self, *argv, responses=None, providers=("telegram",), samples=None, httpx=True):
        """Drive ``main()`` with a scripted client, command line and database rows."""
        if samples is None:
            samples = {"telegram": ["telegram:-100:1"]}
        self.connection = FakeConnection(providers=providers, samples=samples)
        stream = responses or [FakeStream(200, {"content-type": "video/mp4"}, [b"\x00"])]
        client = FakeClient(*stream)
        module = self.module
        module.httpx = None if httpx is False else types.SimpleNamespace(
            Client=lambda **_kwargs: contextlib.nullcontext(client),
            Timeout=lambda *args, **kwargs: (args, kwargs))
        with patch.object(sys, "argv", ["linkcheck.py", *argv]):
            return module.main(), client

    def read_log(self):
        return self.log_path.read_text(encoding="utf-8").splitlines()


class TestStartupEnvironment(LinkcheckHarness):
    def test_the_resolver_address_and_log_path_come_from_the_environment(self):
        module = self.load(resolver_url="http://gateway.internal:9000",
                           db_url="postgresql://someone@db.internal/adulthub")

        self.assertEqual(module.RESOLVER_API_URL, "http://gateway.internal:9000")
        self.assertEqual(module.DB_URL, "postgresql://someone@db.internal/adulthub")
        self.assertEqual(Path(module.LINKCHECK_LOG_PATH).name, "linkcheck.log")

    def test_a_blank_database_url_is_not_a_url(self):
        module = self.load(db_url="   ")

        self.assertIsNone(module.DB_URL)

    def test_the_logging_setup_is_named_for_the_monitor(self):
        self.load()

        self.assertEqual(self.logging_names, ["sync.linkcheck"])


class TestReporting(LinkcheckHarness):
    def test_every_result_lands_on_the_terminal_and_in_the_log(self):
        module = self.load()

        module.report("[telegram] 3/3 ok -> HEALTHY")

        self.assertEqual(self.read_log(), ["2026-01-01 00:00:00 [telegram] 3/3 ok -> HEALTHY"])
        self.assertEqual(self.logs.text("info"), ["[telegram] 3/3 ok -> HEALTHY"])

    def test_the_log_is_appended_to_rather_than_rewritten(self):
        module = self.load()

        module.report("first")
        module.report("second")

        self.assertEqual(len(self.read_log()), 2)

    def test_an_unwritable_log_is_reported_but_never_raises(self):
        module = self.load()
        module.LINKCHECK_LOG_PATH = Path(module.LINKCHECK_LOG_PATH).parent

        module.report("still reported here")

        self.assertTrue(self.logs.has("Could not write linkcheck log", "error"))
        self.assertEqual(self.logs.text("info"), ["still reported here"])


class TestStreamMode(LinkcheckHarness):
    def test_telegram_uses_the_configured_stream_mode(self):
        module = self.load(telegram_mode="direct")

        self.assertEqual(module.stream_mode_for_provider("telegram:-100:42"), "direct")

    def test_every_other_provider_falls_back_to_redirect(self):
        module = self.load(telegram_mode="proxy")

        for video_id in ("youtube:abc", "vimeo:", "no-colon-here"):
            with self.subTest(video_id=video_id):
                self.assertEqual(module.stream_mode_for_provider(video_id), "redirect")


class TestSampling(LinkcheckHarness):
    def setUp(self):
        super().setUp()
        self.load()

    def test_the_sample_query_binds_the_provider_and_the_size(self):
        self.connection = FakeConnection(samples={"telegram": ["telegram:-100:1", "telegram:-100:2"]})

        ids = self.module.sample_ids(self.connection, "telegram", 2)

        self.assertEqual(ids, ["telegram:-100:1", "telegram:-100:2"])
        self.assertEqual(self.connection.statements[0],
                         ("SELECT id FROM videos WHERE provider = %s ORDER BY RANDOM() LIMIT %s",
                          ("telegram", 2)))


class TestOneCheck(LinkcheckHarness):
    def setUp(self):
        super().setUp()
        self.load(resolver_url="http://resolver:8080/")

    def check(self, *responses, video_id="telegram:-100:42"):
        client = FakeClient(*responses)
        return self.module.check_one(client, video_id), client

    def test_a_proxied_playlist_is_accepted_by_its_content_type(self):
        stream = FakeStream(206, {"content-type": "application/vnd.apple.mpegurl"}, [b"#EXTM3U\n"])

        (ok, detail), _client = self.check(stream)

        self.assertTrue(ok)
        self.assertEqual(detail, "proxied playlist OK (8B)")
        self.assertEqual(stream.requested_chunk_size, 8192)

    def test_a_proxied_playlist_is_accepted_by_its_body_alone(self):
        (ok, detail), _client = self.check(FakeStream(200, {}, [b"  #EXTM3U\n"]))

        self.assertEqual((ok, detail), (True, "proxied playlist OK (10B)"))

    def test_a_proxied_video_is_recognised_by_its_ftyp_marker(self):
        (ok, detail), _client = self.check(FakeStream(200, {}, [b"\x00\x00\x00\x18ftyp"]))

        self.assertEqual((ok, detail), (True, "proxied stream OK (8B, video)"))

    def test_a_proxied_video_is_recognised_by_its_content_type(self):
        (ok, detail), _client = self.check(FakeStream(200, {"content-type": "video/mp4"}, [b"\x00" * 8]))

        self.assertEqual((ok, detail), (True, "proxied stream OK (8B, video/mp4)"))

    def test_any_other_body_still_counts_as_a_direct_stream(self):
        (ok, detail), _client = self.check(FakeStream(200, {"content-type": "application/octet-stream"},
                                                      [b"junk"]))

        self.assertEqual((ok, detail), (True, "direct stream OK (4B)"))

    def test_the_resolve_request_asks_for_the_provider_mode_without_following_redirects(self):
        _result, client = self.check(FakeStream(200, {}, [b"ftyp"]))

        self.assertEqual(client.url(),
                         "http://resolver:8080/admin/play/telegram:-100:42/stream?mode=proxy")
        request = client.request()
        self.assertEqual(request["headers"], {**self.module.BROWSER_HEADERS, "Range": "bytes=0-8192"})
        self.assertFalse(request["follow_redirects"])
        self.assertEqual(request["timeout"], 35.0)

    def test_a_foreign_provider_is_resolved_in_redirect_mode(self):
        _result, client = self.check(FakeStream(200, {}, [b"ftyp"]), video_id="youtube:abc")

        self.assertIn("?mode=redirect", client.url())

    def test_a_redirect_is_followed_up_by_a_playlist_request(self):
        playlist = FakeStream(200, {"content-type": "application/vnd.apple.mpegurl"}, [b"#EXTM3U\n"])

        (ok, detail), client = self.check(FakeStream(302, {"location": "https://cdn/one.m3u8"}), playlist)

        self.assertEqual((ok, detail), (True, "playlist OK (8B)"))
        self.assertEqual(client.url(1), "https://cdn/one.m3u8")
        self.assertTrue(client.request(1)["follow_redirects"])
        self.assertEqual(client.request(1)["timeout"], 20.0)

    def test_a_playlist_that_is_not_mpegurl_is_still_a_working_stream(self):
        (ok, detail), _client = self.check(FakeStream(302, {"location": "https://cdn/one"}),
                                           FakeStream(206, {}, [b"\x00" * 40]))

        self.assertEqual((ok, detail), (True, "stream OK (40B, no ctype)"))

    def test_a_dead_playlist_is_reported_with_its_status_and_a_small_read(self):
        gone = FakeStream(404, {}, [b"not found"])

        (ok, detail), _client = self.check(FakeStream(302, {"location": "https://cdn/gone"}), gone)

        self.assertEqual((ok, detail), (False, "playlist HTTP 404"))
        self.assertEqual(gone.requested_chunk_size, 1024)

    def test_a_broken_redirect_is_rejected_by_the_resolver_status(self):
        broken = FakeStream(500, {}, [b"upstream\nblew up"])

        (ok, detail), _client = self.check(broken)

        self.assertEqual((ok, detail), (False, "resolve HTTP 500: upstream blew up"))
        self.assertEqual(broken.requested_chunk_size, 1024)

    def test_a_location_header_without_a_value_is_not_a_redirect(self):
        (ok, detail), client = self.check(FakeStream(302, {"location": ""}, [b"never read"]))

        self.assertEqual((ok, detail), (False, "resolve HTTP 302: "))
        self.assertIsNone(client.responses[0].requested_chunk_size)

    def test_a_refusal_to_connect_is_reported_as_a_resolver_error(self):
        (ok, detail), _client = self.check(OSError("connection refused"))

        self.assertEqual((ok, detail), (False, "resolver error: connection refused"))

    def test_a_failing_playlist_fetch_is_reported_separately(self):
        (ok, detail), _client = self.check(FakeStream(302, {"location": "https://cdn/one"}),
                                           TimeoutError("too slow"))

        self.assertEqual((ok, detail), (False, "playlist fetch error: too slow"))


class TestMainLoop(LinkcheckHarness):
    def setUp(self):
        super().setUp()
        self.load()

    def test_missing_httpx_stops_before_the_database_is_touched(self):
        code, _client = self.running(httpx=False)

        self.assertEqual(code, 2)
        self.assertTrue(self.logs.has("httpx is required", "error"))
        self.assertEqual(self.connection.statements, [])

    def test_the_sample_size_default_comes_from_the_environment(self):
        self.load(sample_default=5)
        samples = {"telegram": [f"telegram:-100:{index}" for index in range(5)]}

        code, _client = self.running(samples=samples)

        self.assertEqual(code, 0)
        self.assertIn(("SELECT id FROM videos WHERE provider = %s ORDER BY RANDOM() LIMIT %s",
                       ("telegram", 5)), self.connection.statements)

    def test_a_failure_rate_above_the_configured_limit_exits_one(self):
        self.load(max_fail_rate="0.2")
        responses = [FakeStream(200, {"content-type": "video/mp4"}, [b"\x00"]),
                     FakeStream(500, {}, [b"gone"]), FakeStream(500, {}, [b"gone"])]

        code, _client = self.running(
            responses=responses,
            samples={"telegram": [f"telegram:-100:{index}" for index in range(3)]})

        self.assertEqual(code, 1)
        self.assertIn("1/3 ok -> BROKEN", self.read_log()[-1])

    def test_a_healthy_provider_exits_zero(self):
        code, _client = self.running()

        self.assertEqual(code, 0)
        self.assertTrue(self.read_log()[-1].endswith("[telegram] 1/1 ok -> HEALTHY"))

    def test_a_provider_without_videos_is_skipped_not_failed(self):
        code, _client = self.running(samples={"telegram": []})

        self.assertEqual(code, 0)
        self.assertIn("no videos in DB, skipping", self.read_log()[0])

    def test_a_fully_broken_provider_exits_one(self):
        samples = {"telegram": [f"telegram:-100:{index}" for index in range(3)]}
        responses = [FakeStream(500, {}, [b"gone"])] * 3

        code, _client = self.running(responses=responses, samples=samples)

        self.assertEqual(code, 1)
        self.assertIn("0/3 ok -> BROKEN", self.read_log()[-1])

    def test_a_partially_broken_provider_is_still_healthy_at_its_own_limit(self):
        samples = {"telegram": [f"telegram:-100:{index}" for index in range(2)]}
        responses = [FakeStream(200, {"content-type": "video/mp4"}, [b"\x00"]),
                     FakeStream(500, {}, [b"gone"])]

        code, _client = self.running(responses=responses, samples=samples)

        self.assertEqual(code, 0)
        self.assertIn("1/2 ok -> HEALTHY", self.read_log()[-1])

    def test_every_check_is_followed_by_a_pause(self):
        samples = {"telegram": ["telegram:-100:1", "telegram:-100:2"]}

        self.running(samples=samples)

        self.assertEqual(self.module.time.sleeps, [1.0, 1.0])

    def test_the_database_connection_is_closed(self):
        self.running()

        self.assertTrue(self.connection.closed)

    def test_a_restricted_provider_list_keeps_only_what_was_asked_for(self):
        code, _client = self.running("--provider", "youtube",
                                     providers=("telegram", "youtube"),
                                     samples={"youtube": ["youtube:abc"]})

        self.assertEqual(code, 0)
        self.assertIn("[youtube] 1/1 ok -> HEALTHY", self.read_log()[-1])
        self.assertNotIn("[telegram]", "\n".join(self.read_log()))

    def test_a_provider_filter_that_matches_nothing_leaves_nothing_to_check(self):
        code, _client = self.running("--provider", "pctv")

        self.assertEqual(code, 2)
        self.assertTrue(self.logs.has("No providers found in DB", "error"))

    def test_the_log_starts_from_empty_for_every_run(self):
        self.log_path.write_text("stale run\n", encoding="utf-8")

        self.running()

        self.assertNotIn("stale run", "\n".join(self.read_log()))
        self.assertEqual(len(self.read_log()), 2)

    def test_the_log_directory_is_created_when_it_is_missing(self):
        self.module.LINKCHECK_LOG_PATH = self.log_path.parent / "deeper" / "linkcheck.log"

        code, _client = self.running()

        self.assertEqual(code, 0)
        self.assertTrue(self.module.LINKCHECK_LOG_PATH.is_file())

    def test_an_unusable_log_path_is_reported_and_the_run_continues(self):
        self.module.LINKCHECK_LOG_PATH = self.log_path.parent

        code, _client = self.running()

        self.assertEqual(code, 0)
        self.assertTrue(self.logs.has("Could not initialize linkcheck log", "error"))

    def test_every_provider_on_the_menu_is_checked_in_order(self):
        samples = {"telegram": ["telegram:-100:1"], "youtube": ["youtube:abc"]}
        responses = [FakeStream(200, {"content-type": "video/mp4"}, [b"\x00"])] * 2

        code, _client = self.running(providers=("telegram", "youtube"), samples=samples,
                                     responses=responses)

        self.assertEqual(code, 0)
        self.assertIn("[telegram]", "\n".join(self.read_log()))
        self.assertIn("[youtube]", "\n".join(self.read_log()))


if __name__ == "__main__":
    unittest.main()
