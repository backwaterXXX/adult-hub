"""Backup checksum and archive verification behavior."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resolver"))

from admin_v2_backups import _verify


class TestBackupVerification(unittest.TestCase):
    def test_checksum_sidecar_accepts_absolute_path_with_matching_basename(self):
        with tempfile.TemporaryDirectory(prefix="admin_backup_verify_") as tmp:
            backup = Path(tmp) / "adult_hub-test.db"
            conn = sqlite3.connect(backup)
            try:
                conn.execute("CREATE TABLE videos (id TEXT PRIMARY KEY)")
            finally:
                conn.close()
            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
            Path(f"{backup}.sha256").write_text(
                f"{digest}  /data/backups/{backup.name}\n", encoding="ascii")

            result = _verify(backup)

        self.assertEqual(result["checksum_status"], "valid")
        self.assertEqual(result["archive_status"], "valid")

    def test_checksum_sidecar_rejects_a_different_basename_even_when_digest_matches(self):
        with tempfile.TemporaryDirectory(prefix="admin_backup_verify_") as tmp:
            backup = Path(tmp) / "adult_hub-test.db"
            backup.write_bytes(b"not a database")
            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
            Path(f"{backup}.sha256").write_text(
                f"{digest}  /data/backups/another.db\n", encoding="ascii")

            result = _verify(backup)

        self.assertEqual(result["checksum_status"], "mismatch")
        self.assertEqual(result["archive_status"], "invalid")


if __name__ == "__main__":
    unittest.main()
