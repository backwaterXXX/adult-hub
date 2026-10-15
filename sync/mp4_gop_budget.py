"""Read MP4 sample tables and estimate video packet bytes needed by seeks.

This is a preflight only. It deliberately does not decode frames or claim that
the estimate is the complete FFmpeg transfer: probes, edit lists, decoder
reordering, and non-video tracks can require additional bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


class MP4IndexError(ValueError):
    """The MP4 sample index is unsupported or malformed."""


@dataclass(frozen=True)
class _Box:
    kind: bytes
    start: int
    payload: int
    end: int


@dataclass(frozen=True)
class GOPEstimate:
    timestamp: float
    sync_sample: int  # zero-based decode-order sample index
    target_sample: int
    packet_bytes: int
    packet_ranges: tuple[tuple[int, int], ...]  # half-open source byte ranges


def _boxes(data: bytes, start: int, end: int) -> Iterable[_Box]:
    pos = start
    while pos < end:
        if pos + 8 > end:
            raise MP4IndexError("truncated box header")
        size = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise MP4IndexError("truncated extended box header")
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise MP4IndexError(f"invalid {kind!r} box size")
        yield _Box(kind, pos, pos + header, pos + size)
        pos += size


def _children(data: bytes, box: _Box) -> list[_Box]:
    return list(_boxes(data, box.payload, box.end))


def _one(boxes: Iterable[_Box], kind: bytes, required: bool = True) -> _Box | None:
    found = [box for box in boxes if box.kind == kind]
    if len(found) > 1 or (required and not found):
        raise MP4IndexError(f"expected one {kind.decode('ascii', 'replace')} box")
    return found[0] if found else None


def _fullbox(data: bytes, box: _Box) -> tuple[int, int]:
    if box.payload + 4 > box.end:
        raise MP4IndexError(f"truncated {box.kind!r} full box")
    return data[box.payload], box.payload + 4


def _u32(data: bytes, offset: int, end: int) -> int:
    if offset + 4 > end:
        raise MP4IndexError("truncated 32-bit field")
    return int.from_bytes(data[offset:offset + 4], "big")


def _table_count(data: bytes, pos: int, end: int) -> tuple[int, int]:
    count = _u32(data, pos, end)
    return count, pos + 4


def _parse_runs(data: bytes, box: _Box, *, signed_value: bool = False) -> list[tuple[int, int]]:
    _version, pos = _fullbox(data, box)
    count, pos = _table_count(data, pos, box.end)
    if pos + count * 8 != box.end:
        raise MP4IndexError(f"invalid {box.kind!r} run table length")
    result = []
    for _ in range(count):
        n = _u32(data, pos, box.end)
        value = int.from_bytes(data[pos + 4:pos + 8], "big", signed=signed_value)
        result.append((n, value))
        pos += 8
    return result


def _parse_sample_sizes(data: bytes, stbl: list[_Box]) -> list[int]:
    stsz = _one(stbl, b"stsz", required=False)
    stz2 = _one(stbl, b"stz2", required=False)
    if bool(stsz) == bool(stz2):
        raise MP4IndexError("expected exactly one of stsz or stz2")
    if stsz:
        _version, pos = _fullbox(data, stsz)
        fixed = _u32(data, pos, stsz.end)
        count = _u32(data, pos + 4, stsz.end)
        pos += 8
        if fixed:
            if pos != stsz.end:
                raise MP4IndexError("invalid fixed-size stsz")
            return [fixed] * count
        if pos + count * 4 != stsz.end:
            raise MP4IndexError("invalid stsz length")
        return [_u32(data, pos + 4 * i, stsz.end) for i in range(count)]

    assert stz2 is not None
    _version, pos = _fullbox(data, stz2)
    if pos + 4 > stz2.end:
        raise MP4IndexError("truncated stz2 header")
    field_size = data[pos + 3]
    count = _u32(data, pos + 4, stz2.end)
    pos += 8
    if field_size == 4:
        need = (count + 1) // 2
        if pos + need != stz2.end:
            raise MP4IndexError("invalid 4-bit stz2 length")
        values = []
        for i in range(count):
            byte = data[pos + i // 2]
            values.append((byte >> 4) if i % 2 == 0 else (byte & 0x0F))
        return values
    if field_size not in (8, 16):
        raise MP4IndexError("unsupported stz2 field size")
    width = field_size // 8
    if pos + count * width != stz2.end:
        raise MP4IndexError("invalid stz2 length")
    return [int.from_bytes(data[pos + i * width:pos + (i + 1) * width], "big")
            for i in range(count)]


def _parse_offsets(data: bytes, stbl: list[_Box]) -> list[int]:
    stco = _one(stbl, b"stco", required=False)
    co64 = _one(stbl, b"co64", required=False)
    if bool(stco) == bool(co64):
        raise MP4IndexError("expected exactly one of stco or co64")
    box = stco or co64
    assert box is not None
    _version, pos = _fullbox(data, box)
    count, pos = _table_count(data, pos, box.end)
    width = 4 if stco else 8
    if pos + count * width != box.end:
        raise MP4IndexError("invalid chunk offset table length")
    return [int.from_bytes(data[pos + i * width:pos + (i + 1) * width], "big")
            for i in range(count)]


def _sample_positions(data: bytes, stbl: list[_Box], sizes: list[int]) -> list[tuple[int, int]]:
    offsets = _parse_offsets(data, stbl)
    stsc = _one(stbl, b"stsc")
    assert stsc is not None
    _version, pos = _fullbox(data, stsc)
    count, pos = _table_count(data, pos, stsc.end)
    if pos + count * 12 != stsc.end or count == 0:
        raise MP4IndexError("invalid stsc table")
    entries = []
    for _ in range(count):
        entries.append((_u32(data, pos, stsc.end), _u32(data, pos + 4, stsc.end)))
        pos += 12
    if entries[0][0] != 1 or any(a[0] >= b[0] for a, b in zip(entries, entries[1:])):
        raise MP4IndexError("invalid stsc chunk ordering")

    positions: list[tuple[int, int]] = []
    entry_index = 0
    sample_index = 0
    for chunk_number, chunk_offset in enumerate(offsets, start=1):
        while entry_index + 1 < len(entries) and entries[entry_index + 1][0] <= chunk_number:
            entry_index += 1
        samples_in_chunk = entries[entry_index][1]
        cursor = chunk_offset
        for _ in range(samples_in_chunk):
            if sample_index >= len(sizes):
                raise MP4IndexError("stsc maps more samples than stsz")
            size = sizes[sample_index]
            positions.append((cursor, cursor + size))
            cursor += size
            sample_index += 1
    if sample_index != len(sizes):
        raise MP4IndexError("stsc maps fewer samples than stsz")
    return positions


def _track_timebase(data: bytes, mdhd: _Box) -> tuple[int, int]:
    version, pos = _fullbox(data, mdhd)
    timescale_pos = pos + (16 if version == 1 else 8)
    duration_pos = timescale_pos + 4
    timescale = _u32(data, timescale_pos, mdhd.end)
    duration_width = 8 if version == 1 else 4
    if duration_pos + duration_width > mdhd.end or timescale <= 0:
        raise MP4IndexError("invalid mdhd timebase")
    return timescale, duration_pos + duration_width


def _edit_map(data: bytes, edts: _Box | None, movie_timescale: int,
              media_timescale: int) -> list[tuple[float, float, float | None]]:
    """Return (movie start, movie end, media start) edits; None means empty edit."""
    if edts is None:
        return []
    boxes = _children(data, edts)
    elst = _one(boxes, b"elst")
    assert elst is not None
    version, pos = _fullbox(data, elst)
    count, pos = _table_count(data, pos, elst.end)
    width = 20 if version == 1 else 12
    if pos + count * width != elst.end:
        raise MP4IndexError("invalid elst length")
    timeline = 0.0
    result = []
    for _ in range(count):
        if version == 1:
            duration = int.from_bytes(data[pos:pos + 8], "big")
            media = int.from_bytes(data[pos + 8:pos + 16], "big", signed=True)
            rate_pos = pos + 16
        else:
            duration = _u32(data, pos, elst.end)
            media = int.from_bytes(data[pos + 4:pos + 8], "big", signed=True)
            rate_pos = pos + 8
        rate_integer = int.from_bytes(data[rate_pos:rate_pos + 2], "big", signed=True)
        rate_fraction = int.from_bytes(data[rate_pos + 2:rate_pos + 4], "big", signed=True)
        if (rate_integer, rate_fraction) != (1, 0):
            raise MP4IndexError("non-unit edit rate is unsupported")
        start = timeline
        timeline += duration / movie_timescale
        media_start = None if media == -1 else media / media_timescale
        result.append((start, timeline, media_start))
        pos += width
    return result


def estimate_video_gops(moov: bytes, timestamps: list[float]) -> list[GOPEstimate]:
    """Estimate packet byte ranges from previous sync sample through target PTS.

    The returned packet byte totals are a lower-bound-style estimate for video
    decoding. They exclude audio, container probing, and possible decoder
    reordering packets, so they must not be presented as a total transfer cap.
    """
    roots = list(_boxes(moov, 0, len(moov)))
    moov_box = _one(roots, b"moov")
    assert moov_box is not None and moov_box.start == 0 and moov_box.end == len(moov)
    moov_children = _children(moov, moov_box)
    mvhd = _one(moov_children, b"mvhd")
    assert mvhd is not None
    movie_timescale, _ = _track_timebase(moov, mvhd)

    video = []
    for trak in (box for box in moov_children if box.kind == b"trak"):
        trak_children = _children(moov, trak)
        mdia = _one(trak_children, b"mdia")
        assert mdia is not None
        mdia_children = _children(moov, mdia)
        hdlr = _one(mdia_children, b"hdlr")
        assert hdlr is not None
        _version, hpos = _fullbox(moov, hdlr)
        if hpos + 8 > hdlr.end or moov[hpos + 4:hpos + 8] != b"vide":
            continue
        mdhd = _one(mdia_children, b"mdhd")
        minf = _one(mdia_children, b"minf")
        assert mdhd is not None and minf is not None
        media_timescale, _ = _track_timebase(moov, mdhd)
        stbl = _one(_children(moov, minf), b"stbl")
        assert stbl is not None
        stbl_children = _children(moov, stbl)
        sizes = _parse_sample_sizes(moov, stbl_children)
        positions = _sample_positions(moov, stbl_children, sizes)
        stts = _one(stbl_children, b"stts")
        assert stts is not None
        durations = _parse_runs(moov, stts)
        dts = []
        current = 0
        for count, delta in durations:
            if delta <= 0:
                raise MP4IndexError("non-positive sample duration")
            if len(dts) + count > len(sizes):
                raise MP4IndexError("stts maps more samples than stsz")
            dts.extend(range(current, current + count * delta, delta))
            current += count * delta
        if len(dts) != len(sizes):
            raise MP4IndexError("stts maps fewer samples than stsz")
        ctts = _one(stbl_children, b"ctts", required=False)
        composition = [0] * len(sizes)
        if ctts:
            ctts_version = moov[ctts.payload]
            runs = _parse_runs(moov, ctts, signed_value=(ctts_version == 1))
            sample = 0
            for count, offset in runs:
                if sample + count > len(sizes):
                    raise MP4IndexError("ctts maps more samples than stsz")
                composition[sample:sample + count] = [offset] * count
                sample += count
            if sample != len(sizes):
                raise MP4IndexError("ctts maps fewer samples than stsz")
        pts = [decode + offset for decode, offset in zip(dts, composition)]
        stss = _one(stbl_children, b"stss", required=False)
        if stss:
            _version, pos = _fullbox(moov, stss)
            count, pos = _table_count(moov, pos, stss.end)
            if pos + count * 4 != stss.end:
                raise MP4IndexError("invalid stss length")
            sync = [int.from_bytes(moov[pos + 4 * i:pos + 4 * i + 4], "big") - 1
                    for i in range(count)]
            if any(i < 0 or i >= len(sizes) for i in sync):
                raise MP4IndexError("stss sample index out of range")
        else:
            sync = list(range(len(sizes)))
        if not sync:
            raise MP4IndexError("video track has no sync samples")
        edits = _edit_map(moov, _one(trak_children, b"edts", required=False),
                          movie_timescale, media_timescale)
        video.append((sizes, positions, pts, sync, edits, media_timescale))

    if len(video) != 1:
        raise MP4IndexError(f"expected one video track, found {len(video)}")
    sizes, positions, pts, sync, edits, timescale = video[0]
    result = []
    for timestamp in timestamps:
        target_seconds = float(timestamp)
        media_seconds = target_seconds
        if edits:
            mapped = None
            for start, end, media_start in edits:
                if start <= target_seconds < end:
                    if media_start is None:
                        break
                    media_seconds = media_start + target_seconds - start
                    mapped = media_seconds
                    break
            if mapped is None:
                raise MP4IndexError("sample timestamp lies in an empty edit or outside edit list")
        target_tick = media_seconds * timescale
        candidates = [i for i, value in enumerate(pts) if value >= target_tick]
        if not candidates:
            target_index = max(range(len(pts)), key=pts.__getitem__)
        else:
            target_index = min(candidates, key=pts.__getitem__)
        preceding = [i for i in sync if i <= target_index]
        if not preceding:
            raise MP4IndexError("no preceding sync sample")
        sync_index = max(preceding)
        spans = tuple(positions[sync_index:target_index + 1])
        result.append(GOPEstimate(
            timestamp=target_seconds,
            sync_sample=sync_index,
            target_sample=target_index,
            packet_bytes=sum(end - start for start, end in spans),
            packet_ranges=spans,
        ))
    return result


def union_bytes(estimates: list[GOPEstimate]) -> int:
    """Count unique indexed packet bytes across GOP estimates."""
    ranges = sorted((start, end) for est in estimates for start, end in est.packet_ranges)
    total = 0
    if not ranges:
        return 0
    start, end = ranges[0]
    for next_start, next_end in ranges[1:]:
        if next_start <= end:
            end = max(end, next_end)
        else:
            total += end - start
            start, end = next_start, next_end
    return total + end - start
