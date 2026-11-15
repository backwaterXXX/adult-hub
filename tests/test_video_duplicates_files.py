"""Tests for atomic duplicate file moves, symlink protection, and retirement rollbacks."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sync.duplicate_files as duplicate_files


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



if __name__ == "__main__":
    unittest.main()
