"""Test database fixture lifecycle contracts."""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

from pg_helper import PgTestCase, TEST_URL
from tests.pg_helper import PgTestCase as PackagedPgTestCase


class TestPgTestCaseSetup(unittest.TestCase):
    def test_class_setup_closes_schema_initialization_connection(self):
        class DummyPgTestCase(PackagedPgTestCase):
            pass

        class Connection:
            closed = False

            def close(self):
                self.closed = True

        connection = Connection()
        with patch("tests.pg_helper.require_url", return_value="postgresql://test"), \
             patch("tests.pg_helper._acquire_run_guard"), \
             patch("tests.pg_helper.init_test_db", return_value=connection) as initialize:
            DummyPgTestCase.setUpClass()

        initialize.assert_called_once_with("postgresql://test")
        self.assertTrue(connection.closed)


CHILD_CODE = """
import sys
from pg_helper import _acquire_run_guard, TEST_URL
try:
    _acquire_run_guard(TEST_URL)
except RuntimeError as exc:
    print("GUARDED:", exc)
    sys.exit(0)
print("UNGUARDED")
sys.exit(1)
"""


class TestRunGuard(PgTestCase):
    def test_second_process_is_rejected(self):
        # This process holds the run lock via setUpClass; a child attempting
        # the same database must fail fast with the guard message.
        code = CHILD_CODE
        # Deliberately minimal env: no runtime config from the parent, but
        # CI provides TEST_DATABASE_URL only through the environment, so
        # forward it (local runs fall back to .env) plus PATH for the binary.
        child_env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "tests")}
        if TEST_URL:
            child_env["TEST_DATABASE_URL"] = TEST_URL
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT), env=child_env,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertIn("GUARDED:", result.stdout)
        self.assertIn("Another test process", result.stdout)

    def test_reacquire_in_same_process_is_noop(self):
        from pg_helper import _acquire_run_guard
        _acquire_run_guard(TEST_URL)

    def test_second_module_copy_shares_the_guard(self):
        # Suites import this file both as `pg_helper` and `tests.pg_helper`;
        # both copies must recognize the already-held lock in one process.
        import pg_helper
        import tests.pg_helper as packaged
        self.assertIsNot(pg_helper, packaged)
        packaged._acquire_run_guard(TEST_URL)


if __name__ == "__main__":
    unittest.main()
