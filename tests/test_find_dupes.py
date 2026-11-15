"""Tests for sync.find_dupes."""

from __future__ import annotations

import csv
import io
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sync.find_dupes as find_dupes


class FindDupesTest(unittest.TestCase):
    def test_fmt_dur(self):
        self.assertEqual(find_dupes.fmt_dur(0), "?")
        self.assertEqual(find_dupes.fmt_dur(-15), "?")
        self.assertEqual(find_dupes.fmt_dur(59), "0min")
        self.assertEqual(find_dupes.fmt_dur(60), "1min")
        self.assertEqual(find_dupes.fmt_dur(130), "2min")

    def test_load_videos(self):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("v1", "telegram", "Title 1", 120, json.dumps(["Alice", "Bob"])),
            ("v2", "jellyfin", None, "240", json.dumps(["  aLiCe  ", "BOB", "Charlie"])),
            ("v3", "telegram", "Bad JSON", 100, "not a json string"),
            ("v4", "telegram", "Single", 100, json.dumps(["Alice"])),
            ("v5", "external", "NonIntDur", "invalid_dur", json.dumps(["Alice", "Bob"])),
            ("v6", "external", "NoneDur", None, json.dumps(["Bob", "Charlie"])),
        ]

        with patch("common.db.connect", return_value=mock_conn):
            videos = find_dupes.load_videos("sqlite:///:memory:")

        mock_conn.close.assert_called_once()
        self.assertEqual(len(videos), 4)

        self.assertEqual(videos[0]["id"], "v1")
        self.assertEqual(videos[0]["provider"], "telegram")
        self.assertEqual(videos[0]["title"], "Title 1")
        self.assertEqual(videos[0]["duration"], 120)
        self.assertEqual(videos[0]["performers"], {"alice", "bob"})

        self.assertEqual(videos[1]["id"], "v2")
        self.assertEqual(videos[1]["provider"], "jellyfin")
        self.assertEqual(videos[1]["title"], "")
        self.assertEqual(videos[1]["duration"], 240)
        self.assertEqual(videos[1]["performers"], {"alice", "bob", "charlie"})

        self.assertEqual(videos[2]["id"], "v5")
        self.assertEqual(videos[2]["duration"], 0)

        self.assertEqual(videos[3]["id"], "v6")
        self.assertEqual(videos[3]["duration"], 0)

    def test_find_groups(self):
        videos = [
            {"id": "v1", "provider": "provA", "title": "A1", "duration": 100, "performers": {"alice", "bob", "charlie"}},
            {"id": "v2", "provider": "provB", "title": "B1", "duration": 110, "performers": {"alice", "bob", "charlie"}},
            {"id": "v3", "provider": "provA", "title": "A2", "duration": 100, "performers": {"alice", "bob"}},
            {"id": "v4", "provider": "provC", "title": "C1", "duration": 600, "performers": {"alice", "bob"}},
            {"id": "v5", "provider": "provD", "title": "D1", "duration": 100, "performers": {"alice"}},
            {"id": "v6", "provider": "provE", "title": "E1", "duration": 0, "performers": {"alice", "bob"}},
        ]

        groups = find_dupes.find_groups(videos, min_shared=2, max_dur_diff=60)

        self.assertGreaterEqual(len(groups), 2)
        self.assertEqual(groups[0]["level"], "STRONG")
        self.assertEqual(groups[0]["shared"], ["alice", "bob", "charlie"])
        self.assertEqual({v["id"] for v in groups[0]["videos"]}, {"v1", "v2"})

        weak_groups = [g for g in groups if g["level"] == "WEAK"]
        self.assertGreaterEqual(len(weak_groups), 1)
        for wg in weak_groups:
            self.assertEqual(wg["level"], "WEAK")

    def test_main_csv_output(self):
        with tempfile.TemporaryDirectory() as td:
            out_csv = Path(td) / "report.csv"
            sample_videos = [
                {"id": "v1", "provider": "provA", "title": "Video 1", "duration": 100, "performers": {"alice", "bob"}},
                {"id": "v2", "provider": "provB", "title": "Video 2", "duration": 110, "performers": {"alice", "bob"}},
            ]

            with patch("sync.find_dupes.load_videos", return_value=sample_videos), \
                 patch("sys.argv", ["find_dupes", "--output", str(out_csv), "--format", "csv"]), \
                 patch("sys.stdout", new_callable=io.StringIO):
                rc = find_dupes.main()

            self.assertEqual(rc, 0)
            self.assertTrue(out_csv.exists())

            with out_csv.open("r", encoding="utf-8") as f:
                reader = list(csv.reader(f))
            self.assertEqual(reader[0], ["group", "level", "shared", "provider", "title", "duration_s", "video_id"])
            self.assertEqual(len(reader), 3)

    def test_main_txt_output(self):
        with tempfile.TemporaryDirectory() as td:
            out_txt = Path(td) / "report.txt"
            sample_videos = [
                {"id": "v1", "provider": "provA", "title": "Video 1", "duration": 100, "performers": {"alice", "bob"}},
                {"id": "v2", "provider": "provB", "title": "Video 2", "duration": 500, "performers": {"alice", "bob"}},
            ]

            with patch("sync.find_dupes.load_videos", return_value=sample_videos), \
                 patch("sys.argv", ["find_dupes", "--output", str(out_txt), "--format", "txt"]), \
                 patch("sys.stdout", new_callable=io.StringIO):
                rc = find_dupes.main()

            self.assertEqual(rc, 0)
            self.assertTrue(out_txt.exists())
            content = out_txt.read_text(encoding="utf-8")
            self.assertIn("### 1. [WEAK] shared: alice, bob", content)
            self.assertIn("- [provA] Video 1 (1min) :: v1", content)

    def test_main_top_15_printing(self):
        sample_videos = []
        for i in range(20):
            sample_videos.append({
                "id": f"va_{i}", "provider": "provA", "title": f"Title A {i}",
                "duration": 100, "performers": {f"p_{i}_1", f"p_{i}_2"},
            })
            sample_videos.append({
                "id": f"vb_{i}", "provider": "provB", "title": f"Title B {i}",
                "duration": 100, "performers": {f"p_{i}_1", f"p_{i}_2"},
            })

        with tempfile.TemporaryDirectory() as td:
            out_csv = Path(td) / "top15.csv"
            with patch("sync.find_dupes.load_videos", return_value=sample_videos), \
                 patch("sys.argv", ["find_dupes", "--output", str(out_csv)]), \
                 patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
                rc = find_dupes.main()

            self.assertEqual(rc, 0)
            captured = mock_stdout.getvalue()
            self.assertIn("Pairs: 20 (STRONG: 20, WEAK: 0)", captured)
            self.assertIn("TOP 15 (for manual review):", captured)
            self.assertIn("15. [STRONG]", captured)
            self.assertNotIn("16. [STRONG]", captured)

    def test_module_executed_as_main(self):
        with tempfile.TemporaryDirectory() as td:
            out_csv = Path(td) / "entry.csv"
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = []
            with patch("common.db.connect", return_value=mock_conn), \
                 patch("sys.argv", ["find_dupes", "--output", str(out_csv)]), \
                 patch("sys.stdout", new_callable=io.StringIO), \
                 self.assertRaises(SystemExit) as cm:
                runpy.run_module("sync.find_dupes", run_name="__main__")
            self.assertEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
