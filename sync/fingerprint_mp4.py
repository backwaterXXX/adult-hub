"""Bounded HTTP range serving and MP4 faststart support for fingerprinting."""

from __future__ import annotations

import http.server
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from video_fingerprints import FingerprintBudget


TELEGRAM_BLOCK_BYTES = 64 * 1024


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        return

    @property
    def budget(self) -> FingerprintBudget:
        return self.server.fingerprint_budget  # type: ignore[attr-defined]

    def do_HEAD(self):
        size = self.budget.load_size()
        self.send_response(200)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def do_GET(self):
        header = self.headers.get("Range") or ""
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", header.strip())
        if not match:
            self.send_error(416)
            return
        start = int(match.group(1))
        requested_end = int(match.group(2)) if match.group(2) else (self.budget.total_size or start) - 1
        total_size = self.budget.total_size or self.budget.load_size()
        self.server.request_ranges.append((start, requested_end))  # type: ignore[attr-defined]
        if start < 0 or start >= total_size or requested_end < start:
            self.send_error(416)
            return
        requested_end = min(requested_end, total_size - 1)

        # FFmpeg commonly starts with an open-ended Range request. Buffering
        # that entire response here makes it download from byte zero through
        # the selected frame before FFmpeg can parse the MP4 index and seek.
        # Send bounded chunks as they arrive instead; FFmpeg closes the socket
        # once it has decoded a frame, so unused media is never fetched.
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{requested_end}/{total_size}")
        self.send_header("Content-Length", str(requested_end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        cursor = start
        try:
            while cursor <= requested_end:
                chunk_end = min(requested_end, cursor + TELEGRAM_BLOCK_BYTES - 1)
                actual_end, data = self.server.virtual_mp4.fetch(cursor, chunk_end)  # type: ignore[attr-defined]
                if not data:
                    budget = self.server.fingerprint_budget  # type: ignore[attr-defined]
                    if budget.bytes_read >= budget.budget:
                        self.server.transfer_error = "Video fingerprint byte budget exhausted"  # type: ignore[attr-defined]
                    break
                self.wfile.write(data)
                self.wfile.flush()
                cursor = actual_end + 1
                if actual_end < chunk_end:
                    budget = self.server.fingerprint_budget  # type: ignore[attr-defined]
                    if budget.bytes_read >= budget.budget:
                        self.server.transfer_error = "Video fingerprint byte budget exhausted"  # type: ignore[attr-defined]
                    break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # FFmpeg has decoded the requested frame and closed its input.
            pass
        except Exception as exc:
            self.server.transfer_error = str(exc)  # type: ignore[attr-defined]



def _box_header(data: bytes, offset: int) -> tuple[int, bytes, int]:
    if offset < 0 or offset + 8 > len(data):
        raise ValueError("Invalid MP4 atom header")
    size = int.from_bytes(data[offset:offset + 4], "big")
    kind = bytes(data[offset + 4:offset + 8])
    header_size = 8
    if size == 1:
        if offset + 16 > len(data):
            raise ValueError("Invalid extended MP4 atom header")
        size = int.from_bytes(data[offset + 8:offset + 16], "big")
        header_size = 16
    elif size == 0:
        size = len(data) - offset
    if size < header_size or offset + size > len(data):
        raise ValueError("Invalid MP4 atom size")
    return size, kind, header_size


def _top_level_layout(header: bytes, budget: Optional[FingerprintBudget] = None) -> tuple[int, Optional[tuple[int, int]]]:
    offset = 0
    buffer_offset = 0
    buf = header
    total_size = budget.total_size if (budget and budget.total_size is not None) else len(header)
    front_moov = None
    while offset + 8 <= total_size:
        rel = offset - buffer_offset
        if rel + 8 > len(buf):
            if budget is None:
                break
            _end, buf = budget.fetch(offset, min(offset + 65535, total_size - 1))
            buffer_offset = offset
            rel = 0
        size = int.from_bytes(buf[rel:rel + 4], "big")
        kind = buf[rel + 4:rel + 8]
        if kind == b"moov":
            if size == 1:
                if rel + 16 > len(buf) and budget is not None:
                    _end, buf = budget.fetch(offset, min(offset + 65535, total_size - 1))
                    buffer_offset = offset
                    rel = 0
                if rel + 16 <= len(buf):
                    size = int.from_bytes(buf[rel + 8:rel + 16], "big")
            front_moov = (offset, size)
            # We only need to know whether moov precedes mdat. A front-loaded
            # moov can be passed through unchanged, even when the 64 KiB
            # probe ends in the middle of that (often much larger) atom.
            return offset, front_moov
        if kind == b"mdat":
            return offset, front_moov
        header_size = 8
        if size == 1:
            if rel + 16 > len(buf) and budget is not None:
                _end, buf = budget.fetch(offset, min(offset + 65535, total_size - 1))
                buffer_offset = offset
                rel = 0
            if rel + 16 > len(buf):
                break
            size = int.from_bytes(buf[rel + 8:rel + 16], "big")
            header_size = 16
        elif size == 0:
            break
        if size < header_size or offset + size > total_size:
            break
        offset += size
    raise ValueError("Could not find MP4 media data header")


def _relocated_moov(data: bytes, shift: int) -> bytes:
    """Patch chunk offsets for a moov atom moved in front of mdat."""
    output = bytearray(data)
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts", b"dinf", b"mvex"}

    def walk(start: int, end: int) -> None:
        offset = start
        while offset + 8 <= end:
            size, kind, header_size = _box_header(output, offset)
            box_end = offset + size
            if box_end > end:
                raise ValueError("Invalid nested MP4 atom")
            payload = offset + header_size
            if kind in containers:
                walk(payload, box_end)
            elif kind in {b"stco", b"co64"}:
                if payload + 8 > box_end:
                    raise ValueError("Invalid MP4 chunk offset table")
                count = int.from_bytes(output[payload + 4:payload + 8], "big")
                width = 4 if kind == b"stco" else 8
                entries = payload + 8
                if entries + count * width > box_end:
                    raise ValueError("Truncated MP4 chunk offset table")
                for index in range(count):
                    position = entries + index * width
                    value = int.from_bytes(output[position:position + width], "big") + shift
                    if value >= 1 << (width * 8):
                        raise ValueError("MP4 chunk offset overflow")
                    output[position:position + width] = value.to_bytes(width, "big")
            offset = box_end

    size, kind, header_size = _box_header(data, 0)
    if kind != b"moov" or size != len(data):
        raise ValueError("Invalid MP4 moov atom")
    walk(header_size, len(data))
    return bytes(output)


class _FaststartMP4:
    """Expose a tail-moov MP4 as a virtual faststart file using sparse ranges."""

    def __init__(self, budget: FingerprintBudget):
        self.budget = budget
        self.total_size = budget.total_size
        assert self.total_size is not None
        _header_end, header = budget.fetch(0, min(65535, self.total_size - 1))
        self.mdat_start, front_moov = _top_level_layout(header, budget)
        if front_moov:
            moov_start, moov_size = front_moov
            if (moov_size < 8 or moov_start + moov_size >= self.total_size):
                raise ValueError("Invalid front-loaded MP4 moov atom")
            self.passthrough = True
            self.moov = b""
            self.moov_size = 0
            self.moov_virtual_start = self.mdat_start
            self.data_virtual_start = self.mdat_start
            return
        self.passthrough = False
        # Locate the moov atom from EOF inward; this is bounded by the remaining
        # Telegram byte budget and uses ranges, so media payload is not downloaded.
        scan_start = max(0, self.total_size - TELEGRAM_BLOCK_BYTES)
        moov_location = None
        min_start = max(0, self.total_size - (budget.budget - budget.bytes_read))
        while scan_start >= min_start:
            scan_end = min(self.total_size - 1, scan_start + TELEGRAM_BLOCK_BYTES - 1)
            _end, chunk = budget.fetch(scan_start, scan_end)
            pos = chunk.find(b"moov")
            while pos >= 4:
                atom_start = scan_start + pos - 4
                atom_size = int.from_bytes(chunk[pos - 4:pos], "big")
                if atom_size >= 8 and atom_start + atom_size <= self.total_size:
                    moov_location = (atom_start, atom_size)
                    break
                pos = chunk.find(b"moov", pos + 4)
            if moov_location or scan_start == 0:
                break
            scan_start = max(0, scan_start - TELEGRAM_BLOCK_BYTES + 7)
        if not moov_location:
            raise RuntimeError("Could not locate MP4 moov atom within the byte budget")
        self.moov_start, self.moov_size = moov_location
        if self.moov_start < self.mdat_start:
            raise ValueError("MP4 already has a front-loaded moov atom")
        _end, raw_moov = budget.fetch(self.moov_start, self.moov_start + self.moov_size - 1)
        if len(raw_moov) != self.moov_size:
            raise RuntimeError("Could not read the complete MP4 moov atom within the byte budget")
        self.moov = _relocated_moov(raw_moov, self.moov_size)
        self.moov_virtual_start = self.mdat_start
        self.data_virtual_start = self.mdat_start + self.moov_size

    def fetch(self, start: int, requested_end: int) -> tuple[int, bytes]:
        if self.passthrough:
            return self.budget.fetch(start, requested_end)
        if start < 0 or start >= self.total_size:
            raise ValueError("Invalid virtual MP4 byte range")
        requested_end = min(requested_end, self.total_size - 1)
        output = bytearray()
        cursor = start
        while cursor <= requested_end:
            if cursor < self.mdat_start:
                boundary = self.mdat_start
                original_start = cursor
                segment = "source"
            elif cursor < self.data_virtual_start:
                boundary = self.data_virtual_start
                original_start = None
                segment = "moov"
            else:
                boundary = self.total_size
                original_start = cursor - self.moov_size
                segment = "source"
            part_end = min(requested_end, boundary - 1)
            if segment == "moov":
                offset = cursor - self.moov_virtual_start
                output.extend(self.moov[offset:offset + part_end - cursor + 1])
                cursor = part_end + 1
                continue
            original_end = original_start + (part_end - cursor)
            _actual_end, data = self.budget.fetch(original_start, original_end)
            output.extend(data)
            cursor += len(data)
            if not data:
                break
        return start + len(output) - 1, bytes(output)
