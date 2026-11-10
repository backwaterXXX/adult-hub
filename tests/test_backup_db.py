"""Tests for backup_db.py (PostgreSQL pg_dump/pg_restore snapshot testing)."""

import gzip
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


import backup_db
from common.db import pg_backup, pg_restore_test, restore_test_target_available


class TestBackupDb(unittest.TestCase):
    def test_restore_target_availability_rejects_missing_malformed_and_primary_urls(self):
        primary = "postgresql://admin:secret@localhost:5432/catalog"
        self.assertTrue(restore_test_target_available(
            "postgresql://restore:secret@localhost:5433/maintenance", primary
        ))
        for candidate in (
            "",
            "not-a-postgres-uri",
            "postgresql://restore:secret@localhost:not-a-port/maintenance",
            "postgresql://restore:secret@localhost:5432/maintenance?host=other",
            "postgresql://restore:secret@one,two:5432/maintenance",
            "postgresql://restore:secret@localhost:5432/catalog?dbname=other&database=catalog",
            "postgresql://admin:secret@localhost:5432/catalog",
            "postgresql://admin:secret@localhost/catalog",
            "postgresql://admin:secret@localhost:5432/maintenance?dbname=catalog",
        ):
            with self.subTest(candidate="same-primary" if candidate.endswith("catalog") else "invalid"):
                self.assertFalse(restore_test_target_available(candidate, primary))

    def test_restore_target_availability_distinguishes_unix_socket_from_tcp(self):
        from psycopg.conninfo import conninfo_to_dict

        primary = "postgresql:///catalog?host=%2Fvar%2Frun%2Fpostgresql"
        self.assertEqual(
            conninfo_to_dict("postgresql:///maintenance?host=%2Fvar%2Frun%2Fpostgresql")["host"],
            "/var/run/postgresql",
        )
        self.assertEqual(
            conninfo_to_dict("postgresql://%2Fvar%2Frun%2Fpostgresql/maintenance")["host"],
            "/var/run/postgresql",
        )
        self.assertTrue(restore_test_target_available(
            "postgresql://restore:secret@localhost:5432/maintenance", primary
        ))
        self.assertFalse(restore_test_target_available(
            "postgresql:///catalog?host=%2Fvar%2Frun%2Fpostgresql", primary
        ))
        self.assertFalse(restore_test_target_available(
            "postgresql:///catalog?host=localhost&port=5432",
            "postgresql://restore:secret@localhost:5432/catalog",
        ))
        self.assertFalse(restore_test_target_available(
            "postgresql:///unused?host=localhost&port=5432&dbname=catalog%252Fdb",
            "postgresql://restore:secret@localhost:5432/catalog%252Fdb",
        ))
        self.assertTrue(restore_test_target_available(
            "postgresql:///maintenance?host=%2Fvar%2Frun%2Fpostgresql",
            primary,
        ))
        self.assertTrue(restore_test_target_available(
            "postgresql://%2Fvar%2Frun%2Fpostgresql/maintenance",
            "postgresql://restore:secret@localhost:5432/catalog",
        ))

    def test_restore_test_preserves_unix_socket_target_for_psycopg_and_pg_restore(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            backup = Path(tmp) / "adulthub-socket-target.dump.gz"
            with gzip.open(backup, "wb") as archive:
                archive.write(b"valid enough to pass the mocked archive listing")
            import hashlib
            Path(f"{backup}.sha256").write_text(
                f"{hashlib.sha256(backup.read_bytes()).hexdigest()}  {backup.name}\n",
                encoding="ascii",
            )

            admin_conn = mock.MagicMock()
            admin_conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
            restored_conn = mock.MagicMock()
            restored_conn.__enter__.return_value = restored_conn
            restored_conn.cursor.return_value.__enter__.return_value.fetchone.side_effect = [
                (True,), (7,),
            ]
            target = ("postgresql://%2Fvar%2Frun%2Fpostgresql/maintenance?dbname=maintenance"
                      "&user=restore&password=secret%2524value")
            command_results = [
                subprocess.CompletedProcess(["pg_restore", "--list"], 0),
                subprocess.CompletedProcess(["pg_restore", "--dbname"], 0),
            ]
            with mock.patch("shutil.which", return_value="/usr/bin/pg_restore"), \
                 mock.patch("subprocess.run", side_effect=command_results) as run, \
                 mock.patch("common.db.psycopg.connect", side_effect=[admin_conn, restored_conn]) as connect:
                result = pg_restore_test(
                    backup, target,
                    primary_db_url="postgresql://user:secret@db:5432/catalog",
                    job_id="c" * 32,
                )

            self.assertEqual(result, {"outcome": "passed", "catalog_rows": 7})
            self.assertEqual(connect.call_args_list[0].args[0],
                             "postgresql://%2Fvar%2Frun%2Fpostgresql/postgres"
                             "?user=restore&password=secret%2524value")
            self.assertIn("/adult_hub_restore_test_" + "c" * 32,
                          connect.call_args_list[1].args[0])
            self.assertNotIn("dbname=", connect.call_args_list[1].args[0])
            restore_environment = run.call_args_list[1].kwargs["env"]
            self.assertEqual(restore_environment["PGHOST"], "/var/run/postgresql")
            self.assertEqual(restore_environment["PGUSER"], "restore")
            self.assertEqual(restore_environment["PGPASSWORD"], "secret%24value")
            self.assertTrue(admin_conn.close.called)

    def test_restore_test_does_not_report_pass_when_database_cleanup_fails(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            backup = Path(tmp) / "adulthub-cleanup-failure.dump.gz"
            with gzip.open(backup, "wb") as archive:
                archive.write(b"valid enough to pass the mocked archive listing")
            import hashlib
            Path(f"{backup}.sha256").write_text(
                f"{hashlib.sha256(backup.read_bytes()).hexdigest()}  {backup.name}\n",
                encoding="ascii",
            )

            admin_conn = mock.MagicMock()
            admin_cursor = admin_conn.cursor.return_value.__enter__.return_value
            admin_cursor.fetchone.return_value = None

            def fail_drop(query, _params=None):
                if "DROP DATABASE IF EXISTS" in str(query):
                    raise RuntimeError("database still in use")
            admin_cursor.execute.side_effect = fail_drop

            restored_conn = mock.MagicMock()
            restored_conn.__enter__.return_value = restored_conn
            restored_conn.cursor.return_value.__enter__.return_value.fetchone.side_effect = [
                (True,), (7,),
            ]
            command_results = [
                subprocess.CompletedProcess(["pg_restore", "--list"], 0),
                subprocess.CompletedProcess(["pg_restore", "--dbname"], 0),
            ]
            with mock.patch("shutil.which", return_value="/usr/bin/pg_restore"), \
                 mock.patch("subprocess.run", side_effect=command_results), \
                 mock.patch("common.db.psycopg.connect", side_effect=[admin_conn, restored_conn]):
                with self.assertRaisesRegex(RuntimeError, "cleanup could not be confirmed"):
                    pg_restore_test(
                        backup,
                        "postgresql://restore:secret@db:5433/maintenance",
                        primary_db_url="postgresql://user:secret@db:5432/catalog",
                        job_id="d" * 32,
                    )

    def test_restore_test_rejects_checksum_mismatch_before_database_access(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            backup = Path(tmp) / "adulthub-corrupt.dump.gz"
            backup.write_bytes(b"not the recorded archive")
            Path(f"{backup}.sha256").write_text(f"{'0' * 64}  {backup.name}\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "checksum does not match"):
                pg_restore_test(backup, "postgresql://user:secret@localhost:5432/maintenance",
                                primary_db_url="postgresql://user:secret@localhost:5432/catalog",
                                job_id="a" * 32)

    def test_restore_test_refuses_the_primary_database_as_maintenance_target(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            backup = Path(tmp) / "adulthub-valid.dump.gz"
            backup.write_bytes(b"archive")
            import hashlib
            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
            Path(f"{backup}.sha256").write_text(f"{digest}  {backup.name}\n", encoding="ascii")
            primary = "postgresql://user:secret@localhost:5432/catalog"
            with self.assertRaisesRegex(ValueError, "must not point at the primary"):
                pg_restore_test(backup, primary, primary_db_url=primary, job_id="a" * 32)

    def test_restore_test_rejects_invalid_archive_before_connecting_to_postgres(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            backup = Path(tmp) / "adulthub-invalid.dump.gz"
            with gzip.open(backup, "wb") as archive:
                archive.write(b"not a PostgreSQL archive")
            import hashlib
            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
            Path(f"{backup}.sha256").write_text(f"{digest}  {backup.name}\n", encoding="ascii")
            failed_listing = subprocess.CompletedProcess(["pg_restore", "--list"], 1)
            with mock.patch("shutil.which", return_value="/usr/bin/pg_restore"), \
                 mock.patch("subprocess.run", return_value=failed_listing), \
                 mock.patch("common.db.psycopg.connect") as connect:
                with self.assertRaisesRegex(ValueError, "Backup archive is invalid"):
                    pg_restore_test(backup, "postgresql://user:secret@localhost:5432/maintenance",
                                    primary_db_url="postgresql://user:secret@localhost:5432/catalog",
                                    job_id="a" * 32)
            connect.assert_not_called()

    def test_restore_test_drops_disposable_database_when_restore_fails(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            backup = Path(tmp) / "adulthub-restore-failure.dump.gz"
            with gzip.open(backup, "wb") as archive:
                archive.write(b"valid enough to pass the mocked archive listing")
            import hashlib
            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
            Path(f"{backup}.sha256").write_text(f"{digest}  {backup.name}\n", encoding="ascii")

            class FakeCursor:
                def __init__(self):
                    self.commands = []

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def execute(self, query, _params=None):
                    self.commands.append(str(query))

                def fetchone(self):
                    return None

            class FakeConnection:
                def __init__(self):
                    self.fake_cursor = FakeCursor()
                    self.closed = False

                def cursor(self):
                    return self.fake_cursor

                def close(self):
                    self.closed = True

            connection = FakeConnection()
            command_results = [
                subprocess.CompletedProcess(["pg_restore", "--list"], 0),
                subprocess.CompletedProcess(["pg_restore", "--dbname"], 1),
            ]
            with mock.patch("shutil.which", return_value="/usr/bin/pg_restore"), \
                 mock.patch("subprocess.run", side_effect=command_results), \
                 mock.patch("common.db.psycopg.connect", return_value=connection) as connect:
                with self.assertRaisesRegex(RuntimeError, "restore into the disposable database failed"):
                    pg_restore_test(backup, "postgresql://user:secret@localhost:5432/maintenance",
                                    primary_db_url="postgresql://user:secret@localhost:5432/catalog",
                                    job_id="b" * 32)

            self.assertTrue(any("CREATE DATABASE" in sql for sql in connection.fake_cursor.commands))
            self.assertTrue(any("DROP DATABASE IF EXISTS" in sql for sql in connection.fake_cursor.commands))
            self.assertTrue(connection.closed)
            self.assertEqual(connect.call_count, 1)

    def test_job_scoped_backup_retry_reuses_verified_artifact(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            job_id = "a" * 32
            def fake_run(cmd, *args, **kwargs):
                if "pg_dump" in cmd[0]:
                    Path(cmd[cmd.index("--file") + 1]).write_bytes(b"SAFE_DUMP")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
            with mock.patch("shutil.which", side_effect=lambda binary: f"/usr/bin/{binary}"), \
                 mock.patch("subprocess.run", side_effect=fake_run) as run:
                first = pg_backup("postgresql://user:secret@localhost:5432/example", tmp, job_id=job_id)

            def verify_only(cmd, *args, **kwargs):
                if "pg_dump" in cmd[0]:
                    raise AssertionError("verified backup should not be regenerated")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            with mock.patch("shutil.which", side_effect=lambda binary: f"/usr/bin/{binary}"), \
                 mock.patch("subprocess.run", side_effect=verify_only) as verify:
                second = pg_backup("postgresql://user:secret@localhost:5432/example", tmp, job_id=job_id)
            self.assertEqual(first, second)
            self.assertEqual(run.call_count, 2)
            self.assertEqual(verify.call_count, 1)

    def test_common_pg_backup_writes_sha256_sidecar(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            def fake_run(cmd, *args, **kwargs):
                if "pg_dump" in cmd[0]:
                    Path(cmd[cmd.index("--file") + 1]).write_bytes(b"SAFE_DUMP")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            with mock.patch("shutil.which", side_effect=lambda binary: f"/usr/bin/{binary}"), \
                 mock.patch("subprocess.run", side_effect=fake_run):
                path = pg_backup("postgresql://user:secret@localhost:5432/example", tmp)
            sidecar = Path(f"{path}.sha256")
            self.assertTrue(sidecar.is_file())
            self.assertIn(path.name, sidecar.read_text(encoding="ascii"))

    def test_common_pg_backup_does_not_publish_an_invalid_archive(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            def reject_archive(cmd, *args, **kwargs):
                if "pg_dump" in cmd[0]:
                    Path(cmd[cmd.index("--file") + 1]).write_bytes(b"INVALID_DUMP")
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"invalid archive")

            with mock.patch("shutil.which", side_effect=lambda binary: f"/usr/bin/{binary}"), \
                 mock.patch("subprocess.run", side_effect=reject_archive):
                with self.assertRaisesRegex(RuntimeError, "archive verification failed"):
                    pg_backup("postgresql://user:secret@localhost:5432/example", tmp,
                              job_id="c" * 32)

            self.assertEqual(list(Path(tmp).glob("adulthub-*.dump.gz")), [])
            self.assertEqual(list(Path(tmp).glob("adulthub-*.dump.gz.sha256")), [])
            self.assertEqual(list(Path(tmp).glob(".*")), [])

    def test_job_scoped_retry_rebuilds_archive_with_valid_checksum_but_invalid_structure(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            job_id = "d" * 32
            destination = Path(tmp) / f"adulthub-job-{job_id}.dump.gz"
            with gzip.open(destination, "wb") as compressed:
                compressed.write(b"NOT_A_POSTGRES_ARCHIVE")
            import hashlib
            Path(f"{destination}.sha256").write_text(
                f"{hashlib.sha256(destination.read_bytes()).hexdigest()}  {destination.name}\n",
                encoding="ascii",
            )
            restore_calls = []

            def fake_run(cmd, *args, **kwargs):
                if "pg_restore" in cmd[0]:
                    restore_calls.append(cmd)
                    return subprocess.CompletedProcess(
                        cmd, 1 if len(restore_calls) == 1 else 0, stdout=b"", stderr=b"invalid archive")
                Path(cmd[cmd.index("--file") + 1]).write_bytes(b"VALID_ARCHIVE")
                return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

            with mock.patch("shutil.which", side_effect=lambda binary: f"/usr/bin/{binary}"), \
                 mock.patch("subprocess.run", side_effect=fake_run):
                result = pg_backup("postgresql://user:secret@localhost:5432/example", tmp, job_id=job_id)

            self.assertEqual(result, destination)
            self.assertEqual(len(restore_calls), 2)
            with gzip.open(destination, "rb") as compressed:
                self.assertEqual(compressed.read(), b"VALID_ARCHIVE")
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            self.assertEqual(Path(f"{destination}.sha256").read_text().split()[0], digest)

    def test_backup_success_writes_verified_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            dest_dir = Path(tmp) / "backups"
            argv = [
                "backup_db.py",
                "--db-url", "postgresql://user:pass@127.0.0.1:5432/adulthub",
                "--dest-dir", str(dest_dir),
                "--keep", "7",
            ]

            def fake_run(cmd, *args, **kwargs):
                if "pg_dump" in cmd[0]:
                    Path(cmd[cmd.index("--file") + 1]).write_bytes(b"DUMP_BYTES")
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "pg_restore" in cmd[0]:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"TABLE DATA 1\nTABLE DATA 2\n", stderr=b"")
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"unknown cmd")

            with mock.patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"), \
                 mock.patch("subprocess.run", side_effect=fake_run) as run, \
                 mock.patch.object(sys, "argv", argv):
                rc = backup_db.main()

            self.assertEqual(rc, 0)
            snaps = sorted(dest_dir.glob("adulthub-*.dump.gz"))
            self.assertEqual(len(snaps), 1)
            with gzip.open(snaps[0], "rb") as fh:
                self.assertEqual(fh.read(), b"DUMP_BYTES")
            self.assertEqual(Path(f"{snaps[0]}.sha256").read_text().split()[1], snaps[0].name)
            dump_call = next(call.args[0] for call in run.call_args_list if "pg_dump" in call.args[0][0])
            self.assertNotEqual(dump_call[dump_call.index("--file") + 1], "-")
            self.assertIn("--file", dump_call)
            self.assertEqual(run.call_args_list[0].kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(list(dest_dir.glob(".*.dump")), [])
            self.assertEqual(list(dest_dir.glob(".*.tmp")), [])

    def test_backup_dump_failure_leaves_no_partial_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            dest_dir = Path(tmp) / "backups"
            argv = ["backup_db.py", "--db-url", "postgresql://u:p@localhost/db",
                    "--dest-dir", str(dest_dir)]

            def fail_dump(cmd, *args, **kwargs):
                Path(cmd[cmd.index("--file") + 1]).write_bytes(b"partial")
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"dump failed")

            with mock.patch("shutil.which", side_effect=lambda binary: f"/usr/bin/{binary}"), \
                 mock.patch("subprocess.run", side_effect=fail_dump), \
                 mock.patch.object(sys, "argv", argv):
                self.assertEqual(backup_db.main(), 1)

            self.assertEqual(list(dest_dir.glob("adulthub-*.dump.gz")), [])
            self.assertEqual(list(dest_dir.glob(".*")), [])

    def test_backup_archive_verification_failure_leaves_no_partial_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            dest_dir = Path(tmp) / "backups"
            argv = ["backup_db.py", "--db-url", "postgresql://u:p@localhost/db",
                    "--dest-dir", str(dest_dir)]

            def reject_archive(cmd, *args, **kwargs):
                if "pg_dump" in cmd[0]:
                    Path(cmd[cmd.index("--file") + 1]).write_bytes(b"not a valid dump")
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"invalid archive")

            with mock.patch("shutil.which", side_effect=lambda binary: f"/usr/bin/{binary}"), \
                 mock.patch("subprocess.run", side_effect=reject_archive), \
                 mock.patch.object(sys, "argv", argv):
                self.assertEqual(backup_db.main(), 1)

            self.assertEqual(list(dest_dir.glob("adulthub-*.dump.gz")), [])
            self.assertEqual(list(dest_dir.glob("adulthub-*.dump.gz.sha256")), [])
            self.assertEqual(list(dest_dir.glob(".*")), [])

    def test_missing_source_returns_1(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            dest_dir = Path(tmp) / "backups"
            argv = [
                "backup_db.py",
                "--db-url", "",
                "--dest-dir", str(dest_dir),
                "--keep", "7",
            ]
            with mock.patch.dict(os.environ, {"DATABASE_URL": ""}, clear=True), \
                 mock.patch.object(sys, "argv", argv):
                rc = backup_db.main()
            self.assertEqual(rc, 1)

    def test_missing_binaries_returns_1(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            dest_dir = Path(tmp) / "backups"
            argv = [
                "backup_db.py",
                "--db-url", "postgresql://user:pass@127.0.0.1:5432/adulthub",
                "--dest-dir", str(dest_dir),
                "--keep", "7",
            ]
            with mock.patch("shutil.which", return_value=None), \
                 mock.patch.object(sys, "argv", argv):
                rc = backup_db.main()
            self.assertEqual(rc, 1)

    def test_prune_removes_old_snapshots_keeps_fresh(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_backup_") as tmp:
            dest_dir = Path(tmp) / "backups"
            dest_dir.mkdir(parents=True)
            old1 = dest_dir / "adulthub-20000101-0000.dump.gz"
            old2 = dest_dir / "adulthub-20000102-0000.dump.gz"
            fresh = dest_dir / "adulthub-20990101-0000.dump.gz"
            for p in (old1, old2, fresh):
                p.write_bytes(b"dummy")
                Path(f"{p}.sha256").write_text(f"{'0' * 64}  {p.name}\n")
            old_mtime = time.time() - 10 * 86400
            os.utime(old1, (old_mtime, old_mtime))
            os.utime(old2, (old_mtime, old_mtime))

            argv = [
                "backup_db.py",
                "--db-url", "postgresql://user:pass@127.0.0.1:5432/adulthub",
                "--dest-dir", str(dest_dir),
                "--keep", "7",
            ]

            def fake_run(cmd, *args, **kwargs):
                if "pg_dump" in cmd[0]:
                    Path(cmd[cmd.index("--file") + 1]).write_bytes(b"DUMP_BYTES")
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
                if "pg_restore" in cmd[0]:
                    return subprocess.CompletedProcess(cmd, 0, stdout=b"TABLE DATA 1\n", stderr=b"")
                return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"unknown cmd")

            with mock.patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"), \
                 mock.patch("subprocess.run", side_effect=fake_run), \
                 mock.patch.object(sys, "argv", argv):
                rc = backup_db.main()

            self.assertEqual(rc, 0)
            self.assertFalse(old1.exists(), "old snapshot should be pruned")
            self.assertFalse(old2.exists(), "old snapshot should be pruned")
            self.assertFalse(Path(f"{old1}.sha256").exists(), "old snapshot checksum should be pruned too")
            self.assertTrue(fresh.exists(), "fresh snapshot should stay")
            remaining = sorted(dest_dir.glob("adulthub-*.dump.gz"))
            self.assertEqual(len(remaining), 2)
            self.assertIn(fresh, remaining)


if __name__ == "__main__":
    unittest.main()
