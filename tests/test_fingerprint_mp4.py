"""The bounded range server and the virtual faststart view over a tail-moov MP4."""

import email.message
import struct
import types
import unittest


from sync.fingerprint_mp4 import (
    TELEGRAM_BLOCK_BYTES,
    _FaststartMP4,
    _RangeHandler,
    _box_header,
    _relocated_moov,
    _top_level_layout,
)


def atom(kind: bytes, payload: bytes = b"", *, size: int = None) -> bytes:
    """One MP4 atom; ``size`` overrides the header so truncated or padded boxes can be built."""
    total = (8 + len(payload)) if size is None else size
    return total.to_bytes(4, "big") + kind + payload


def atom64(kind: bytes, payload: bytes = b"") -> bytes:
    """One atom with the 64-bit size header: marker ``1``, real size in bytes 8-16."""
    return atom(kind, struct.pack(">Q", 16 + len(payload)) + payload, size=1)


def offset_table(kind: bytes, offsets, width: int) -> bytes:
    body = b"".join(value.to_bytes(width, "big") for value in offsets)
    return atom(kind, b"\x00" * 4 + len(offsets).to_bytes(4, "big") + body)


def stco(offsets) -> bytes:
    return offset_table(b"stco", offsets, 4)


def co64(offsets) -> bytes:
    return offset_table(b"co64", offsets, 8)


def moov(*, offsets=(1_000,), table: bytes = b"stco") -> bytes:
    """A moov carrying one chunk-offset table, wrapped in the usual container chain."""
    stbl = atom(b"stbl", offset_table(table, offsets, 4 if table == b"stco" else 8))
    return atom(b"moov", atom(b"mvhd", b"\x00" * 8) + atom(b"trak", atom(b"mdia", atom(b"minf", stbl))))


def ftyp() -> bytes:
    return atom(b"ftyp", b"isom\x00\x00\x02\x00isom")


def chunk_offsets(relocated: bytes, kind: bytes = b"stco", width: int = 4):
    """The entries of the first offset table inside a moov, as the reader would see them."""
    table = relocated[relocated.find(kind) + 4:]
    count = int.from_bytes(table[4:8], "big")
    return [int.from_bytes(table[8 + index * width:8 + (index + 1) * width], "big")
            for index in range(count)]


def header_of(*atoms: bytes) -> bytes:
    return b"".join(atoms)


class VirtualFile:
    """The ``FingerprintBudget`` surface this module uses: bounded range reads over a byte image."""

    def __init__(self, data: bytes = b"", *, budget: int = 1 << 30):
        self.data = data
        self.budget = budget
        self.bytes_read = 0
        self.total_size = len(data)
        self.requests = []
        self.size_reads = 0
        self.reader = None
        self.short_read = None

    def load_size(self) -> int:
        self.size_reads += 1
        return len(self.data)

    def fetch(self, start: int, end: int):
        self.requests.append((start, end))
        if self.reader is not None:
            return self.reader(start, end)
        data = self.data[start:end + 1]
        if self.short_read is not None:
            data = data[:self.short_read]
        self.bytes_read += len(data)
        return start + len(data) - 1, bytes(data)


class EmptyFile(VirtualFile):
    """An upstream that answers with nothing, the way an exhausted budget does."""

    def fetch(self, start, end):
        self.requests.append((start, end))
        return start - 1, b""


class RecordingSocket:
    """Where a response is written: keeps the bytes and can fail like a client that hung up."""

    def __init__(self, *, error: BaseException = None):
        self.payload = bytearray()
        self.error = error
        self.flushes = 0

    def write(self, data):
        if self.error is not None and self.payload.endswith(b"\r\n\r\n"):
            raise self.error  # the client hung up once the headers were on the wire
        self.payload.extend(data)

    def flush(self):
        self.flushes += 1

    @property
    def headers(self):
        head, _, _body = self.payload.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        return lines[0], dict(line.split(": ", 1) for line in lines[1:] if ": " in line)

    @property
    def body(self):
        _head, _, body = self.payload.partition(b"\r\n\r\n")
        return bytes(body)


class Mp4Harness(unittest.TestCase):
    """Build byte images and read them the way the daemon does."""

    def layout(self, header: bytes, budget=None):
        return _top_level_layout(header, budget)

    def virtual(self, data: bytes, *, budget: int = 1 << 30):
        return _FaststartMP4(VirtualFile(data, budget=budget))


class RangeHandlerHarness(unittest.TestCase):
    """Drive one request through the handler without a socket, a thread or FFmpeg."""

    def setUp(self):
        self.file = VirtualFile()

    def serve(self, data: bytes = b"", *, budget: int = 1 << 30, virtual=None, reader=None):
        self.file = VirtualFile(data, budget=budget)
        if reader is not None:
            self.file.reader = reader
        self.mp4 = virtual or self.file
        self.server = types.SimpleNamespace(
            fingerprint_budget=self.file, virtual_mp4=self.mp4, request_ranges=[], transfer_error=None)
        return self.server

    def handler(self, *, method: str = "GET", range_header=None, wfile=None):
        headers = email.message.Message()
        if range_header is not None:
            headers["Range"] = range_header
        handler = _RangeHandler.__new__(_RangeHandler)
        handler.server = self.server
        handler.headers = headers
        handler.command = method
        handler.request_version = "HTTP/1.1"
        handler.requestline = f"{method} /video.mp4 HTTP/1.1"
        handler.wfile = wfile or RecordingSocket()
        handler.close_connection = False
        return handler

    def head(self, **kwargs):
        handler = self.handler(**kwargs)
        handler.do_HEAD()
        return handler

    def get(self, range_header, **kwargs):
        handler = self.handler(range_header=range_header, **kwargs)
        getattr(handler, "do_GET")()
        return handler

    def status(self, handler):
        return handler.wfile.headers[0]

    def header(self, handler, name):
        return handler.wfile.headers[1].get(name)


class TestBoxHeaders(Mp4Harness):
    def test_a_plain_header_reports_its_size_and_kind(self):
        data = atom(b"ftyp", b"isom")

        self.assertEqual(_box_header(data, 0), (12, b"ftyp", 8))

    def test_a_sixty_four_bit_header_reports_the_real_size(self):
        data = atom64(b"mdat", b"payload")

        self.assertEqual(_box_header(data, 0), (23, b"mdat", 16))

    def test_a_zero_size_header_runs_to_the_end_of_the_data(self):
        data = atom(b"mdat", b"payload", size=0)

        self.assertEqual(_box_header(data, 0), (15, b"mdat", 8))

    def test_a_header_before_the_start_of_the_data_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid MP4 atom header"):
            _box_header(atom(b"ftyp"), -1)

    def test_a_header_cut_short_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid MP4 atom header"):
            _box_header(b"\x00\x00\x00\x10ft", 0)

    def test_a_sixty_four_bit_size_without_bytes_behind_it_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid extended MP4 atom header"):
            _box_header(b"\x00\x00\x00\x01mdat\x00\x00\x00\x00\x00\x00\x00", 0)

    def test_an_atom_shorter_than_its_own_header_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid MP4 atom size"):
            _box_header(atom(b"free", size=4), 0)

    def test_an_atom_that_promises_more_bytes_than_exist_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid MP4 atom size"):
            _box_header(atom(b"mdat", b"payload", size=4_000), 0)


class TestTopLevelLayout(Mp4Harness):
    def test_a_front_loaded_moov_ends_the_walk_immediately(self):
        header = header_of(ftyp(), moov(offsets=(50_000,)), atom(b"mdat", b"payload"))
        ftyp_size = len(ftyp())

        self.assertEqual(_top_level_layout(header), (ftyp_size, (ftyp_size, len(moov(offsets=(50_000,))))))

    def test_media_data_first_is_reported_without_a_front_moov(self):
        moov_bytes = moov()
        header = header_of(ftyp(), atom(b"mdat", b"payload"), moov_bytes)
        mdat_start = len(ftyp())

        self.assertEqual(_top_level_layout(header), (mdat_start, None))

    def test_padding_before_the_media_data_is_skipped(self):
        header = header_of(ftyp(), atom(b"free", b"\x00" * 16), atom(b"mdat", b"payload"))

        self.assertEqual(_top_level_layout(header), (len(ftyp()) + 24, None))

    def test_a_sixty_four_bit_padding_atom_is_stepped_over(self):
        padding = atom64(b"free", b"\x00" * 16)
        header = header_of(ftyp(), padding, atom(b"mdat", b"payload"))

        self.assertEqual(_top_level_layout(header)[0], len(ftyp()) + len(padding))

    def test_a_front_moov_is_found_even_with_a_sixty_four_bit_header(self):
        moov_bytes = atom(b"moov", struct.pack(">Q", 16 + 8) + atom(b"mvhd", b"\x00" * 8), size=1)
        header = header_of(ftyp(), moov_bytes, atom(b"mdat", b"payload"))

        offset, front = _top_level_layout(header)

        self.assertEqual(front, (len(ftyp()), 24))
        self.assertEqual(offset, len(ftyp()))

    def test_a_probe_that_ends_inside_a_moov_header_asks_for_the_rest(self):
        image = header_of(ftyp(), atom64(b"moov", b"\x00" * 8))
        budget = VirtualFile(image, budget=1 << 20)
        budget.total_size = len(image)

        offset, front = _top_level_layout(image[:len(ftyp()) + 12], budget)

        self.assertEqual(front, (len(ftyp()), 24))
        self.assertEqual(offset, len(ftyp()))
        self.assertEqual(budget.requests, [(len(ftyp()), len(image) - 1)])

    def test_a_probe_that_ends_inside_a_padding_header_asks_for_the_rest(self):
        padding = atom64(b"free", b"\x00" * 8)
        image = header_of(ftyp(), padding, atom(b"mdat", b"payload"))
        budget = VirtualFile(image)
        budget.total_size = len(image)

        self.assertEqual(_top_level_layout(image[:len(ftyp()) + 12], budget)[0],
                         len(ftyp()) + len(padding))

    def test_a_probe_cut_inside_a_sixty_four_bit_padding_header_gives_up(self):
        budget = VirtualFile(ftyp() + b"\x00\x00\x00\x01free\x00\x00\x00\x00")
        budget.total_size = 10_000

        with self.assertRaisesRegex(ValueError, "Could not find MP4 media data header"):
            _top_level_layout(budget.data, budget)

    def test_a_probe_that_stops_before_the_next_atom_header_asks_for_the_rest(self):
        front = moov(offsets=(200,))
        image = header_of(ftyp(), front, atom(b"mdat", b"payload"))
        budget = VirtualFile(image)
        budget.total_size = len(image)

        offset, located = _top_level_layout(image[:len(ftyp())], budget)

        self.assertEqual((offset, located), (len(ftyp()), (len(ftyp()), len(front))))
        self.assertEqual(budget.requests, [(len(ftyp()), len(image) - 1)])

    def test_a_walk_that_runs_out_of_probe_without_a_budget_gives_up(self):
        header = header_of(ftyp(), atom(b"free", b"\x00" * 4_096))[:len(ftyp()) + 4]

        with self.assertRaisesRegex(ValueError, "Could not find MP4 media data header"):
            _top_level_layout(header)

    def test_a_zero_sized_padding_atom_gives_up(self):
        header = header_of(ftyp(), atom(b"free", b"\x00" * 8, size=0))

        with self.assertRaisesRegex(ValueError, "Could not find MP4 media data header"):
            _top_level_layout(header)

    def test_an_atom_promising_bytes_past_the_file_gives_up(self):
        header = header_of(ftyp(), atom(b"free", b"\x00" * 8, size=9_000))
        budget = VirtualFile(header)
        budget.total_size = len(header)

        with self.assertRaisesRegex(ValueError, "Could not find MP4 media data header"):
            _top_level_layout(header, budget)

    def test_an_atom_smaller_than_its_own_header_gives_up(self):
        header = header_of(ftyp(), atom(b"free", size=6))
        budget = VirtualFile(header)
        budget.total_size = len(header)

        with self.assertRaisesRegex(ValueError, "Could not find MP4 media data header"):
            _top_level_layout(header, budget)

    def test_a_probe_with_nothing_left_to_walk_gives_up(self):
        with self.assertRaisesRegex(ValueError, "Could not find MP4 media data header"):
            _top_level_layout(b"\x00\x00\x00\x10")

    def test_a_front_moov_bigger_than_the_probe_still_counts_as_front_loaded(self):
        moov_bytes = atom(b"moov", b"\x00" * 200_000, size=180_000)
        budget = VirtualFile(header_of(ftyp(), moov_bytes))
        budget.total_size = 400_000

        self.assertEqual(_top_level_layout(budget.data[:len(ftyp()) + 8], budget)[1],
                         (len(ftyp()), 180_000))


class TestRelocatedMoov(Mp4Harness):
    def test_chunk_offsets_move_by_the_length_of_the_relocated_atom(self):
        original = moov(offsets=(1_000, 2_000))

        relocated = _relocated_moov(original, 512)

        self.assertEqual(chunk_offsets(relocated), [1_512, 2_512])
        self.assertEqual(len(relocated), len(original))

    def test_a_sixty_four_bit_table_is_patched_at_full_width(self):
        original = moov(offsets=(1_000, 2_000), table=b"co64")

        relocated = _relocated_moov(original, 512)

        self.assertEqual(chunk_offsets(relocated, b"co64", 8), [1_512, 2_512])

    def test_every_container_chain_is_walked(self):
        original = atom(b"moov", atom(b"mvex", atom(b"trak", atom(b"edts", atom(b"dinf",
                     atom(b"mdia", atom(b"minf", atom(b"stbl", stco((7,))))))))))

        self.assertEqual(chunk_offsets(_relocated_moov(original, 3)), [10])

    def test_an_atom_that_is_not_a_moov_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Invalid MP4 moov atom"):
            _relocated_moov(atom(b"mdat", b"payload"), 8)

    def test_a_moov_that_does_not_span_the_whole_buffer_is_refused(self):
        with self.assertRaisesRegex(ValueError, "Invalid MP4 moov atom"):
            _relocated_moov(moov() + b"\x00", 8)

    def test_a_nested_atom_reaching_past_its_parent_is_refused(self):
        greedy = atom(b"stco", b"\x00" * 8, size=200)
        original = atom(b"moov", atom(b"mvhd", b"\x00" * 8) + atom(b"trak", greedy) + b"\x00" * 200)

        with self.assertRaisesRegex(ValueError, "Invalid nested MP4 atom"):
            _relocated_moov(original, 8)

    def test_a_chunk_table_without_a_count_is_refused(self):
        original = atom(b"moov", atom(b"stco", b"\x00" * 4, size=12))

        with self.assertRaisesRegex(ValueError, "Invalid MP4 chunk offset table"):
            _relocated_moov(original, 8)

    def test_a_chunk_table_promising_more_entries_than_it_has_is_refused(self):
        original = atom(b"moov", atom(b"stco", b"\x00" * 4 + (4).to_bytes(4, "big") + b"\x00" * 4))

        with self.assertRaisesRegex(ValueError, "Truncated MP4 chunk offset table"):
            _relocated_moov(original, 8)

    def test_a_thirty_two_bit_offset_that_would_overflow_is_refused(self):
        original = moov(offsets=(0xFFFF_FFF8,))

        with self.assertRaisesRegex(ValueError, "MP4 chunk offset overflow"):
            _relocated_moov(original, 16)


class TestFrontLoadedVirtualFile(Mp4Harness):
    def test_a_front_moov_is_passed_straight_through(self):
        front = moov(offsets=(200,))
        image = header_of(ftyp(), front, atom(b"mdat", b"payload"))
        budget = VirtualFile(image)

        virtual = _FaststartMP4(budget)

        self.assertTrue(virtual.passthrough)
        self.assertEqual(virtual.moov, b"")
        self.assertEqual(virtual.moov_size, 0)
        self.assertEqual(virtual.mdat_start, len(ftyp()))
        self.assertEqual(virtual.moov_virtual_start, len(ftyp()))
        self.assertEqual(virtual.data_virtual_start, len(ftyp()))
        _end, data = virtual.fetch(0, 8)
        self.assertEqual(data, image[:9])

    def test_the_probe_is_one_sixty_four_kib_block(self):
        image = header_of(ftyp(), moov(), b"\x00" * 300_000)
        budget = VirtualFile(image)

        _FaststartMP4(budget)

        self.assertEqual(budget.requests, [(0, TELEGRAM_BLOCK_BYTES - 1)])

    def test_a_front_moov_reaching_the_end_of_the_file_is_refused(self):
        image = header_of(ftyp(), moov())

        with self.assertRaisesRegex(ValueError, "Invalid front-loaded MP4 moov atom"):
            _FaststartMP4(VirtualFile(image))

    def test_a_front_moov_smaller_than_its_header_is_refused(self):
        image = header_of(ftyp(), atom(b"moov", size=4), atom(b"mdat", b"payload"))

        with self.assertRaisesRegex(ValueError, "Invalid front-loaded MP4 moov atom"):
            _FaststartMP4(VirtualFile(image))


class TestTailLoadedVirtualFile(Mp4Harness):
    def setUp(self):
        self.payload = bytes(range(256)) * 200
        self.table = stco((len(ftyp()) + 8,))
        self.moov = atom(b"moov", atom(b"mvhd", b"\x00" * 8) + atom(b"trak",
                          atom(b"mdia", atom(b"minf", atom(b"stbl", self.table)))))
        self.image = header_of(ftyp(), atom(b"mdat", self.payload), self.moov)
        self.mdat_start = len(ftyp())

    def build(self, *, budget: int = 1 << 30):
        self.file = VirtualFile(self.image, budget=budget)
        self.virtual = _FaststartMP4(self.file)
        return self.virtual

    def test_the_moov_moves_in_front_of_the_media_data(self):
        virtual = self.build()

        self.assertFalse(virtual.passthrough)
        self.assertEqual((virtual.moov_start, virtual.moov_size),
                         (len(self.image) - len(self.moov), len(self.moov)))
        self.assertEqual(virtual.mdat_start, self.mdat_start)
        self.assertEqual(virtual.moov_virtual_start, self.mdat_start)
        self.assertEqual(virtual.data_virtual_start, self.mdat_start + len(self.moov))

    def test_the_relocated_moov_offsets_point_at_their_new_positions(self):
        virtual = self.build()

        self.assertEqual(chunk_offsets(virtual.moov), [self.mdat_start + 8 + len(self.moov)])

    def test_a_virtual_read_of_the_header_comes_from_the_source(self):
        virtual = self.build()

        end, data = virtual.fetch(0, self.mdat_start - 1)

        self.assertEqual(data, self.image[:self.mdat_start])
        self.assertEqual(self.file.requests[-1], (0, self.mdat_start - 1))

    def test_a_virtual_read_of_the_relocated_moov_needs_no_source_bytes(self):
        virtual = self.build()
        reads = len(self.file.requests)

        end, data = virtual.fetch(self.mdat_start, self.mdat_start + len(self.moov) - 1)

        self.assertEqual(data, virtual.moov)
        self.assertEqual(len(self.file.requests), reads)

    def test_a_virtual_read_of_the_media_data_is_shifted_back(self):
        virtual = self.build()
        start = self.mdat_start + len(self.moov)

        _end, data = virtual.fetch(start, start + 7)

        self.assertEqual(data, self.image[self.mdat_start:self.mdat_start + 8])
        self.assertEqual(self.file.requests[-1], (self.mdat_start, self.mdat_start + 7))

    def test_one_read_across_all_three_segments_returns_them_in_order(self):
        virtual = self.build()
        last = self.mdat_start + len(self.moov) + 3

        end, data = virtual.fetch(0, last)

        self.assertEqual(data, self.image[:self.mdat_start] + virtual.moov
                         + self.image[self.mdat_start:self.mdat_start + 4])
        self.assertEqual(end, last)

    def test_a_read_reaching_the_end_of_the_file_is_clipped(self):
        virtual = self.build()

        _end, data = virtual.fetch(0, len(self.image) * 2)

        self.assertEqual(len(data), len(self.image))
        self.assertEqual(data, self.image[:self.mdat_start] + virtual.moov
                         + self.image[self.mdat_start:len(self.image) - virtual.moov_size])

    def test_a_read_past_the_end_of_the_file_is_refused(self):
        virtual = self.build()

        with self.assertRaisesRegex(ValueError, "Invalid virtual MP4 byte range"):
            virtual.fetch(len(self.image), len(self.image) + 8)

    def test_a_negative_read_is_refused(self):
        virtual = self.build()

        with self.assertRaisesRegex(ValueError, "Invalid virtual MP4 byte range"):
            virtual.fetch(-1, 8)

    def test_the_tail_is_walked_backwards_until_the_moov_shows_up(self):
        padding = atom(b"free", b"\x00" * 90_000)
        self.image = header_of(ftyp(), atom(b"mdat", self.payload * 3), self.moov, padding)
        total = len(self.image)

        virtual = self.build()

        self.assertEqual(virtual.moov_start, total - len(padding) - len(self.moov))
        self.assertEqual(self.file.requests[1:3],
                         [(total - TELEGRAM_BLOCK_BYTES, total - 1),
                          (total - 2 * TELEGRAM_BLOCK_BYTES + 7, total - TELEGRAM_BLOCK_BYTES + 6)])

    def test_a_source_read_that_comes_back_empty_ends_the_virtual_read(self):
        virtual = self.build()
        start = self.mdat_start + len(self.moov)
        self.file.reader = lambda begin, _end: (begin - 1, b"")

        end, data = virtual.fetch(start, len(self.image) - 1)

        self.assertEqual((end, data), (start - 1, b""))
        self.assertEqual(self.file.requests[-1][0], self.mdat_start)

    def test_a_media_payload_that_spells_moov_is_not_mistaken_for_the_atom(self):
        noise = b"\x00\x00\x00\x00moov" + b"\x00" * 200
        self.image = header_of(ftyp(), atom(b"mdat", noise + self.payload), self.moov)

        virtual = self.build()

        self.assertEqual(virtual.moov_size, len(self.moov))

    def test_a_moov_beyond_the_byte_budget_is_reported_as_unreadable(self):
        self.image = header_of(ftyp(), atom(b"mdat", self.payload * 2), self.moov)

        with self.assertRaisesRegex(RuntimeError, "Could not locate MP4 moov atom within the byte budget"):
            self.build(budget=len(self.moov) + 64)

    def test_a_moov_that_the_last_read_cuts_short_is_reported_as_unreadable(self):
        located = self.build()

        def cut_the_moov_read(start, end):
            data = self.image[start:end + 1]
            if start == located.moov_start:
                data = data[:-1]
            return start + len(data) - 1, data

        truncated = VirtualFile(self.image)
        truncated.reader = cut_the_moov_read

        with self.assertRaisesRegex(RuntimeError, "Could not read the complete MP4 moov atom"):
            _FaststartMP4(truncated)

    def test_a_moov_already_sitting_before_the_media_data_is_refused(self):
        decoy = atom(b"free", b"\x00\x00\x00\x40moov" + b"\x00" * 12)
        self.image = header_of(ftyp(), decoy, atom(b"mdat", self.payload))

        with self.assertRaisesRegex(ValueError, "MP4 already has a front-loaded moov atom"):
            self.build()


class TestHeadRequest(RangeHandlerHarness):
    def test_the_advertised_size_comes_from_the_budget(self):
        self.serve(b"\x00" * 4_096)

        handler = self.head()

        self.assertEqual(self.status(handler), "HTTP/1.1 200 OK")
        self.assertEqual(self.header(handler, "Content-Length"), "4096")
        self.assertEqual(self.file.size_reads, 1)

    def test_the_response_offers_ranges_and_closes(self):
        self.serve(b"\x00" * 16)

        handler = self.head()

        self.assertEqual(self.header(handler, "Accept-Ranges"), "bytes")
        self.assertEqual(self.header(handler, "Content-Type"), "video/mp4")
        self.assertEqual(self.header(handler, "Connection"), "close")
        self.assertTrue(handler.close_connection)

    def test_no_media_is_sent_for_a_head(self):
        self.serve(b"\x00" * 16)

        self.assertEqual(self.head().wfile.body, b"")


class TestRangeRequests(RangeHandlerHarness):
    def setUp(self):
        super().setUp()
        self.image = header_of(ftyp(), moov(), atom(b"mdat", b"payload"))

    def test_an_exact_range_is_served_with_a_content_range(self):
        self.serve(self.image)
        start, end = 4, 11

        handler = self.get(f"bytes={start}-{end}")

        self.assertEqual(self.status(handler), "HTTP/1.1 206 Partial Content")
        self.assertEqual(self.header(handler, "Content-Range"), f"bytes {start}-{end}/{len(self.image)}")
        self.assertEqual(self.header(handler, "Content-Length"), str(end - start + 1))
        self.assertEqual(handler.wfile.body, self.image[start:end + 1])
        self.assertEqual(self.server.request_ranges, [(start, end)])

    def test_the_body_is_flushed_after_every_block(self):
        data = bytes(range(256)) * 900
        self.serve(data)

        handler = self.get(f"bytes=0-{TELEGRAM_BLOCK_BYTES * 2 + 10}")

        self.assertEqual(handler.wfile.flushes, 3)

    def test_a_read_is_split_into_sixty_four_kib_blocks(self):
        data = b"\x00" * (TELEGRAM_BLOCK_BYTES * 2 + 5)
        self.serve(data)

        handler = self.get(f"bytes=0-{len(data) - 1}")

        self.assertEqual(self.file.requests,
                         [(0, TELEGRAM_BLOCK_BYTES - 1),
                          (TELEGRAM_BLOCK_BYTES, 2 * TELEGRAM_BLOCK_BYTES - 1),
                          (2 * TELEGRAM_BLOCK_BYTES, len(data) - 1)])
        self.assertEqual(handler.wfile.body, data)

    def test_an_open_ended_range_serves_the_rest_of_the_file(self):
        self.serve(self.image)
        start = len(ftyp())

        handler = self.get(f"bytes={start}-")

        self.assertEqual(handler.wfile.body, self.image[start:])
        self.assertEqual(self.server.request_ranges, [(start, len(self.image) - 1)])

    def test_an_open_ended_range_without_a_known_size_is_refused(self):
        self.serve(self.image)
        self.file.total_size = None

        handler = self.get("bytes=16-")

        self.assertEqual(self.status(handler), "HTTP/1.1 416 Range Not Satisfiable")

    def test_a_request_without_a_range_header_is_refused(self):
        self.serve(self.image)

        handler = self.get(None)

        self.assertEqual(self.status(handler), "HTTP/1.1 416 Range Not Satisfiable")
        self.assertEqual(self.server.request_ranges, [])

    def test_a_range_that_is_not_two_numbers_is_refused(self):
        self.serve(self.image)

        for header in ("bytes=abc-def", "bytes=-100", "items=0-10", ""):
            with self.subTest(header=header):
                self.assertEqual(self.status(self.get(header)),
                                 "HTTP/1.1 416 Range Not Satisfiable")
        self.assertEqual(self.server.request_ranges, [])

    def test_a_start_at_or_past_the_end_is_refused(self):
        self.serve(self.image)

        for start in (len(self.image), len(self.image) + 500):
            with self.subTest(start=start):
                self.assertEqual(self.status(self.get(f"bytes={start}-{start + 10}")),
                                 "HTTP/1.1 416 Range Not Satisfiable")

    def test_a_range_ending_before_it_starts_is_refused(self):
        self.serve(self.image)

        handler = self.get("bytes=100-20")

        self.assertEqual(self.status(handler), "HTTP/1.1 416 Range Not Satisfiable")
        self.assertEqual(self.server.request_ranges, [(100, 20)])

    def test_a_range_reaching_past_the_end_is_clipped_to_the_last_byte(self):
        self.serve(self.image)
        start = len(self.image) - 4

        handler = self.get(f"bytes={start}-{start + 1_000}")

        self.assertEqual(self.header(handler, "Content-Range"), f"bytes {start}-{len(self.image) - 1}/{len(self.image)}")
        self.assertEqual(handler.wfile.body, self.image[start:])

    def test_a_hung_up_client_ends_the_transfer_without_an_error(self):
        self.serve(self.image)
        socket = RecordingSocket(error=BrokenPipeError())

        handler = self.get("bytes=0-100", wfile=socket)

        self.assertIsNone(self.server.transfer_error)
        self.assertTrue(handler.close_connection)

    def test_a_reset_client_ends_the_transfer_without_an_error(self):
        self.serve(self.image)
        for error in (ConnectionResetError(), ConnectionAbortedError()):
            with self.subTest(error=type(error).__name__):
                self.get("bytes=0-100", wfile=RecordingSocket(error=error))
                self.assertIsNone(self.server.transfer_error)

    def test_a_failing_source_is_recorded_for_the_fetcher(self):
        def refuse(_start, _end):
            raise RuntimeError("Telegram refused the range")

        self.serve(self.image, reader=refuse)

        self.get("bytes=0-100")

        self.assertEqual(self.server.transfer_error, "Telegram refused the range")

    def test_a_source_that_returns_nothing_at_the_budget_reports_the_budget(self):
        self.serve(self.image, budget=1_024)
        self.file.bytes_read = 1_024
        self.mp4 = EmptyFile(self.image)
        self.server.virtual_mp4 = self.mp4

        self.get("bytes=0-100")

        self.assertEqual(self.server.transfer_error, "Video fingerprint byte budget exhausted")

    def test_a_source_that_returns_nothing_below_the_budget_stops_quietly(self):
        self.serve(self.image, budget=1_024)
        self.mp4 = EmptyFile(self.image)
        self.server.virtual_mp4 = self.mp4

        handler = self.get("bytes=0-100")

        self.assertIsNone(self.server.transfer_error)
        self.assertEqual(handler.wfile.body, b"")

    def test_a_short_read_at_the_budget_reports_the_budget(self):
        self.serve(self.image, budget=8)
        self.file.bytes_read = 8
        self.file.short_read = 2

        self.get("bytes=0-100")

        self.assertEqual(self.server.transfer_error, "Video fingerprint byte budget exhausted")

    def test_a_short_read_below_the_budget_stops_quietly(self):
        self.serve(self.image, budget=8_192)
        self.file.short_read = 2

        handler = self.get("bytes=0-100")

        self.assertIsNone(self.server.transfer_error)
        self.assertEqual(handler.wfile.body, self.image[:2])
        self.assertEqual(self.file.requests, [(0, 100)])

    def test_the_budget_property_reads_the_server_attribute(self):
        self.serve(self.image)

        self.assertIs(self.handler().budget, self.file)


if __name__ == "__main__":
    unittest.main()
