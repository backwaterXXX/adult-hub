"""Pooled catalog sessions reuse a connection and never leak session state."""

from common import db
from tests.pg_helper import PgTestCase


class TestPooledConnection(PgTestCase):
    def tearDown(self):
        db.close_pools()
        super().tearDown()

    def test_close_returns_the_session_to_the_pool(self):
        first = db.pooled_connect(self.db_url)
        raw = first._raw
        first.close()

        second = db.pooled_connect(self.db_url)
        self.assertIs(second._raw, raw)
        second.close()

    def test_release_rolls_back_an_open_transaction(self):
        conn = db.pooled_connect(self.db_url)
        conn.execute("CREATE TEMP TABLE pool_probe (value integer)")
        conn.close()

        conn = db.pooled_connect(self.db_url)
        try:
            self.assertIsNone(
                conn.execute("SELECT to_regclass('pg_temp.pool_probe')").fetchone()[0]
            )
        finally:
            conn.close()

    def test_a_closed_session_is_discarded(self):
        conn = db.pooled_connect(self.db_url)
        broken = conn._raw
        broken.close()
        conn.close()

        conn = db.pooled_connect(self.db_url)
        try:
            self.assertIsNot(conn._raw, broken)
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        finally:
            conn.close()

    def test_a_double_close_releases_only_once(self):
        conn = db.pooled_connect(self.db_url)
        raw = conn._raw
        conn.close()
        conn.close()

        first = db.pooled_connect(self.db_url)
        second = db.pooled_connect(self.db_url)
        try:
            self.assertIsNot(first._raw, second._raw)
            self.assertIs(first._raw, raw)
        finally:
            first.close()
            second.close()

    def test_the_row_factory_does_not_leak(self):
        conn = db.pooled_connect(self.db_url)
        conn.row_factory = db.Record
        conn.close()

        conn = db.pooled_connect(self.db_url)
        try:
            self.assertIsNone(conn.row_factory)
        finally:
            conn.close()
