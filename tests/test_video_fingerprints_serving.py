"""A Telegram pHash attempt: what it downloads, what FFmpeg is asked for, what it gives up on."""

import contextlib
import hashlib
import http.client
import http.server
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from common.telegram_transfer_budget import (
    DailyTelegramTransferBudgetExceeded,
    DailyTelegramTransferBudgetUnavailable,
)

MODULE_PATH = Path(__file__).resolve().parents[1] / "sync" / "video_fingerprints.py"
SYNC_DIR = MODULE_PATH.parent

PREFETCH = 128 * 1024
SIZE = 300_000
FTYP = (20).to_bytes(4, "big") + b"ftyp" + b"isom\x00\x00\x02\x00isom"
FRONT_MOOV = (1000).to_bytes(4, "big") + b"moov" + bytes(992)
SOURCE = (FTYP + FRONT_MOOV).ljust(SIZE, b"\x00")
VIDEO_ID = "telegram:-100:1:42"


def frame_payload(width=160, height=88, colour=(10, 20, 30)):
    handle = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(handle, format="BMP")
    return handle.getvalue()


def jpeg_payload(width=640, height=360, colour=(200, 120, 40)):
    handle = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(handle, format="JPEG")
    return handle.getvalue()


def argument(argv, flag):
    return argv[argv.index(flag) + 1]


class Completed:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeTime:
    """A monotonic clock the test walks forward one scripted moment at a time."""

    def __init__(self, moments=(0.0,), clock=1_767_225_600.0):
        self.moments = [float(moment) for moment in moments]
        self.clock = clock

    def monotonic(self):
        if len(self.moments) > 1:
            return self.moments.pop(0)
        return self.moments[0]

    def time(self):
        return self.clock


class FakeResponse:
    def __init__(self, status_code, headers, chunks):
        self.status_code = status_code
        self.headers = headers
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"resolver HTTP {self.status_code}")

    def iter_bytes(self):
        yield from self.chunks


class FakeResolver:
    """The resolver a fingerprint attempt talks to: one size, then bounded byte ranges."""

    def __init__(self, source=SOURCE):
        self.source = source
        self.requests = []
        self.status_code = 206
        self.content_range = "echo"
        self.content_length = None
        self.drop_bytes = 0
        self.extra_bytes = 0
        self.chunks = 1
        self.error = None
        self.head_error = None

    def stream(self, method, url, headers=None, timeout=None):
        self.requests.append((method, url, dict(headers or {}), timeout))
        if method == "HEAD":
            if self.head_error is not None:
                raise self.head_error
            length = len(self.source) if self.content_length is None else self.content_length
            return FakeResponse(200, {"content-length": str(length)}, [])
        if self.error is not None:
            raise self.error
        start, end = self.ranged(headers)
        payload = self.source[start:end + 1]
        if self.extra_bytes:
            payload += b"\x00" * self.extra_bytes
        if self.drop_bytes:
            payload = payload[:-self.drop_bytes]
        if self.content_range == "echo":
            echoed = f"bytes {start}-{end}/{len(self.source)}"
        else:
            echoed = self.content_range
        response_headers = {} if echoed is None else {"content-range": echoed}
        return FakeResponse(self.status_code, response_headers, self.split(payload))

    @staticmethod
    def ranged(headers):
        start, end = headers["Range"].removeprefix("bytes=").split("-")
        return int(start), int(end)

    def split(self, payload):
        if self.chunks == 1:
            return [payload]
        edges = [index * len(payload) // self.chunks for index in range(self.chunks + 1)]
        return [payload[edges[index]:edges[index + 1]] for index in range(self.chunks)]

    def requested_ranges(self):
        return [self.ranged(headers)
                for _method, _url, headers, _timeout in self.requests if "Range" in headers]


class FlakyOS:
    """Real filesystem calls with the n-th rename set to fail half way."""

    def __init__(self, *, fail_on=None):
        self.fail_on = fail_on
        self.renames = 0

    def __getattr__(self, name):
        return getattr(os, name)

    def replace(self, source, target):
        self.renames += 1
        if self.fail_on == self.renames:
            raise OSError("rename interrupted")
        return os.replace(source, target)


class FakeLedger:
    """``reserve_telegram_fingerprint_bytes`` without the daily allowance file."""

    def __init__(self, *, error=None, fail_on=None):
        self.error = error
        self.fail_on = fail_on
        self.charges = []

    def __call__(self, byte_count, **kwargs):
        self.charges.append((byte_count, kwargs))
        if self.error is None:
            return
        if self.fail_on is None or len(self.charges) == self.fail_on:
            raise self.error


class FakeSubprocess:
    """FFmpeg stand-in: answers each seek from a script and may touch the range server first."""

    def __init__(self):
        self.PIPE = "pipe"
        self.TimeoutExpired = subprocess.TimeoutExpired
        self.frames = []
        self.thumbs = []
        self.calls = []
        self.probe_range = None

    def run(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        self.calls.append((argv, kwargs))
        if self.probe_range is not None and "-i" in argv:
            self.fetch(argument(argv, "-i"))
        return self.answer("thumbs" if "mjpeg" in argv else "frames")

    def answer(self, which):
        queue = getattr(self, which)
        outcome = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def fetch(self, video_url):
        """Act like FFmpeg for one request: read whatever the server is willing to give."""
        authority_path = video_url.partition("://")[2]
        authority, _, path = authority_path.partition("/")
        start, end = self.probe_range
        connection = http.client.HTTPConnection(authority, timeout=5)
        try:
            connection.request("GET", f"/{path}", headers={"Range": f"bytes={start}-{end}"})
            connection.getresponse().read()
        except Exception:
            pass
        finally:
            connection.close()


class ScriptedAnswers:
    """A queue that repeats its last entry and can raise the exceptions it was handed."""

    def __init__(self, values=(), *, default=False):
        self.values = list(values)
        self.default = default

    def next(self):
        if not self.values:
            return self.default
        answer = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeImageChecks:
    """``common.image_utils`` with answers the test scripts frame by frame."""

    def __init__(self):
        self.black = ScriptedAnswers()
        self.bumper = ScriptedAnswers()
        self.seen = []

    def is_black_image(self, image):
        self.seen.append(("black", image.size))
        return self.black.next()

    def compute_dhash(self, image):
        self.seen.append(("dhash", image.size))
        return "0" * 16

    def is_bumper_hash(self, digest, hashes, max_distance=4):
        self.seen.append(("bumper", len(hashes), max_distance))
        return self.bumper.next()


class FakeThumbnails:
    """The two thumbnail helpers from ``sync/thumbnails.py``, with scriptable answers."""

    def __init__(self):
        self.bumper_hashes = ScriptedAnswers(default=set())
        self.valid = ScriptedAnswers(default=True)
        self.calls = []

    def _get_cached_bumper_hashes(self):
        return self.bumper_hashes.next()

    def _valid_thumbnail_bytes(self, data, url, bumper_hashes=None, minimum_size=None):
        self.calls.append((len(data), url, len(bumper_hashes or ()), minimum_size))
        return self.valid.next()


class FingerprintHarness(unittest.TestCase):
    """One freshly executed copy of ``sync/video_fingerprints.py`` per test, with no real network."""

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        temp = tempfile.TemporaryDirectory(prefix="adult_hub_phash_")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stack.enter_context(patch.object(sys, "path", [str(SYNC_DIR), *sys.path]))

        self.resolver = FakeResolver()
        self.ledger = FakeLedger()
        self.subprocess = FakeSubprocess()
        self.subprocess.frames = [Completed(stdout=frame_payload())]
        self.subprocess.thumbs = [Completed(stdout=jpeg_payload())]
        self.image_checks = FakeImageChecks()
        self.thumbnails = FakeThumbnails()
        self.time = FakeTime()
        self.sprites = []
        self.servers = []
        self.teardowns = []
        self.module = self.load()

    def load(self):
        spec = importlib.util.spec_from_file_location("video_fingerprints_under_test", MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.stack.enter_context(patch.dict(sys.modules, self.stubbed_modules()))
        module.httpx = self.resolver
        module.time = self.time
        module.subprocess = self.subprocess
        module.reserve_telegram_fingerprint_bytes = self.ledger
        module.http = types.SimpleNamespace(server=types.SimpleNamespace(
            ThreadingHTTPServer=self.listening_server))
        self.module = module
        return module

    def stubbed_modules(self):
        stash = types.ModuleType("stash_phash")
        video = types.ModuleType("stash_phash.video")
        phash = types.ModuleType("stash_phash.phash")

        def combine_images(images):
            self.sprites.append(len(images))
            return f"sprite-of-{len(images)}"

        video.combine_images = combine_images
        phash.perception_hash = lambda image: 0x1234_5678_90ab_cdef
        stash.video = video
        stash.phash = phash
        image_utils = types.ModuleType("common.image_utils")
        image_utils.compute_dhash = self.image_checks.compute_dhash
        image_utils.is_black_image = self.image_checks.is_black_image
        image_utils.is_bumper_hash = self.image_checks.is_bumper_hash
        thumbnails = types.ModuleType("thumbnails")
        thumbnails._get_cached_bumper_hashes = self.thumbnails._get_cached_bumper_hashes
        thumbnails._valid_thumbnail_bytes = self.thumbnails._valid_thumbnail_bytes
        return {"stash_phash": stash, "stash_phash.video": video, "stash_phash.phash": phash,
                "common.image_utils": image_utils, "thumbnails": thumbnails}

    def listening_server(self, address, handler):
        """The real loopback server, remembered, with its teardown calls recorded."""
        server = http.server.ThreadingHTTPServer(address, handler)
        for name in ("shutdown", "server_close"):
            original = getattr(server, name)

            def watched(name=name, original=original):
                self.teardowns.append(name)
                return original()

            setattr(server, name, watched)
        self.servers.append(server)
        return server

    def budget(self, video_id=VIDEO_ID, *, budget=4 * PREFETCH, **kwargs):
        kwargs.setdefault("cache_dir", None)
        return self.module.FingerprintBudget(video_id, budget=budget, **kwargs)

    def cached_budget(self, video_id=VIDEO_ID, cache_id="job-1", **kwargs):
        return self.budget(video_id, cache_dir=str(self.root), cache_id=cache_id, **kwargs)

    def cache_dir_for(self, cache_id="job-1", video_id=VIDEO_ID):
        identity = hashlib.sha256(f"{cache_id}\0{video_id}".encode("utf-8")).hexdigest()
        return self.root / identity

    def phash(self, video_id=VIDEO_ID, duration=2432, **kwargs):
        return self.module.telegram_phash(video_id, duration, **kwargs)

    def sample_times(self, duration=2432):
        return self.module._stash_sample_times(duration)


class TestStreamAddress(FingerprintHarness):
    def test_the_proxy_address_is_built_under_the_admin_prefix(self):
        plain = self.budget(resolver_url="http://gateway:8090")
        prefixed = self.budget(resolver_url="http://gateway:8090/admin/")

        self.assertEqual(plain.stream_url,
                         f"http://gateway:8090/admin/play/{VIDEO_ID}/stream?mode=proxy")
        self.assertEqual(prefixed.base_url, "http://gateway:8090/admin")

    def test_the_size_probe_asks_the_resolver_once(self):
        budget = self.budget()

        self.assertEqual(budget.load_size(), SIZE)
        self.assertEqual(budget.total_size, SIZE)
        self.assertEqual(self.resolver.requests, [("HEAD", budget.stream_url, {}, 15.0)])

    def test_a_response_without_a_usable_size_is_refused(self):
        for size in ("abc", ""):
            with self.subTest(size=size):
                self.resolver.content_length = size
                with self.assertRaisesRegex(ValueError, "did not provide a video size"):
                    self.budget().load_size()

    def test_a_dead_size_probe_is_not_translated(self):
        self.resolver.head_error = ConnectionResetError("resolver is gone")

        with self.assertRaises(ConnectionResetError):
            self.budget().load_size()


class TestRangeGateway(FingerprintHarness):
    def test_a_read_starts_with_a_playback_sized_prefetch(self):
        budget = self.budget()

        end, data = budget.fetch(0, 31)

        self.assertEqual((end, len(data)), (31, 32))
        self.assertEqual(self.resolver.requested_ranges(), [(0, PREFETCH - 1)])
        self.assertEqual(self.ledger.charges[0][0], PREFETCH)
        self.assertEqual(budget.bytes_read, PREFETCH)
        self.assertEqual(budget.fetch(0, 31), (31, data))

    def test_the_first_read_asks_the_resolver_for_the_size_it_lacks(self):
        budget = self.budget()

        budget.fetch(0, 31)

        self.assertEqual([method for method, *_ in self.resolver.requests], ["HEAD", "GET"])

    def test_a_short_file_is_clamped_to_its_own_length(self):
        self.resolver.source = SOURCE[:1000]

        end, data = self.budget().fetch(900, 999)

        self.assertEqual((end, len(data)), (999, 100))

    def test_a_range_outside_the_file_is_refused(self):
        budget = self.budget()

        for start, end in ((-1, 10), (SIZE, SIZE + 10), (100, 50)):
            with self.subTest(range=(start, end)):
                with self.assertRaisesRegex(ValueError, "Invalid Telegram byte range"):
                    budget.fetch(start, end)

    def test_a_response_that_is_not_a_partial_content_is_refused(self):
        self.resolver.status_code = 200

        with self.assertRaisesRegex(ValueError, "did not honor a bounded byte range"):
            self.budget().fetch(0, 31)

    def test_a_response_without_a_byte_range_header_is_refused(self):
        self.resolver.content_range = None

        with self.assertRaisesRegex(ValueError, "unexpected byte range"):
            self.budget().fetch(0, 31)

    def test_a_response_for_other_bytes_than_were_asked_is_refused(self):
        self.resolver.content_range = f"bytes 4096-8191/{SIZE}"

        with self.assertRaisesRegex(ValueError, "unexpected byte range"):
            self.budget().fetch(0, 31)

    def test_a_response_that_streams_past_the_requested_range_is_refused(self):
        self.resolver.extra_bytes = 16

        with self.assertRaisesRegex(ValueError, "exceeded the requested range"):
            self.budget().fetch(0, 31)

    def test_a_truncated_response_is_refused(self):
        self.resolver.drop_bytes = 16

        with self.assertRaisesRegex(ValueError, "incomplete byte range"):
            self.budget().fetch(0, 31)

    def test_a_response_split_into_pieces_is_still_one_block(self):
        self.resolver.chunks = 5

        _end, data = self.budget().fetch(0, 31)

        self.assertEqual(len(data), 32)

    def test_a_dead_resolver_surfaces_instead_of_retrying(self):
        self.resolver.error = RuntimeError("connection reset by peer")

        with self.assertRaisesRegex(RuntimeError, "connection reset"):
            self.budget().fetch(0, 31)


class TestTransferCaps(FingerprintHarness):
    def test_a_memory_cache_that_cannot_continue_ends_the_read(self):
        budget = self.budget(budget=PREFETCH)
        budget.fetch(0, 31)

        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            budget.fetch(PREFETCH, PREFETCH + 31)

    def test_a_persisted_cache_refuses_to_write_a_short_final_block(self):
        budget = self.cached_budget(budget=PREFETCH + 32)
        budget.fetch(0, 31)

        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            budget.fetch(PREFETCH, PREFETCH + 31)

    def test_a_persisted_cache_stops_once_the_cap_is_full(self):
        budget = self.cached_budget(budget=PREFETCH)
        budget.fetch(0, 31)

        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            budget.fetch(PREFETCH, PREFETCH + 31)

    def test_a_denied_reservation_ends_the_read(self):
        budget = self.budget()
        budget._reserve = lambda _count: False

        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            budget.fetch(0, 31)

    def test_a_reservation_the_cap_has_no_room_for_is_denied(self):
        budget = self.budget(budget=1024)
        budget.bytes_read = 512

        self.assertFalse(budget._reserve(1024))
        self.assertEqual(self.ledger.charges, [])

    def test_a_callable_rate_limit_is_resolved_and_kept_at_one_byte_per_second(self):
        self.budget(max_bytes_per_second=lambda: 4096).fetch(0, 31)
        self.budget(max_bytes_per_second=lambda: 0).fetch(0, 31)

        self.assertEqual(self.ledger.charges[0][1],
                         {"max_bytes_per_second": 4096, "allow_rate_above_configured": False})
        self.assertEqual(self.ledger.charges[-1][1]["max_bytes_per_second"], 1)

    def test_an_oversized_rate_can_be_allowed_explicitly(self):
        self.budget(max_bytes_per_second=9_000, allow_rate_above_configured=True).fetch(0, 31)

        self.assertEqual(self.ledger.charges[0][1],
                         {"max_bytes_per_second": 9000, "allow_rate_above_configured": True})


class TestTransferLedgerFiles(FingerprintHarness):
    def test_a_fresh_attempt_resumes_from_the_persisted_charge(self):
        first = self.cached_budget()
        first.fetch(0, 31)
        with open(first.usage_path, encoding="utf-8") as handle:
            ledger = json.load(handle)

        self.assertEqual(ledger["bytes_reserved"], PREFETCH)
        self.assertEqual(self.cached_budget().bytes_read, PREFETCH)
        self.assertEqual([name for name in os.listdir(first.cache_dir)
                          if name.endswith(".range")], ["0.range"])

    def test_a_tampered_ledger_is_refused(self):
        budget = self.cached_budget()
        payloads = ("[]", '{"bytes_reserved": "all"}', '{"bytes_reserved": -1}',
                    '{"bytes_reserved": 10}', '{"updated_at": "yesterday"}')
        for payload in payloads:
            with self.subTest(payload=payload):
                with open(budget.usage_path, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                with self.assertRaisesRegex(ValueError, "Invalid pHash transfer ledger"):
                    self.cached_budget()

    def test_a_failed_ledger_rename_leaves_no_temp_file_behind(self):
        self.module.os = FlakyOS(fail_on=1)

        with self.assertRaises(OSError):
            self.cached_budget()

        self.assertEqual(os.listdir(self.cache_dir_for()), [])

    def test_a_failed_block_rename_leaves_no_temp_file_behind(self):
        budget = self.cached_budget()
        self.module.os = FlakyOS(fail_on=2)

        with self.assertRaises(OSError):
            budget.fetch(0, 31)

        self.assertEqual(os.listdir(budget.cache_dir), ["usage.json"])

    def test_a_persisted_block_bigger_than_its_own_slot_is_refused(self):
        budget = self.cached_budget()
        budget.total_size = PREFETCH + 1_000
        with open(os.path.join(budget.cache_dir, f"{PREFETCH}.range"), "wb") as handle:
            handle.write(bytes(PREFETCH + 16))

        with self.assertRaisesRegex(ValueError, "unexpected size"):
            budget.fetch(PREFETCH, PREFETCH + 31)


class TestPhashFrames(FingerprintHarness):
    def test_an_unknown_duration_is_refused_before_anything_is_downloaded(self):
        with self.assertRaisesRegex(ValueError, "known video duration"):
            self.phash(duration=0)

        self.assertEqual(self.resolver.requests, [])

    def test_every_stash_sample_is_extracted_into_the_sprite(self):
        digest = self.phash()

        self.assertEqual(digest, ("1234567890abcdef", PREFETCH))
        self.assertEqual(self.sprites, [25])
        self.assertEqual(len(self.subprocess.calls), 25)
        first = self.subprocess.calls[0][0]
        self.assertEqual([argument(first, flag) for flag in ("-ss", "-vf", "-i")],
                         [str(self.sample_times()[0]), "scale=160:-2",
                          f"http://127.0.0.1:{self.servers[0].server_address[1]}/video.mp4"])
        self.assertEqual([argument(argv, "-ss") for argv, _ in self.subprocess.calls],
                         [str(moment) for moment in self.sample_times()])

    def test_a_frame_of_the_wrong_width_is_an_error(self):
        self.subprocess.frames = [Completed(stdout=frame_payload(width=64))]

        with self.assertRaisesRegex(RuntimeError, "unexpected pHash frame size"):
            self.phash()

    def test_a_failing_frame_without_a_transfer_problem_blames_ffmpeg(self):
        self.subprocess.frames = [Completed(returncode=1, stderr=b"no frame decoded")]

        with self.assertRaisesRegex(RuntimeError, "could not extract pHash frame") as caught:
            self.phash()
        self.assertIn("no frame decoded", str(caught.exception))
        self.assertIn(f"range bytes={PREFETCH}, requests=[]", str(caught.exception))

    def test_the_range_server_streams_from_the_same_budget_as_the_resolver(self):
        self.subprocess.probe_range = (0, 1023)
        self.subprocess.frames = [Completed(returncode=1, stderr=b"seek failed")]

        with self.assertRaisesRegex(RuntimeError, r"requests=\[\(0, 1023\)\]"):
            self.phash()

        server = self.servers[0]
        self.assertEqual(server.request_ranges, [(0, 1023)])
        self.assertIs(server.virtual_mp4.budget, server.fingerprint_budget)
        self.assertIsNone(server.transfer_error)

    def test_an_exhausted_byte_budget_is_named_with_its_size(self):
        self.subprocess.probe_range = (PREFETCH, PREFETCH + 31)
        self.subprocess.frames = [Completed(returncode=2, stderr=b"")]

        with self.assertRaisesRegex(RuntimeError,
                                    r"exhausted its 131072-byte transfer budget "
                                    r"after reserving 131072 bytes"):
            self.phash(byte_budget=PREFETCH)

    def test_a_blocked_transfer_ledger_is_forwarded_to_the_caller(self):
        self.subprocess.probe_range = (PREFETCH, PREFETCH + 31)
        self.subprocess.frames = [Completed(returncode=2, stderr=b"")]
        self.ledger.error = DailyTelegramTransferBudgetUnavailable(
            "The pHash fingerprint transfer ledger is unavailable")
        self.ledger.fail_on = 2

        with self.assertRaisesRegex(DailyTelegramTransferBudgetUnavailable,
                                    "fingerprint transfer ledger"):
            self.phash()
        self.assertEqual(len(self.ledger.charges), 2)

    def test_a_spent_daily_allowance_is_forwarded_to_the_caller(self):
        self.subprocess.probe_range = (PREFETCH, PREFETCH + 31)
        self.subprocess.frames = [Completed(returncode=2, stderr=b"")]
        self.ledger.error = DailyTelegramTransferBudgetExceeded(
            "Telegram fingerprint daily transfer limit reached")
        self.ledger.fail_on = 2

        with self.assertRaisesRegex(DailyTelegramTransferBudgetExceeded,
                                    "daily transfer limit"):
            self.phash()
        self.assertEqual(len(self.ledger.charges), 2)

    def test_a_frame_that_outruns_ffmpeg_is_a_timeout(self):
        self.subprocess.frames = [subprocess.TimeoutExpired(cmd="ffmpeg", timeout=120)]

        with self.assertRaisesRegex(TimeoutError,
                                    r"timed out extracting pHash frame at 121\.6s "
                                    r"after reading 131072 bytes"):
            self.phash()

    def test_the_whole_attempt_stops_at_its_deadline(self):
        self.module.time = FakeTime(moments=[0.0, 0.0, 10_000.0])

        with self.assertRaisesRegex(TimeoutError, "exceeded its time limit"):
            self.phash(timeout_seconds=1200)
        self.assertEqual(len(self.subprocess.calls), 1)

    def test_each_frame_gets_the_time_left_but_never_more_than_two_minutes(self):
        self.module.time = FakeTime(moments=[0.0, 0.0, 0.0, 599.5])

        self.phash(timeout_seconds=600)

        self.assertEqual([kwargs["timeout"] for _, kwargs in self.subprocess.calls[:2]],
                         [120.0, 1.0])
        self.assertEqual(len(self.subprocess.calls), 25)

    def test_the_loopback_server_is_torn_down_even_when_a_frame_fails(self):
        self.subprocess.frames = [Completed(returncode=1, stderr=b"boom")]

        with self.assertRaises(RuntimeError):
            self.phash()

        self.assertEqual(self.teardowns, ["shutdown", "server_close"])
        self.assertEqual(self.servers[0].socket.fileno(), -1)


class TestPhashThumbnails(FingerprintHarness):
    def test_a_candidate_is_decoded_again_and_re_encoded_for_the_poster(self):
        phash, bytes_read, thumbnail = self.phash(include_thumbnail=True)

        self.assertEqual(phash, "1234567890abcdef")
        self.assertEqual(bytes_read, PREFETCH)
        self.assertEqual(Image.open(io.BytesIO(thumbnail)).size, (640, 360))
        self.assertEqual(self.thumbnails.calls[0][1], "phash://frame")
        self.assertEqual(self.thumbnails.calls[0][3], (180, 320))
        thumb = self.subprocess.calls[-1][0]
        self.assertEqual([argument(thumb, flag) for flag in ("-ss", "-vf")],
                         [str(self.sample_times()[12]), "scale='min(640,iw)':-2"])

    def test_the_middle_sample_is_tried_first_and_the_rest_outwards(self):
        self.thumbnails.valid = ScriptedAnswers([False, True])

        thumbnail = self.phash(include_thumbnail=True)[2]

        self.assertIsNotNone(thumbnail)
        sought = [argument(argv, "-ss") for argv, _ in self.subprocess.calls[25:]]
        self.assertEqual([float(value) for value in sought[:2]],
                         [self.sample_times()[12], self.sample_times()[11]])

    def test_black_and_bumper_frames_are_never_offered(self):
        self.image_checks.black = ScriptedAnswers([True, False])
        self.image_checks.bumper = ScriptedAnswers([True])
        self.thumbnails.bumper_hashes = ScriptedAnswers([{1, 2}])

        thumbnail = self.phash(include_thumbnail=True)[2]

        self.assertIsNone(thumbnail)
        self.assertEqual(len(self.subprocess.calls), 25)
        self.assertEqual(self.image_checks.seen[:4],
                         [("black", (160, 88)), ("black", (160, 88)),
                          ("dhash", (160, 88)), ("bumper", 2, 4)])

    def test_a_frame_that_cannot_be_judged_is_still_offered_as_a_poster(self):
        self.image_checks.black = ScriptedAnswers([RuntimeError("stats unavailable")])

        thumbnail = self.phash(include_thumbnail=True)[2]

        self.assertIsNotNone(thumbnail)
        self.assertEqual(len(self.subprocess.calls), 26)

    def test_a_poster_ffmpeg_refuses_to_draw_is_skipped(self):
        self.subprocess.thumbs = [Completed(returncode=1, stderr=b"boom")]

        self.assertIsNone(self.phash(include_thumbnail=True)[2])

    def test_a_poster_smaller_than_the_library_minimum_is_skipped(self):
        self.subprocess.thumbs = [Completed(stdout=jpeg_payload(width=100, height=60))]

        self.assertIsNone(self.phash(include_thumbnail=True)[2])

    def test_a_poster_that_would_be_too_heavy_to_store_is_skipped(self):
        class Inflated(io.BytesIO):
            def getvalue(self):
                return super().getvalue() + b"\x00" * 1_500_001

        self.module.BytesIO = Inflated

        self.assertIsNone(self.phash(include_thumbnail=True)[2])
        self.assertEqual(self.thumbnails.calls, [])

    def test_a_poster_the_library_refuses_is_not_stored(self):
        self.thumbnails.valid = ScriptedAnswers([False])

        self.assertIsNone(self.phash(include_thumbnail=True)[2])
        self.assertEqual(len(self.thumbnails.calls), 25)

    def test_without_the_flag_the_frames_are_never_judged(self):
        self.phash(include_thumbnail=False)

        self.assertEqual(len(self.subprocess.calls), 25)
        self.assertEqual(self.image_checks.seen, [])
        self.assertEqual(self.thumbnails.calls, [])


class TestPhashRequirements(FingerprintHarness):
    def test_a_missing_imaging_dependency_is_reported_before_anything_is_downloaded(self):
        with patch.dict(sys.modules, {"stash_phash.video": None}):
            with self.assertRaisesRegex(RuntimeError, "Pillow is required"):
                self.phash()

        self.assertEqual(self.resolver.requests, [])

    def test_a_missing_ffmpeg_is_reported_before_anything_is_downloaded(self):
        with patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "ffmpeg is required"):
                self.phash()

        self.assertEqual(self.resolver.requests, [])


class TestBothDeploymentLayouts(FingerprintHarness):
    """The images install this file as a flat script; the repository can import it as a package."""

    def test_the_script_layout_reaches_the_mp4_helpers_by_name(self):
        self.assertEqual(self.module.__package__, "")
        self.assertEqual(self.module._fingerprint_mp4.__name__, "fingerprint_mp4")

    def test_the_package_layout_reaches_the_mp4_helpers_as_a_sibling(self):
        package = types.ModuleType("sync")
        package.__path__ = [str(SYNC_DIR)]

        with patch.dict(sys.modules, {"sync": package}):
            spec = importlib.util.spec_from_file_location("sync.video_fingerprints", MODULE_PATH)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

        self.assertEqual(module.__package__, "sync")
        self.assertEqual(module._fingerprint_mp4.__name__, "sync.fingerprint_mp4")
        self.assertIs(module._RangeHandler, module._fingerprint_mp4._RangeHandler)


if __name__ == "__main__":
    unittest.main()
