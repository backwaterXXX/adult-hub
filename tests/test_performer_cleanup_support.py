"""Tests for common.performer_cleanup_support."""

from __future__ import annotations

import gzip
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


if __name__ == "__main__":
    unittest.main()
