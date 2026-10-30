"""Tests for restore_db.py and common.db.pg_restore."""

from __future__ import annotations

import gzip
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import restore_db


class TestRestoreDb(unittest.TestCase):
    def test_missing_source_returns_1(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_restore_") as tmp:
            dump_file = Path(tmp) / "test.dump"
            dump_file.write_bytes(b"DATA")
            argv = ["restore_db.py", "--db-url", "", "--file", str(dump_file)]
            with mock.patch.dict(os.environ, {"DATABASE_URL": ""}, clear=True), \
                 mock.patch.object(sys, "argv", argv):
                rc = restore_db.main()
            self.assertEqual(rc, 1)

    def test_missing_binaries_returns_1(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_restore_") as tmp:
            dump_file = Path(tmp) / "test.dump"
            dump_file.write_bytes(b"DATA")
            argv = ["restore_db.py", "--db-url", "postgresql://u:p@127.0.0.1:5432/db", "--file", str(dump_file)]
            with mock.patch("shutil.which", return_value=None), \
                 mock.patch.object(sys, "argv", argv):
                rc = restore_db.main()
            self.assertEqual(rc, 1)

    def test_missing_file_returns_1(self):
        argv = [
            "restore_db.py",
            "--db-url", "postgresql://u:p@127.0.0.1:5432/db",
            "--file", "/nonexistent/dump.dump.gz",
        ]
        with mock.patch("shutil.which", return_value="/usr/bin/pg_restore"), \
             mock.patch.object(sys, "argv", argv):
            rc = restore_db.main()
        self.assertEqual(rc, 1)

    def test_dry_run_lists_contents_without_restoring(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_restore_") as tmp:
            dump_file = Path(tmp) / "test.dump"
            dump_file.write_bytes(b"DUMP_DATA")

            argv = [
                "restore_db.py",
                "--dry-run",
                "--file", str(dump_file),
            ]

            calls = []

            def fake_run(cmd, *args, **kwargs):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout=b"1; 1259 16384 TABLE public videos adulthub\n", stderr=b"")

            with mock.patch("shutil.which", return_value="/usr/bin/pg_restore"), \
                 mock.patch("subprocess.run", side_effect=fake_run), \
                 mock.patch.object(sys, "argv", argv):
                rc = restore_db.main()

            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            self.assertIn("--list", calls[0])

    def test_restore_success_invokes_pg_restore_and_migrations(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_restore_") as tmp:
            dump_file = Path(tmp) / "adulthub-20261003-1200.dump.gz"
            with gzip.open(dump_file, "wb") as gz:
                gz.write(b"MOCK_PG_DUMP_BYTES")

            argv = [
                "restore_db.py",
                "--db-url", "postgresql://u:p@127.0.0.1:5432/db",
                "--file", str(dump_file),
                "--yes",
            ]

            calls = []

            def fake_run(cmd, *args, **kwargs):
                calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, stdout=b"OK", stderr=b"")

            with mock.patch("shutil.which", return_value="/usr/bin/pg_restore"), \
                 mock.patch("subprocess.run", side_effect=fake_run), \
                 mock.patch("common.migrations.apply_migrations", return_value=[]) as mock_migrate, \
                 mock.patch.object(sys, "argv", argv):
                rc = restore_db.main()

            self.assertEqual(rc, 0)
            # Verify verify call (--list) and restore call
            self.assertEqual(len(calls), 2)
            self.assertIn("--list", calls[0])
            self.assertIn("--clean", calls[1])
            self.assertIn("--if-exists", calls[1])
            self.assertIn("--no-owner", calls[1])
            self.assertIn("--no-privileges", calls[1])
            mock_migrate.assert_called_once()

    def test_latest_dump_auto_discovery(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_restore_") as tmp:
            backup_dir = Path(tmp)
            old_dump = backup_dir / "adulthub-20260101-0000.dump.gz"
            new_dump = backup_dir / "adulthub-20260102-0000.dump.gz"
            old_dump.write_bytes(b"OLD")
            new_dump.write_bytes(b"NEW")
            os.utime(old_dump, (1000, 1000))
            os.utime(new_dump, (2000, 2000))

            found = restore_db.find_latest_dump(backup_dir)
            self.assertEqual(found, new_dump)

    def test_latest_dump_ignores_checksum_sidecars_and_temp_files(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_restore_") as tmp:
            backup_dir = Path(tmp)
            dump = backup_dir / "adulthub-20260103-0000.dump.gz"
            dump.write_bytes(b"NEW")
            sidecar = dump.with_name(dump.name + ".sha256")
            sidecar.write_text("0" * 64 + f"  {dump.name}\n")
            temp_file = backup_dir / f".{dump.name}.deadbeef.dump"
            temp_file.write_bytes(b"PARTIAL")
            os.utime(dump, (1000, 1000))
            os.utime(sidecar, (3000, 3000))
            os.utime(temp_file, (4000, 4000))

            found = restore_db.find_latest_dump(backup_dir)
            self.assertEqual(found, dump)

    def test_checksum_mismatch_aborts_before_restore(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_restore_") as tmp:
            dump_file = Path(tmp) / "adulthub-20261003-1200.dump.gz"
            with gzip.open(dump_file, "wb") as gz:
                gz.write(b"MOCK_PG_DUMP_BYTES")
            digest = hashlib.sha256(dump_file.read_bytes()).hexdigest()
            wrong = "0" * 64 if digest != "0" * 64 else "1" * 64
            dump_file.with_name(dump_file.name + ".sha256").write_text(
                f"{wrong}  {dump_file.name}\n")

            argv = [
                "restore_db.py",
                "--db-url", "postgresql://u:p@127.0.0.1:5432/db",
                "--file", str(dump_file),
                "--yes",
            ]

            with mock.patch("shutil.which", return_value="/usr/bin/pg_restore"), \
                 mock.patch("restore_db.db.pg_restore") as restore, \
                 mock.patch.object(sys, "argv", argv):
                rc = restore_db.main()

            self.assertEqual(rc, 1)
            restore.assert_not_called()

    def test_matching_checksum_allows_restore(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_restore_") as tmp:
            dump_file = Path(tmp) / "adulthub-20261003-1200.dump.gz"
            with gzip.open(dump_file, "wb") as gz:
                gz.write(b"MOCK_PG_DUMP_BYTES")
            digest = hashlib.sha256(dump_file.read_bytes()).hexdigest()
            dump_file.with_name(dump_file.name + ".sha256").write_text(
                f"{digest}  {dump_file.name}\n")

            argv = [
                "restore_db.py",
                "--db-url", "postgresql://u:p@127.0.0.1:5432/db",
                "--file", str(dump_file),
                "--yes",
            ]

            with mock.patch("shutil.which", return_value="/usr/bin/pg_restore"), \
                 mock.patch("restore_db.db.pg_restore", return_value=True) as restore, \
                 mock.patch.object(sys, "argv", argv):
                rc = restore_db.main()

            self.assertEqual(rc, 0)
            restore.assert_called_once()
            self.assertEqual(Path(restore.call_args.kwargs["dump_path"]), dump_file)


if __name__ == "__main__":
    unittest.main()
