"""Test database fixture lifecycle contracts."""

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

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
sys.path.insert(0, {root!r})
sys.path.insert(0, {tests!r})
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
        code = CHILD_CODE.format(root=str(ROOT), tests=str(ROOT / "tests"))
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT), env={},
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
