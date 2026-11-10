import unittest

from sync.mp4_gop_budget import MP4IndexError, estimate_video_gops, union_bytes


def box(kind: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + kind + payload


def fullbox(kind: bytes, payload: bytes, version: int = 0) -> bytes:
    return box(kind, bytes([version, 0, 0, 0]) + payload)


def _synthetic_moov() -> bytes:
    mvhd = fullbox(b"mvhd", b"\0" * 8 + (1000).to_bytes(4, "big")
                   + (6000).to_bytes(4, "big") + b"\0" * 80)
    mdhd = fullbox(b"mdhd", b"\0" * 8 + (1000).to_bytes(4, "big")
                   + (6000).to_bytes(4, "big") + b"\0" * 4)
    hdlr = fullbox(b"hdlr", b"\0" * 4 + b"vide" + b"\0" * 12)
    stts = fullbox(b"stts", (1).to_bytes(4, "big")
                   + (6).to_bytes(4, "big") + (1000).to_bytes(4, "big"))
    stss = fullbox(b"stss", (2).to_bytes(4, "big")
                   + (1).to_bytes(4, "big") + (4).to_bytes(4, "big"))
    stsz = fullbox(b"stsz", (0).to_bytes(4, "big") + (6).to_bytes(4, "big")
                   + b"".join((10).to_bytes(4, "big") for _ in range(6)))
    stsc = fullbox(b"stsc", (2).to_bytes(4, "big")
                   + (1).to_bytes(4, "big") + (3).to_bytes(4, "big") + (1).to_bytes(4, "big")
                   + (2).to_bytes(4, "big") + (3).to_bytes(4, "big") + (1).to_bytes(4, "big"))
    stco = fullbox(b"stco", (2).to_bytes(4, "big")
                   + (1000).to_bytes(4, "big") + (1030).to_bytes(4, "big"))
    stbl = box(b"stbl", stts + stss + stsz + stsc + stco)
    minf = box(b"minf", stbl)
    mdia = box(b"mdia", mdhd + hdlr + minf)
    trak = box(b"trak", mdia)
    return box(b"moov", mvhd + trak)


class TestMP4GOPBudget(unittest.TestCase):
    def test_counts_samples_from_previous_sync_frame_to_target(self):
        estimates = estimate_video_gops(_synthetic_moov(), [1.2, 4.2])
        self.assertEqual([(e.sync_sample, e.target_sample, e.packet_bytes) for e in estimates],
                         [(0, 2, 30), (3, 5, 30)])
        self.assertEqual(union_bytes(estimates), 60)

    def test_rejects_malformed_moov(self):
        with self.assertRaises(MP4IndexError):
            estimate_video_gops(b"not an mp4", [1.0])


if __name__ == "__main__":
    unittest.main()
