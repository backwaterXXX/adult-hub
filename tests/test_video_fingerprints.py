import unittest
import sys
import tempfile
import types
from unittest import mock
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync"))
from video_fingerprints import FingerprintBudget, PREFETCH_BYTES, _stash_phash, _stash_sample_times
from fingerprint_mp4 import _FaststartMP4, _top_level_layout


class TestVideoFingerprints(unittest.TestCase):
    def test_sample_times_match_stash_positions(self):
        samples = _stash_sample_times(2432.0)
        self.assertEqual(len(samples), 25)
        self.assertEqual(samples[0], 121.60000000000001)
        self.assertAlmostEqual(samples[1] - samples[0], 87.552)
        self.assertAlmostEqual(samples[-1], 2222.848)

    def test_front_loaded_moov_larger_than_probe_is_passthrough(self):
        total_size = 200_000
        moov_size = 100_000
        prefix = (
            (16).to_bytes(4, "big") + b"ftyp" + b"isom0000"
            + (8).to_bytes(4, "big") + b"free"
            + moov_size.to_bytes(4, "big") + b"moov"
        )

        class Budget:
            def __init__(self):
                self.total_size = total_size
                self.bytes_read = 0
                self.fetches = []

            def fetch(self, start, end):
                self.fetches.append((start, end))
                data = bytearray(end - start + 1)
                data[:len(prefix)] = prefix
                return end, bytes(data)

        self.assertEqual(_top_level_layout(prefix), (24, (24, moov_size)))
        budget = Budget()
        virtual = _FaststartMP4(budget)
        self.assertTrue(virtual.passthrough)
        self.assertEqual(budget.fetches, [(0, 65_535)])
        _, passthrough = virtual.fetch(0, 31)
        self.assertEqual(passthrough[:len(prefix)], prefix)

    def test_padding_atom_larger_than_probe_skips_to_mdat(self):
        total_size = 500_000
        free_size = 100_000
        mdat_offset = 16 + free_size

        class Budget:
            def __init__(self):
                self.total_size = total_size
                self.bytes_read = 0
                self.fetches = []

            def fetch(self, start, end):
                self.fetches.append((start, end))
                buf = bytearray(end - start + 1)
                if start == 0:
                    prefix = (16).to_bytes(4, "big") + b"ftyp" + b"isom0000" + free_size.to_bytes(4, "big") + b"free"
                    buf[:len(prefix)] = prefix
                if start <= mdat_offset <= end:
                    rel = mdat_offset - start
                    buf[rel:rel + 8] = (200_000).to_bytes(4, "big") + b"mdat"
                return end, bytes(buf)

        budget = Budget()
        offset, front_moov = _top_level_layout(budget.fetch(0, 65_535)[1], budget)
        self.assertEqual(offset, mdat_offset)
        self.assertIsNone(front_moov)

    def test_rejects_front_loaded_moov_that_consumes_whole_file(self):
        class Budget:
            total_size = 10_000

            def fetch(self, start, end):
                data = (16).to_bytes(4, "big") + b"ftyp" + b"isom0000"
                data += (9_984).to_bytes(4, "big") + b"moov"
                return end, data.ljust(end - start + 1, b"\0")

        with self.assertRaisesRegex(ValueError, "Invalid front-loaded MP4 moov atom"):
            _FaststartMP4(Budget())

    def test_phash_uses_the_pinned_stash_implementation(self):
        package = types.ModuleType("stash_phash")
        implementation = types.ModuleType("stash_phash.phash")
        implementation.perception_hash = mock.Mock(return_value=0x1234567890ABCDEF)
        package.phash = implementation
        image = Image.new("RGBA", (800, 450), (10, 20, 30, 255))
        with mock.patch.dict(sys.modules, {
            "stash_phash": package, "stash_phash.phash": implementation,
        }):
            self.assertEqual(_stash_phash(image), "1234567890abcdef")
        implementation.perception_hash.assert_called_once_with(image)

    def test_gateway_never_exceeds_remaining_budget(self):
        class Response:
            status_code = 206

            def __init__(self, start, end, total):
                self.headers = {"content-range": f"bytes {start}-{end}/{total}"}
                self.data = b"x" * (end - start + 1)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def iter_bytes(self):
                yield self.data

        class Client:
            def stream(self, _method, _url, *, headers, timeout):
                start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
                return Response(start, end, 100_000)

        budget = FingerprintBudget(
            "telegram:-1001234567890:42:999", budget=1024,
        )
        budget.total_size = 100_000
        budget.http_client = Client()
        end, data = budget.fetch(0, 99_999)
        self.assertEqual(end, 1023)
        self.assertEqual(len(data), 1024)
        self.assertEqual(budget.bytes_read, 1024)
        cached_end, cached_data = budget.fetch(0, 1023)
        self.assertEqual((cached_end, len(cached_data), budget.bytes_read), (1023, 1024, 1024))
        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            budget.fetch(1024, 2047)

    def test_gateway_prefetches_playback_sized_ranges_and_reuses_them(self):
        class Response:
            status_code = 206

            def __init__(self, start, end, total):
                self.headers = {"content-range": f"bytes {start}-{end}/{total}"}
                self.data = b"x" * (end - start + 1)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def iter_bytes(self):
                yield self.data

        class Client:
            def __init__(self):
                self.ranges = []

            def stream(self, _method, _url, *, headers, timeout):
                start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
                self.ranges.append((start, end))
                return Response(start, end, PREFETCH_BYTES * 2)

        budget = FingerprintBudget(
            "telegram:-1001234567890:42:999", budget=PREFETCH_BYTES * 2,
        )
        budget.total_size = PREFETCH_BYTES * 2
        client = Client()
        budget.http_client = client

        end, first = budget.fetch(0, 65_535)
        self.assertEqual((end, len(first)), (65_535, 65_536))
        end, second = budget.fetch(65_536, 131_071)
        self.assertEqual((end, len(second)), (131_071, 65_536))
        self.assertEqual(client.ranges, [(0, PREFETCH_BYTES - 1)])
        self.assertEqual(budget.bytes_read, PREFETCH_BYTES)

    def test_disk_cache_and_transfer_ledger_survive_a_new_attempt(self):
        class Response:
            status_code = 206

            def __init__(self, start, end, total):
                self.headers = {"content-range": f"bytes {start}-{end}/{total}"}
                self.data = bytes([start // PREFETCH_BYTES % 256]) * (end - start + 1)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def iter_bytes(self):
                yield self.data

        class Client:
            def __init__(self):
                self.ranges = []

            def stream(self, _method, _url, *, headers, timeout):
                start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
                self.ranges.append((start, end))
                return Response(start, end, PREFETCH_BYTES * 3)

        with tempfile.TemporaryDirectory() as cache_dir:
            first = FingerprintBudget(
                "telegram:-100:1:42", budget=PREFETCH_BYTES * 2,
                cache_dir=cache_dir, cache_id="job-1",
            )
            first.total_size = PREFETCH_BYTES * 3
            first.http_client = Client()
            first.fetch(0, 65_535)
            first.fetch(65_536, 131_071)
            self.assertEqual(first.bytes_read, PREFETCH_BYTES)

            second_client = Client()
            resumed = FingerprintBudget(
                "telegram:-100:1:42", budget=PREFETCH_BYTES * 2,
                cache_dir=cache_dir, cache_id="job-1",
            )
            resumed.total_size = PREFETCH_BYTES * 3
            resumed.http_client = second_client
            _end, data = resumed.fetch(0, 65_535)
            self.assertEqual(len(data), 65_536)
            self.assertEqual(resumed.bytes_read, PREFETCH_BYTES)
            self.assertEqual(second_client.ranges, [])

            resumed.fetch(PREFETCH_BYTES, PREFETCH_BYTES + 65_535)
            self.assertEqual(resumed.bytes_read, PREFETCH_BYTES * 2)
            self.assertEqual(second_client.ranges, [(PREFETCH_BYTES, 2 * PREFETCH_BYTES - 1)])
            with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
                resumed.fetch(2 * PREFETCH_BYTES, 2 * PREFETCH_BYTES + 65_535)

            fresh_job = FingerprintBudget(
                "telegram:-100:1:42", budget=PREFETCH_BYTES,
                cache_dir=cache_dir, cache_id="job-2",
            )
            self.assertEqual(fresh_job.bytes_read, 0)


if __name__ == "__main__":
    unittest.main()
