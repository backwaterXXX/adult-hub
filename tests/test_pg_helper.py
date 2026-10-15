"""Test database fixture lifecycle contracts."""

import unittest
from unittest.mock import patch

from tests.pg_helper import PgTestCase


class TestPgTestCaseSetup(unittest.TestCase):
    def test_class_setup_closes_schema_initialization_connection(self):
        class DummyPgTestCase(PgTestCase):
            pass

        class Connection:
            closed = False

            def close(self):
                self.closed = True

        connection = Connection()
        with patch("tests.pg_helper.require_url", return_value="postgresql://test"), \
             patch("tests.pg_helper.init_test_db", return_value=connection) as initialize:
            DummyPgTestCase.setUpClass()

        initialize.assert_called_once_with("postgresql://test")
        self.assertTrue(connection.closed)
