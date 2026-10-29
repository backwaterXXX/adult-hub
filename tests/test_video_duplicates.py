import json
import os
import sys
import tempfile
import unittest
import asyncio
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from fastapi.responses import JSONResponse
from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase, TEST_URL

sys.path.insert(0, str(Path(__file__).parent.parent / "resolver"))
from video_duplicates import (
    find_duplicate_candidates, scan_duplicate_index, _score, _dismissals_for_group, _index_dismissals,
)
import video_duplicates
from common import db
import admin_v2_jobs

sys.path.insert(0, str(Path(__file__).parents[1] / "sync"))
import duplicate_files
import video_duplicate_automation


class TestAutomaticDuplicateRules(unittest.TestCase):
    def test_split_part_identity_uses_original_filename_markers(self):
        first = {"source_filename": "Studio_scene_s1.mp4"}
        second = {"source_filename": "Studio_scene_s2.mp4"}
        self.assertEqual(video_duplicate_automation._part_identity(first), (True, 1))
        self.assertEqual(video_duplicate_automation._part_identity(second), (True, 2))

    def test_split_scene_provenance_without_number_is_ambiguous(self):
        row = {"scene_match_source": "stashdb:split_title_duration_v1",
               "source_filename": "Studio_scene.mp4", "title": "Same catalog title"}
        self.assertEqual(video_duplicate_automation._part_identity(row), (True, None))

    def test_multi_marker_part_identity_detects_conflicts(self):
        first = {"title": "Series Vol 2 Part 1"}
        second = {"title": "Series Vol 2 Part 2"}
        third = {"title": "Series Vol 2 Part 1"}
        self.assertEqual(video_duplicate_automation._part_identity(first), (True, (('part', 1), ('vol', 2))))
        self.assertEqual(video_duplicate_automation._part_identity(second), (True, (('part', 2), ('vol', 2))))
        self.assertNotEqual(video_duplicate_automation._part_identity(first), video_duplicate_automation._part_identity(second))
        self.assertEqual(video_duplicate_automation._part_identity(first), video_duplicate_automation._part_identity(third))

    def test_conflicting_part_numbers_within_row_is_ambiguous(self):
        row = {"source_filename": "Series_Part1.mp4", "title": "Series Part 2"}
        self.assertEqual(video_duplicate_automation._part_identity(row), (True, None))

    def test_duration_tolerance_is_bounded(self):
        self.assertTrue(video_duplicate_automation._duration_matches(
            {"duration": 858}, {"duration": 866}))
        self.assertFalse(video_duplicate_automation._duration_matches(
            {"duration": 858}, {"duration": 880}))


class TestDuplicateFileMoveAtomic(unittest.TestCase):
    def test_duplicate_match_does_not_follow_symlink_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")
            target.symlink_to(source)

            self.assertFalse(duplicate_files._duplicate_file_matches(source, target))
            self.assertFalse(duplicate_files._duplicate_file_matches(target, source))
            self.assertEqual(source.read_text(encoding="utf-8"), "registered stream")


    def test_duplicate_match_rejects_symlink_swap_at_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")
            target.write_text("registered stream", encoding="utf-8")
            real_open = os.open

            def replace_target_with_symlink(path, flags, *args, **kwargs):
                if Path(path) == target:
                    target.unlink()
                    target.symlink_to(source)
                return real_open(path, flags, *args, **kwargs)

            with patch.object(duplicate_files.os, "open", side_effect=replace_target_with_symlink):
                self.assertFalse(duplicate_files._duplicate_file_matches(source, target))

            self.assertTrue(target.is_symlink())
            self.assertEqual(source.read_text(encoding="utf-8"), "registered stream")

    def test_duplicate_match_accepts_two_empty_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left, right = root / "left.strm", root / "right.strm"
            left.touch()
            right.touch()

            self.assertTrue(duplicate_files._duplicate_file_matches(left, right))

    def test_duplicate_match_detects_last_byte_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left, right = root / "left.strm", root / "right.strm"
            left.write_bytes(b"x" * (2 * 1024 * 1024))
            right.write_bytes(b"x" * (2 * 1024 * 1024 - 1) + b"y")

            self.assertFalse(duplicate_files._duplicate_file_matches(left, right))

    def test_duplicate_match_rejects_non_regular_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, child = root / "source.strm", root / "folder"
            source.write_text("registered stream", encoding="utf-8")
            child.mkdir()

            self.assertFalse(duplicate_files._duplicate_file_matches(source, child))
            self.assertFalse(duplicate_files._duplicate_file_matches(child, source))

    def test_duplicate_match_fails_closed_if_file_changes_during_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left, right = root / "left.strm", root / "right.strm"
            content = b"x" * (2 * 1024 * 1024)
            left.write_bytes(content)
            right.write_bytes(content)
            real_open, real_fstat = os.open, os.fstat
            opened_fds = []
            fstat_calls = 0

            def track_open(*args, **kwargs):
                fd = real_open(*args, **kwargs)
                opened_fds.append(fd)
                return fd

            def mutate_after_content_read(fd):
                nonlocal fstat_calls
                fstat_calls += 1
                if fstat_calls == 3:
                    with left.open("ab") as changed:
                        changed.write(b"changed")
                return real_fstat(fd)

            with patch.object(duplicate_files.os, "open", side_effect=track_open), \
                 patch.object(duplicate_files.os, "fstat", side_effect=mutate_after_content_read):
                self.assertFalse(duplicate_files._duplicate_file_matches(left, right))

            self.assertEqual(len(opened_fds), 2)
            for fd in opened_fds:
                with self.assertRaises(OSError):
                    real_fstat(fd)

    def test_duplicate_match_fails_closed_when_no_follow_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left, right = root / "left.strm", root / "right.strm"
            left.write_text("same", encoding="utf-8")
            right.write_text("same", encoding="utf-8")

            with patch.object(duplicate_files.os, "O_NOFOLLOW", None):
                self.assertFalse(duplicate_files._duplicate_file_matches(left, right))

    def test_move_refuses_to_replace_a_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            missing = root / "missing-target"
            source.write_text("registered stream", encoding="utf-8")
            target.symlink_to(missing)

            with self.assertRaises(FileExistsError):
                duplicate_files._duplicate_move_file(source, target)

            self.assertEqual(source.read_text(encoding="utf-8"), "registered stream")
            self.assertTrue(target.is_symlink())
            self.assertFalse(missing.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo") and hasattr(os, "O_NONBLOCK"),
                         "FIFO open flags are unavailable on this platform")
    def test_move_does_not_block_if_source_changes_to_fifo_before_open(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")
            real_open = os.open

            def replace_source_with_fifo(path, flags, *args, **kwargs):
                if Path(path) == source:
                    source.unlink()
                    os.mkfifo(source)
                    self.assertTrue(flags & os.O_NONBLOCK)
                return real_open(path, flags, *args, **kwargs)

            with patch.object(duplicate_files.os, "open", side_effect=replace_source_with_fifo):
                with self.assertRaisesRegex(RuntimeError, "Source file changed"):
                    duplicate_files._duplicate_move_file(source, target)

            self.assertTrue(source.is_fifo())
            self.assertFalse(target.exists())
            self.assertEqual(list(root.glob(".*.duplicate-tmp-*")), [])

    def test_move_fails_closed_when_no_follow_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source.strm", root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")

            with patch.object(duplicate_files.os, "O_NOFOLLOW", None):
                with self.assertRaisesRegex(OSError, "require O_NOFOLLOW"):
                    duplicate_files._duplicate_move_file(source, target)

            self.assertEqual(source.read_text(encoding="utf-8"), "registered stream")
            self.assertFalse(target.exists())
            self.assertEqual(list(root.glob(".*.duplicate-tmp-*")), [])

    def test_move_does_not_overwrite_a_target_created_after_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")
            real_link = os.link

            def create_target_before_atomic_publish(temp_path, target_path, **kwargs):
                Path(target_path).write_text("operator-owned file", encoding="utf-8")
                return real_link(temp_path, target_path, **kwargs)

            with patch.object(duplicate_files.os, "link", side_effect=create_target_before_atomic_publish):
                with self.assertRaises(FileExistsError):
                    duplicate_files._duplicate_move_file(source, target)

            self.assertEqual(source.read_text(encoding="utf-8"), "registered stream")
            self.assertEqual(target.read_text(encoding="utf-8"), "operator-owned file")
            self.assertEqual(list(root.glob(".*.duplicate-tmp-*")), [])

    def test_move_does_not_retire_source_if_target_is_replaced_after_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")
            real_link = os.link

            def replace_after_publish(temp_path, target_path, **kwargs):
                real_link(temp_path, target_path, **kwargs)
                Path(target_path).unlink()
                Path(target_path).write_text("operator-owned file", encoding="utf-8")

            with patch.object(duplicate_files.os, "link", side_effect=replace_after_publish):
                with self.assertRaisesRegex(RuntimeError, "Target file changed"):
                    duplicate_files._duplicate_move_file(source, target)

            self.assertEqual(source.read_text(encoding="utf-8"), "registered stream")
            self.assertEqual(target.read_text(encoding="utf-8"), "operator-owned file")
            self.assertEqual(list(root.glob(".*.duplicate-tmp-*")), [])

    def test_move_restores_source_if_target_changes_during_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")
            real_rename = os.rename

            def replace_target_during_retirement(source_path, retired_path, *args, **kwargs):
                result = real_rename(source_path, retired_path, *args, **kwargs)
                if Path(source_path) == source:
                    target.unlink()
                    target.write_text("operator-owned file", encoding="utf-8")
                return result

            with patch.object(duplicate_files.os, "rename", side_effect=replace_target_during_retirement):
                with self.assertRaisesRegex(RuntimeError, "Target file changed"):
                    duplicate_files._duplicate_move_file(source, target)

            self.assertEqual(source.read_text(encoding="utf-8"), "registered stream")
            self.assertEqual(target.read_text(encoding="utf-8"), "operator-owned file")
            self.assertEqual(list(root.glob(".*.duplicate-retire-*")), [])
            self.assertEqual(list(root.glob(".*.duplicate-tmp-*")), [])

    def test_move_restores_source_if_target_changes_at_retired_file_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")
            real_unlink = Path.unlink

            def replace_target_before_retired_unlink(path, *args, **kwargs):
                if path.name == source.name and ".duplicate-retire-" in path.parent.name:
                    target.unlink()
                    target.write_text("operator-owned file", encoding="utf-8")
                return real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", new=replace_target_before_retired_unlink):
                with self.assertRaisesRegex(RuntimeError, "Target file changed after source retirement"):
                    duplicate_files._duplicate_move_file(source, target)

            self.assertEqual(source.read_text(encoding="utf-8"), "registered stream")
            self.assertEqual(target.read_text(encoding="utf-8"), "operator-owned file")
            self.assertEqual(list(root.glob(".*.duplicate-retire-*")), [])
            self.assertEqual(list(root.glob(".*.duplicate-tmp-*")), [])

    def test_move_restores_source_if_source_directory_sync_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")
            real_fsync = os.fsync
            fsync_calls = 0

            def fail_source_directory_sync(fd):
                nonlocal fsync_calls
                fsync_calls += 1
                if fsync_calls == 3:
                    raise OSError("simulated source directory sync failure")
                return real_fsync(fd)

            with patch.object(duplicate_files.os, "fsync", side_effect=fail_source_directory_sync):
                with self.assertRaisesRegex(OSError, "source directory sync failure"):
                    duplicate_files._duplicate_move_file(source, target)

            self.assertEqual(source.read_text(encoding="utf-8"), "registered stream")
            self.assertEqual(target.read_text(encoding="utf-8"), "registered stream")
            self.assertEqual(list(root.glob(".*.duplicate-retire-*")), [])
            self.assertEqual(list(root.glob(".*.duplicate-tmp-*")), [])

    def test_move_preserves_source_changed_before_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")
            real_link = os.link

            def change_source_before_publish(temp_path, target_path, **kwargs):
                source.write_text("operator replacement", encoding="utf-8")
                return real_link(temp_path, target_path, **kwargs)

            with patch.object(duplicate_files.os, "link", side_effect=change_source_before_publish):
                with self.assertRaisesRegex(RuntimeError, "Source file changed"):
                    duplicate_files._duplicate_move_file(source, target)

            self.assertEqual(source.read_text(encoding="utf-8"), "operator replacement")
            self.assertEqual(target.read_text(encoding="utf-8"), "registered stream")
            self.assertEqual(list(root.glob(".*.duplicate-tmp-*")), [])

    def test_move_preserves_source_replaced_between_check_and_atomic_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")
            real_rename = os.rename

            def replace_before_retirement(source_path, retired_path, *args, **kwargs):
                if Path(source_path) == source:
                    source.unlink()
                    source.write_text("operator replacement", encoding="utf-8")
                return real_rename(source_path, retired_path, *args, **kwargs)

            with patch.object(duplicate_files.os, "rename", side_effect=replace_before_retirement):
                with self.assertRaisesRegex(RuntimeError, "Source file changed"):
                    duplicate_files._duplicate_move_file(source, target)

            self.assertEqual(source.read_text(encoding="utf-8"), "operator replacement")
            self.assertEqual(target.read_text(encoding="utf-8"), "registered stream")
            self.assertEqual(list(root.glob(".*.duplicate-retire-*")), [])
            self.assertEqual(list(root.glob(".*.duplicate-tmp-*")), [])

    def test_move_preserves_dangling_symlink_replaced_at_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            missing = root / "operator-owned-missing-target"
            source.write_text("registered stream", encoding="utf-8")
            real_rename = os.rename

            def replace_with_symlink_before_retirement(source_path, retired_path, *args, **kwargs):
                if Path(source_path) == source:
                    source.unlink()
                    source.symlink_to(missing)
                return real_rename(source_path, retired_path, *args, **kwargs)

            with patch.object(duplicate_files.os, "rename",
                              side_effect=replace_with_symlink_before_retirement):
                with self.assertRaisesRegex(RuntimeError, "Source file changed"):
                    duplicate_files._duplicate_move_file(source, target)

            self.assertTrue(source.is_symlink())
            self.assertEqual(os.readlink(source), str(missing))
            self.assertFalse(missing.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "registered stream")
            self.assertEqual(list(root.glob(".*.duplicate-retire-*")), [])
            self.assertEqual(list(root.glob(".*.duplicate-tmp-*")), [])

    def test_move_keeps_replaced_source_if_another_file_occupies_restore_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")
            real_rename = os.rename

            def replace_and_reoccupy_source(source_path, retired_path, *args, **kwargs):
                if Path(source_path) == source:
                    source.unlink()
                    source.write_text("replaced before retirement", encoding="utf-8")
                    result = real_rename(source_path, retired_path, *args, **kwargs)
                    source.write_text("new concurrent occupant", encoding="utf-8")
                    return result
                return real_rename(source_path, retired_path, *args, **kwargs)

            with patch.object(duplicate_files.os, "rename", side_effect=replace_and_reoccupy_source):
                with self.assertRaisesRegex(RuntimeError, "preserved in a recovery directory"):
                    duplicate_files._duplicate_move_file(source, target)

            recovery_dirs = list(root.glob(".*.duplicate-retire-*"))
            self.assertEqual(len(recovery_dirs), 1)
            self.assertEqual(source.read_text(encoding="utf-8"), "new concurrent occupant")
            self.assertEqual((recovery_dirs[0] / source.name).read_text(encoding="utf-8"),
                             "replaced before retirement")
            self.assertEqual(target.read_text(encoding="utf-8"), "registered stream")
            self.assertEqual(list(root.glob(".*.duplicate-tmp-*")), [])

    def test_move_publishes_complete_target_then_removes_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.strm"
            target = root / "target.strm"
            source.write_text("registered stream", encoding="utf-8")

            duplicate_files._duplicate_move_file(source, target)

            self.assertFalse(source.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "registered stream")
            self.assertEqual(list(root.glob(".*.duplicate-tmp-*")), [])


class TestDuplicateDismissalIndex(unittest.TestCase):
    def test_many_dismissals_are_partitioned_by_video_and_group(self):
        count = 10_000
        dismissed = {(f"left:{i}", f"right:{i}") for i in range(count)}

        by_video = _index_dismissals(dismissed)

        self.assertEqual(sum(len(pairs) for pairs in by_video.values()), count * 2)
        self.assertEqual(
            _dismissals_for_group({"left:731", "right:731"}, by_video),
            {("left:731", "right:731")},
        )
        # Pairs that touch the group but cross into a different file group must
        # not suppress the group's otherwise-visible same-file candidate.
        crossed = _index_dismissals(dismissed | {("left:731", "right:732")})
        self.assertEqual(
            _dismissals_for_group({"left:731", "right:731"}, crossed),
            {("left:731", "right:731")},
        )

    def test_large_scan_uses_group_local_dismissal_lookup(self):
        group_count = 10_000
        rows = []
        dismissals = []
        for index in range(group_count):
            file_id = f"file{index:04d}"
            left = f"telegram:-1003000:{index * 2}:{file_id}"
            right = f"telegram:-1004000:{index * 2 + 1}:{file_id}"
            rows.extend((
                {"id": left, "provider": "telegram", "title": f"First {index}",
                 "slug": file_id, "duration": 600, "performers": "[]", "studio": "",
                 "thumbnail": None, "source_url": ""},
                {"id": right, "provider": "telegram", "title": f"Second {index}",
                 "slug": file_id, "duration": 600, "performers": "[]", "studio": "",
                 "thumbnail": None, "source_url": ""},
            ))
            dismissals.append((left, right))

        class Connection:
            row_factory = None

            def execute(self, query):
                if "SELECT COUNT(*) FROM videos" in query:
                    class CountResult:
                        def fetchone(self):
                            return (len(rows),)
                    return CountResult()
                if "FROM videos ORDER BY id" in query:
                    return rows
                if "FROM performer_aliases" in query or "FROM video_duplicate_decisions" in query:
                    return []
                if "FROM video_duplicate_dismissals" in query:
                    return dismissals
                raise AssertionError(f"Unexpected query: {query}")

            def close(self):
                pass

        connection = Connection()
        with (patch("video_duplicates.db.connect", return_value=connection),
              patch("video_duplicates.db.table_columns", return_value={
                  "id", "provider", "title", "slug", "duration", "performers",
                  "studio", "thumbnail", "url",
              }),
              patch("video_duplicates.db.table_names", return_value={
                  "videos", "performer_aliases", "video_duplicate_decisions",
                  "video_duplicate_dismissals",
              }),
              patch("video_duplicates._dismissals_for_group",
                    wraps=_dismissals_for_group) as group_lookup):
            result = find_duplicate_candidates("fixture", limit=100)

        self.assertEqual(result["scanned"], group_count * 2)
        self.assertEqual(result["total"], 0)
        self.assertEqual(group_lookup.call_count, group_count)


class TestVideoDuplicateCandidates(PgTestCase):
    def add_video(self, video_id, title, *, provider="source", slug="", duration=600,
                  performers=None, studio=""):
        conn = db.connect(TEST_URL)
        with conn:
            conn.execute(
                "INSERT INTO videos (id, provider, title, slug, duration, performers, studio) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (video_id, provider, title, slug, duration,
                 json.dumps(performers or []), studio),
            )
        conn.close()

    def test_identical_title_does_not_create_suggestion(self):
        self.add_video("a:1", "A Very Specific Scene With Alex", provider="a",
                       duration=601, performers=["Alex", "Blake"])
        self.add_video("b:2", "A Very Specific Scene With Alex", provider="b",
                       duration=603, performers=["Alex", "Casey"])

        result = find_duplicate_candidates(TEST_URL)

        self.assertEqual(result["total"], 0)
        conn = db.connect(TEST_URL)
        try:
            self.assertEqual(conn.execute("SELECT count(*) FROM videos").fetchone()[0], 2)
            tables = db.table_names(conn)
            self.assertIn("videos", tables)
        finally:
            conn.close()

    def test_score_uses_cast_and_time_not_title(self):
        self.add_video("a:1", "Only Fans", duration=1200,
                       performers=["Alex", "Blake"])
        self.add_video("b:2", "Lucas Entertainment", duration=1205,
                       performers=["Alex", "Blake"])

        result = find_duplicate_candidates(TEST_URL)

        self.assertEqual(result["total"], 1)
        self.assertAlmostEqual(result["pairs"][0]["score"], 85.4426357, places=4)
        self.assertEqual(result["pairs"][0]["category"], "Strong suggestion")
        self.assertIn("Shared cast: Alex, Blake", result["pairs"][0]["reasons"])

    def test_score_threshold_boundaries_and_incomplete_data(self):
        def row(duration, cast):
            return {"duration": duration, "performer_norm": set(cast)}

        at_70 = _score(row(3000, "abcd"), row(3014, "abcde"), set("abcd"))
        self.assertAlmostEqual(at_70["score"], 70.0)
        self.assertEqual(at_70["category"], "Needs review")
        at_85 = _score(row(896, "ab"), row(900, "ab"), set("ab"))
        self.assertAlmostEqual(at_85["score"], 85.0)
        self.assertEqual(at_85["category"], "Strong suggestion")
        self.assertLessEqual(at_85["score"], 95)
        self.assertIsNone(_score(row(0, "abc"), row(1, "abc"), set("abc")))

    def test_close_time_with_matching_cast_is_a_candidate(self):
        self.add_video("a:1", "Same title", duration=3000,
                       performers=["A", "B", "C", "D"])
        self.add_video("b:2", "Same title", duration=3015,
                       performers=["A", "B", "C", "D", "E"])
        self.assertEqual(find_duplicate_candidates(TEST_URL)["total"], 1)

    def test_one_shared_actor_does_not_create_suggestion(self):
        self.add_video("a:1", "Men", duration=600, performers=["Alex"])
        self.add_video("b:2", "Men", duration=602, performers=["Alex"])

        self.assertEqual(find_duplicate_candidates(TEST_URL)["total"], 0)

    def test_solo_scene_duplicate_detected(self):
        self.add_video("a:1", "Morning Workout Session 1", duration=900, performers=["Alex"])
        self.add_video("b:2", "Morning Workout Session 1 HD", duration=903, performers=["Alex"])

        result = find_duplicate_candidates(TEST_URL)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pairs"][0]["evidence"], "solo_title_duration")
        self.assertIn("Matching solo performer (Alex)", result["pairs"][0]["reasons"][0])

    def test_solo_scene_different_performer_rejected(self):
        self.add_video("a:1", "Morning Workout Session 1", duration=900, performers=["Alex"])
        self.add_video("b:2", "Morning Workout Session 1", duration=900, performers=["Blake"])

        self.assertEqual(find_duplicate_candidates(TEST_URL)["total"], 0)

    def test_solo_scene_different_duration_rejected(self):
        self.add_video("a:1", "Morning Workout Session 1", duration=900, performers=["Alex"])
        self.add_video("b:2", "Morning Workout Session 1", duration=930, performers=["Alex"])

        self.assertEqual(find_duplicate_candidates(TEST_URL)["total"], 0)

    def test_different_volume_numbers_are_not_duplicates(self):
        self.add_video("a:1", "British Boys Fetish Club Discipline Tales Vol 12",
                       duration=2910, performers=["Discipline"])
        self.add_video("b:2", "British Boys Fetish Club Discipline Tales Vol 131",
                       duration=2905, performers=["Discipline"])

        result = find_duplicate_candidates(TEST_URL)
        self.assertEqual(result["total"], 0)

    def test_discipline_tales_different_volumes_are_not_duplicates(self):
        self.add_video("telegram:-1001618765010:18190:5388927107315301064",
                       "🔥  #SPANKING British Boys Fetish Club Discipline Tales Vol 247",
                       provider="telegram", duration=2913, performers=["Discipline"])
        self.add_video("telegram:-1001618765010:18991:5238030749994623110",
                       "British Boys Fetish Club Discipline Tales Vol 273",
                       provider="telegram", duration=2898, performers=["Discipline"])

        result = find_duplicate_candidates(TEST_URL)
        self.assertEqual(result["total"], 0)

    def test_same_volume_scenes_are_duplicates(self):
        self.add_video("a:1", "🔥  #SPANKING British Boys Fetish Club Discipline Tales Vol 247",
                       duration=2913, performers=["Discipline"])
        self.add_video("b:2", "British Boys Fetish Club Discipline Tales Vol 247 HD",
                       duration=2915, performers=["Discipline"])

        result = find_duplicate_candidates(TEST_URL)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pairs"][0]["evidence"], "solo_title_duration")

    def test_multi_performer_different_parts_are_not_duplicates(self):
        self.add_video("a:1", "Next Stop Vegas - Part 2-1", duration=1200,
                       performers=["Drew Dixon", "Roman Todd"])
        self.add_video("b:2", "Next Stop Vegas - Part 2-2", duration=1205,
                       performers=["Drew Dixon", "Roman Todd"])

        result = find_duplicate_candidates(TEST_URL)
        self.assertEqual(result["total"], 0)

    def test_same_telegram_file_id_groups_different_titles(self):
        self.add_video("telegram:-1001000:1389:5000000000000000001", "#OnlyFans",
                       provider="telegram", slug="5000000000000000001", duration=1220,
                       performers=["Dato Foland", "Gabriel Cross", "Griffin Barrows"])
        self.add_video("telegram:-1001000:1244:5000000000000000001", "Other title",
                       provider="telegram", slug="5000000000000000001", duration=1220,
                       performers=["Dato Foland", "Gabriel Cross", "Griffin Barrows"])
        result = find_duplicate_candidates(TEST_URL)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pairs"][0]["score"], 100.0)
        self.assertEqual(result["pairs"][0]["category"], "Same file")
        self.assertIn("Same Telegram file", result["pairs"][0]["reasons"][0])

    def test_oshash_only_group_is_a_review_suggestion_not_same_file(self):
        first = "telegram:-1001:1:file-a"
        second = "telegram:-1002:2:file-b"
        self.add_video(first, "First upload", provider="telegram", slug="file-a")
        self.add_video(second, "Second upload", provider="telegram", slug="file-b")
        with db.connect(TEST_URL) as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO video_fingerprints (video_id,algorithm,fingerprint) VALUES (%s,'OSHASH',%s)",
                    ((first, "0123456789abcdef"), (second, "0123456789abcdef")),
                )

        exact = find_duplicate_candidates(TEST_URL, category="same_file")
        suggestions = find_duplicate_candidates(TEST_URL, category="strong")

        self.assertEqual(exact["total"], 0)
        self.assertEqual(suggestions["total"], 1)
        pair = suggestions["pairs"][0]
        self.assertEqual(pair["evidence"], "oshash")
        self.assertEqual(pair["category"], "Strong suggestion")
        self.assertLess(pair["score"], 100)
        self.assertIn("Matching OSHASH", pair["reasons"][0])

    def test_telegram_ref_remains_identity_when_slug_conflicts(self):
        self.add_video("telegram:-1001:1:900719925474099312345", "#OnlyFans",
                       provider="telegram", slug="wrong", duration=600)
        self.add_video("telegram:-1001:2:900719925474099312345", "Men",
                       provider="telegram", slug="900719925474099312345", duration=600)
        result = find_duplicate_candidates(TEST_URL)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["pairs"][0]["category"], "Same file")
        self.assertTrue(any("slug" in reason for reason in result["pairs"][0]["reasons"]))

    def test_matching_web_slug_alone_is_not_a_candidate(self):
        self.add_video("web-a:1", "Lucas Entertainment", provider="web-a", slug="same-slug")
        self.add_video("web-b:2", "Lucas Entertainment", provider="web-b", slug="same-slug")
        self.assertEqual(find_duplicate_candidates(TEST_URL)["total"], 0)

    def test_offset_paginates_candidates(self):
        for index, cast in enumerate((["Alex", "Blake"], ["Casey", "Devon"], ["Ellis", "Frankie"])):
            self.add_video(f"a:{index}", f"Scene {index}", provider="a", duration=1200, performers=cast)
            self.add_video(f"b:{index}", f"Other {index}", provider="b", duration=1205, performers=cast)
        full = find_duplicate_candidates(TEST_URL, limit=50)
        self.assertEqual(full["total"], 3)
        self.assertFalse(full["truncated"])
        self.assertEqual(full["offset"], 0)
        pages = [find_duplicate_candidates(TEST_URL, limit=1, offset=offset) for offset in (0, 1, 2)]
        self.assertEqual([len(page["pairs"]) for page in pages], [1, 1, 1])
        self.assertTrue(pages[0]["truncated"])
        self.assertTrue(pages[1]["truncated"])
        self.assertFalse(pages[2]["truncated"])
        self.assertEqual({page["pairs"][0]["left"]["id"] for page in pages}, {"a:0", "a:1", "a:2"})
        past_end = find_duplicate_candidates(TEST_URL, limit=1, offset=99)
        self.assertEqual(past_end["pairs"], [])
        self.assertFalse(past_end["truncated"])

    def test_full_index_is_not_truncated_for_decision_validation(self):
        for index in range(520):
            cast = [f"Actor{index}", f"Partner{index}"]
            self.add_video(f"a:{index}", f"Scene {index}", provider="a",
                           duration=1200, performers=cast)
            self.add_video(f"b:{index}", f"Other {index}", provider="b",
                           duration=1205, performers=cast)
        page = find_duplicate_candidates(TEST_URL, limit=500)
        self.assertEqual(page["total"], 520)
        self.assertTrue(page["truncated"])
        full_index = scan_duplicate_index(TEST_URL)
        self.assertEqual(full_index["total"], 520)
        self.assertTrue(any(
            "a:519" in {pair["left"]["id"], pair["right"]["id"], *(m["id"] for m in pair.get("members", []))}
            for pair in full_index["pairs"]))

    def test_large_exact_duplicate_index_is_complete_and_pages_without_dropping_groups(self):
        group_count = 1200
        rows = []
        for index in range(group_count):
            file_id = f"largefile{index:04d}"
            rows.extend((
                (f"telegram:-1001000:{index * 2}:{file_id}", f"First copy {index}", file_id),
                (f"telegram:-1002000:{index * 2 + 1}:{file_id}", f"Second copy {index}", file_id),
            ))
        with db.connect(TEST_URL) as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO videos (id,provider,title,slug,duration,performers,studio) "
                    "VALUES (%s,'telegram',%s,%s,600,'[]','')", rows,
                )

        scan = scan_duplicate_index(TEST_URL)
        middle = find_duplicate_candidates(TEST_URL, limit=500, offset=500, category="same_file")
        last = find_duplicate_candidates(TEST_URL, limit=500, offset=1000, category="same_file")
        self.assertTrue(scan["complete"])
        self.assertEqual(scan["scanned"], group_count * 2)
        self.assertEqual(scan["total"], group_count)
        self.assertEqual(middle["total"], group_count)
        self.assertEqual(len(middle["pairs"]), 500)
        self.assertTrue(middle["truncated"])
        self.assertEqual(len(last["pairs"]), 200)
        self.assertFalse(last["truncated"])
        flattened = [member["id"] for pair in (*middle["pairs"], *last["pairs"])
                     for member in pair["members"]]
        self.assertEqual(len(flattened), 1400)
        self.assertEqual(len(set(flattened)), 1400)

    def test_large_exact_duplicate_scan_uses_active_dismissals_per_group(self):
        group_count = 5_000
        videos = []
        dismissals = []
        for index in range(group_count):
            file_id = f"dismissedfile{index:04d}"
            left = f"telegram:-1003000:{index * 2}:{file_id}"
            right = f"telegram:-1004000:{index * 2 + 1}:{file_id}"
            videos.extend(((left, f"First {index}", file_id), (right, f"Second {index}", file_id)))
            dismissals.append((left, right, 1, f"dismiss-{index}", 1))

        with db.connect(TEST_URL) as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO videos (id,provider,title,slug,duration,performers,studio) "
                    "VALUES (%s,'telegram',%s,%s,600,'[]','')", videos,
                )
                conn.executemany(
                    "INSERT INTO video_duplicate_dismissals "
                    "(left_id,right_id,active,operation_id,created_at) VALUES (%s,%s,%s,%s,%s)",
                    dismissals,
                )

        scan = scan_duplicate_index(TEST_URL)
        self.assertTrue(scan["complete"])
        self.assertEqual(scan["scanned"], group_count * 2)
        self.assertEqual(scan["total"], 0)

    def test_category_filter_applies_before_pagination(self):
        self.add_video("telegram:-1001:1:shared", "Alpha", provider="telegram",
                       duration=600, performers=["A", "B"])
        self.add_video("telegram:-1001:2:shared", "Beta", provider="telegram",
                       duration=600, performers=["A", "B"])
        self.add_video("a:1", "Only Fans", provider="a", duration=1200, performers=["Alex", "Blake"])
        self.add_video("b:2", "Lucas Entertainment", provider="b", duration=1205, performers=["Alex", "Blake"])
        self.assertEqual(find_duplicate_candidates(TEST_URL, limit=50)["total"], 2)
        same = find_duplicate_candidates(TEST_URL, limit=50, category="same_file")
        self.assertEqual(same["total"], 1)
        self.assertEqual(same["pairs"][0]["category"], "Same file")
        self.assertEqual(same["category"], "same_file")
        with patch.object(video_duplicates, "_candidate_pairs",
                          side_effect=AssertionError("same-file scan must not run heuristics")):
            exact_only = find_duplicate_candidates(TEST_URL, limit=50, category="same_file")
        self.assertTrue(exact_only["complete"])
        self.assertEqual(exact_only["total"], 1)
        strong = find_duplicate_candidates(TEST_URL, limit=50, category="strong")
        self.assertEqual(strong["total"], 1)
        self.assertEqual(strong["pairs"][0]["category"], "Strong suggestion")
        review = find_duplicate_candidates(TEST_URL, limit=50, category="review")
        self.assertEqual(review["total"], 0)
        self.assertEqual(review["pairs"], [])

    def test_same_file_group_over_250_is_one_group(self):
        for i in range(260):
            self.add_video(f"telegram:-1001:{i+1}:hugeopaqueid", f"title {i}", provider="telegram",
                           slug="hugeopaqueid", duration=600, performers=[])
        result = find_duplicate_candidates(TEST_URL, limit=5)
        self.assertEqual(result["total"], 1)
        self.assertEqual(len(result["pairs"][0]["members"]), 260)
        self.assertFalse(result["truncated"])

    def test_handles_null_performers_and_caps_results(self):
        self.add_video("a:1", "This Is A Very Specific Scene", duration=600, performers=["Alex", "Blake"])
        self.add_video("b:2", "Different title", duration=602, performers=["Alex", "Blake"])
        conn = db.connect(TEST_URL)
        with conn:
            conn.execute("UPDATE videos SET performers = NULL WHERE id = 'a:1'")
        conn.close()

        result = find_duplicate_candidates(TEST_URL, limit=0)

        self.assertEqual(len(result["pairs"]), 0)
        self.assertEqual(result["total"], 0)
        self.assertFalse(result["truncated"])


    def test_v2_duplicate_route_paginates_real_large_candidate_index(self):
        group_count = 1200
        padding_count = 48_000
        videos = []
        for index in range(group_count):
            file_id = f"routefile{index:04d}"
            videos.extend((
                (f"telegram:-1005000:{index * 2}:{file_id}", "telegram", f"First {index}", file_id),
                (f"telegram:-1006000:{index * 2 + 1}:{file_id}", "telegram", f"Second {index}", file_id),
            ))
        videos.extend(
            (f"catalog-padding:{index:05d}", "other", f"Unrelated item {index}", "")
            for index in range(padding_count)
        )
        with db.connect(TEST_URL) as conn:
            with conn:
                conn.executemany(
                    "INSERT INTO videos (id,provider,title,slug,duration,performers,studio) "
                    "VALUES (%s,%s,%s,%s,600,'[]','')", videos,
                )

        admin_v2 = SimpleNamespace(
            _admin_blocked=lambda _request: False,
            JSONResponse=JSONResponse,
            asyncio=asyncio,
            HUB_DB_URL=TEST_URL,
        )
        router = admin_v2_jobs.build_router(admin_v2)
        endpoint = next(route.endpoint for route in router.routes
                        if route.path == "/api/v2/duplicates")

        def make_request(offset):
            path = "/admin/api/v2/duplicates"
            return Request({
                "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
                "path": path, "raw_path": path.encode(),
                "query_string": f"limit=200&offset={offset}&category=same_file".encode(),
                "headers": [], "client": ("127.0.0.1", 1), "server": ("test", 80),
                "root_path": "",
            })

        pages = [asyncio.run(endpoint(make_request(offset), 200, offset, "same_file"))
                 for offset in range(0, group_count, 200)]
        flattened = [member["id"] for page in pages for pair in page["pairs"]
                     for member in pair["members"]]

        self.assertEqual(len(pages), 6)
        self.assertTrue(all(page["ok"] for page in pages))
        self.assertTrue(all(page["total"] == group_count for page in pages))
        self.assertTrue(all(page["scanned"] == group_count * 2 + padding_count for page in pages))
        self.assertTrue(all(page["complete"] for page in pages))
        self.assertTrue(all(len(page["pairs"]) == 200 for page in pages))
        self.assertEqual(len(flattened), group_count * 2)
        self.assertEqual(len(set(flattened)), group_count * 2)


if __name__ == "__main__":
    unittest.main()
