"""A Telegram pHash attempt: frames extraction, thumbnails, and dependency verification."""

import importlib.util
import io
import subprocess
import sys
import types
import unittest
from unittest.mock import patch

from PIL import Image

from common.telegram_transfer_budget import (
    DailyTelegramTransferBudgetExceeded,
    DailyTelegramTransferBudgetUnavailable,
)

try:
    from tests.test_video_fingerprints_serving import (
        MODULE_PATH,
        PREFETCH,
        SYNC_DIR,
        Completed,
        FakeTime,
        FingerprintHarness,
        ScriptedAnswers,
        argument,
        frame_payload,
        jpeg_payload,
    )
except ImportError:
    from test_video_fingerprints_serving import (
        MODULE_PATH,
        PREFETCH,
        SYNC_DIR,
        Completed,
        FakeTime,
        FingerprintHarness,
        ScriptedAnswers,
        argument,
        frame_payload,
        jpeg_payload,
    )


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
    """The module, loaded as ``sync.video_fingerprints``, reaches its MP4 helpers as a sibling."""

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
