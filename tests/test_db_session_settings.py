"""Session settings every application connection must carry."""

from common import db
from tests.pg_helper import PgTestCase


class TestConnectionSessionSettings(PgTestCase):
    def test_new_connection_disables_jit_and_pins_utc(self):
        conn = db.connect(self.db_url)
        try:
            self.assertEqual(conn.execute("SHOW jit").fetchone()[0], "off")
            self.assertEqual(conn.execute("SHOW timezone").fetchone()[0], "UTC")
        finally:
            conn.close()

    def test_reopened_session_keeps_the_settings(self):
        conn = db.connect(self.db_url)
        try:
            conn._raw.close()
            self.assertEqual(conn.execute("SHOW jit").fetchone()[0], "off")
        finally:
            conn.close()
