"""Tests for common.performer_cleanup_support."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import common.performer_cleanup_support as pcs


class PerformerCleanupSupportTest(unittest.TestCase):
    def test_docker_pg_dump_backup_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stdout = b"CREATE TABLE dummy (id int);"

            with patch("subprocess.run", return_value=mock_res) as mock_run:
                backup_file = pcs.docker_pg_dump_backup(tmp_path, "test_prefix")

            mock_run.assert_called_once()
            self.assertTrue(backup_file.exists())
            self.assertTrue(backup_file.name.startswith("test_prefix-"))
            self.assertTrue(backup_file.name.endswith(".sql.gz"))

            with gzip.open(backup_file, "rb") as f:
                self.assertEqual(f.read(), b"CREATE TABLE dummy (id int);")

    def test_docker_pg_dump_backup_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            mock_res = MagicMock()
            mock_res.returncode = 1
            mock_res.stderr = b"Connection refused to docker daemon"

            with patch("subprocess.run", return_value=mock_res), \
                 self.assertRaises(RuntimeError) as cm:
                pcs.docker_pg_dump_backup(tmp_path, "failed_prefix")
            self.assertIn("docker pg_dump failed: Connection refused", str(cm.exception))

    def test_create_db_backup_pg_backup_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            expected_path = tmp_path / "backup.sql.gz"
            with patch("common.db.pg_backup", return_value=expected_path) as mock_pg:
                result = pcs.create_db_backup("postgresql://host/db", tmp_path, "pref")

            self.assertEqual(result, expected_path)
            mock_pg.assert_called_once_with("postgresql://host/db", tmp_path, prefix="pref")

    def test_create_db_backup_fallback_on_runtime_error(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            expected_path = tmp_path / "fallback.sql.gz"
            with patch("common.db.pg_backup", side_effect=RuntimeError("pg_dump not in PATH")), \
                 patch(
                     "common.performer_cleanup_support.docker_pg_dump_backup",
                     return_value=expected_path,
                 ) as mock_docker:
                result = pcs.create_db_backup(None, tmp_path, "pref")

            self.assertEqual(result, expected_path)
            mock_docker.assert_called_once_with(tmp_path, "pref")

    def test_remove_person_dir_counted_rmtree_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            person_dir = tmp_path / "people" / "A" / "Alice"
            person_dir.mkdir(parents=True)
            report_mock = MagicMock()

            count = pcs.remove_person_dir_counted(
                person_dir,
                base_dir=tmp_path,
                people_dir=tmp_path / "people",
                report=report_mock,
            )

            self.assertEqual(count, 1)
            self.assertFalse(person_dir.exists())
            report_mock.assert_called_once()
            self.assertIn("Removed Jellyfin People folder: people/A/Alice", report_mock.call_args[0][0])

    def test_remove_person_dir_counted_rmtree_fails_docker_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            people_dir = tmp_path / "people"
            person_dir = people_dir / "B" / "Bob"
            report_mock = MagicMock()

            mock_docker_res = MagicMock()
            mock_docker_res.returncode = 0

            with patch("shutil.rmtree", side_effect=PermissionError("Permission denied")), \
                 patch("subprocess.run", return_value=mock_docker_res) as mock_run:
                count = pcs.remove_person_dir_counted(
                    person_dir,
                    base_dir=tmp_path,
                    people_dir=people_dir,
                    report=report_mock,
                )

            self.assertEqual(count, 1)
            mock_run.assert_called_once_with(
                ["docker", "exec", "jellyfin", "rm", "-rf", "/config/metadata/People/B/Bob"],
                capture_output=True,
            )
            self.assertTrue(any("via docker" in call[0][0] for call in report_mock.call_args_list))

    def test_remove_person_dir_via_docker_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            people_dir = tmp_path / "people"
            person_dir = people_dir / "C" / "Charlie"
            report_mock = MagicMock()

            mock_docker_res = MagicMock()
            mock_docker_res.returncode = 1

            orig_error = PermissionError("denied")
            with patch("subprocess.run", return_value=mock_docker_res):
                count = pcs.remove_person_dir_via_docker(
                    person_dir,
                    orig_error,
                    base_dir=tmp_path,
                    people_dir=people_dir,
                    report=report_mock,
                )

            self.assertEqual(count, 0)
            self.assertTrue(any("Error removing" in call[0][0] for call in report_mock.call_args_list))

    def test_remove_person_dir_via_docker_exception(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            outside_dir = tmp_path / "outside" / "Dave"
            report_mock = MagicMock()

            count = pcs.remove_person_dir_via_docker(
                outside_dir,
                RuntimeError("some err"),
                base_dir=tmp_path,
                people_dir=tmp_path / "people",
                report=report_mock,
            )

            self.assertEqual(count, 0)
            self.assertTrue(any("Error removing" in call[0][0] for call in report_mock.call_args_list))

    def test_iter_jellyfin_person_dirs_nonexistent(self):
        with tempfile.TemporaryDirectory() as td:
            nonexistent = Path(td) / "missing_people"
            self.assertEqual(list(pcs.iter_jellyfin_person_dirs(nonexistent)), [])

    def test_iter_jellyfin_person_dirs_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            people_dir = tmp_path / "people"
            people_dir.mkdir()

            (people_dir / "notes.txt").write_text("info")

            dir_a = people_dir / "A"
            dir_a.mkdir()
            actor_1 = dir_a / "Actor 1"
            actor_1.mkdir()
            (dir_a / "ignore_file.nfo").write_text("ignore")

            dir_b = people_dir / "B"
            dir_b.mkdir()
            actor_2 = dir_b / "Actor 2"
            actor_2.mkdir()

            dir_c = people_dir / "C"
            dir_c.mkdir()

            yielded = list(pcs.iter_jellyfin_person_dirs(people_dir))
            self.assertEqual(set(yielded), {actor_1, actor_2})

    def test_build_video_nfo(self):
        mock_conn = MagicMock()
        mock_sync_api = MagicMock()
        mock_sync_api.load_performer_thumbs.return_value = {"Alice": "http://thumb/alice"}
        mock_sync_api.format_dateadded.return_value = "2026-01-01 12:00:00"

        row = (
            "telegram:chan:123",  # id
            "telegram",           # provider
            "Sample Video",       # title
            "sample-video",       # slug
            3600,                 # duration
            "http://thumb/v.jpg", # thumb
            "ignored_raw_perfs",  # _perfs_raw
            json.dumps(["Cat1", "Cat2"]),  # cats_raw
            json.dumps(["Tag1", "Tag2"]),  # tags_raw
            "Studio X",           # studio
            1700000000.0,         # first_seen
        )

        new_perfs = ["Alice", "Bob"]
        video_dict, genres, tag_set = pcs.build_video_nfo(
            mock_conn, row, new_perfs, sync_api=mock_sync_api
        )

        self.assertEqual(video_dict["id"], "telegram:chan:123")
        self.assertEqual(video_dict["provider"], "telegram")
        self.assertEqual(video_dict["provider_video_id"], "chan:123")
        self.assertEqual(video_dict["title"], "Sample Video")
        self.assertEqual(video_dict["slug"], "sample-video")
        self.assertEqual(video_dict["duration"], 3600)
        self.assertEqual(video_dict["thumbnail"], "http://thumb/v.jpg")
        self.assertEqual(video_dict["performers"], ["Alice", "Bob"])
        self.assertEqual(video_dict["performer_thumbs"], {"Alice": "http://thumb/alice"})
        self.assertEqual(video_dict["studio"], "Studio X")
        self.assertEqual(video_dict["dateadded"], "2026-01-01 12:00:00")

        self.assertEqual(genres, {"Cat1", "Cat2"})
        self.assertEqual(tag_set, {"Tag1", "Tag2"})

    def test_regenerate_telegram_nfo_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchone.return_value = None
            mock_sync_api = MagicMock()

            ok = pcs.regenerate_telegram_nfo(
                mock_conn, "telegram:123", ["Performer 1"], media_dir=Path(td), sync_api=mock_sync_api
            )
            self.assertFalse(ok)

    def test_regenerate_telegram_nfo_file_missing(self):
        with tempfile.TemporaryDirectory() as td:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchone.return_value = (
                "telegram", "Title", "slug", 100, "thumb", json.dumps(["Cat"]),
                json.dumps(["Tag"]), "Studio", 1700000000, "description plot",
            )
            mock_sync_api = MagicMock()
            mock_sync_api.stable_media_basename.return_value = "video_base"

            ok = pcs.regenerate_telegram_nfo(
                mock_conn, "telegram:123", ["Performer 1"], media_dir=Path(td), sync_api=mock_sync_api
            )
            self.assertFalse(ok)

    def test_regenerate_telegram_nfo_success(self):
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchone.return_value = (
                "telegram", None, None, None, None, ["CatA"],
                ["TagA"], None, None, "Plot summary",
            )
            mock_sync_api = MagicMock()
            mock_sync_api.stable_media_basename.return_value = "video_base"
            mock_sync_api.load_performer_thumbs.return_value = {"Performer 1": "thumb.jpg"}
            mock_sync_api.format_dateadded.return_value = "2026-01-01"

            nfo_file = tmp_path / "video_base.nfo"
            nfo_file.write_text("<movie></movie>")

            ok = pcs.regenerate_telegram_nfo(
                mock_conn, "telegram:123", ["Performer 1"], media_dir=tmp_path, sync_api=mock_sync_api
            )
            self.assertTrue(ok)

            mock_sync_api.create_nfo_file.assert_called_once()
            call_args = mock_sync_api.create_nfo_file.call_args[0]
            video_dict = call_args[0]
            cats = call_args[1]
            tags = call_args[2]
            path = call_args[3]

            self.assertEqual(video_dict["id"], "telegram:123")
            self.assertEqual(video_dict["title"], "telegram:123")
            self.assertEqual(video_dict["plot"], "Plot summary")
            self.assertEqual(cats, {"CatA"})
            self.assertEqual(tags, {"TagA"})
            self.assertEqual(path, nfo_file)


if __name__ == "__main__":
    unittest.main()
