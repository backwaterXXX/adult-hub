"""A connection whose server session was dropped (for example by a database restart)
opens a fresh session on the next use instead of failing every later statement."""

from __future__ import annotations

import psycopg

from common import db
from tests.pg_helper import PgTestCase


class TestConnectionReconnect(PgTestCase):
    def test_statement_after_dropped_session_reopens_the_connection(self):
        conn = db.connect(self.db_url)
        try:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
            conn._raw.close()  # simulate the server dropping the session

            self.assertEqual(conn.execute("SELECT 2").fetchone()[0], 2)
            self.assertFalse(conn._raw.closed)
        finally:
            conn.close()

    def test_commit_and_context_exit_reopen_a_dropped_session(self):
        conn = db.connect(self.db_url)
        try:
            conn._raw.close()

            with conn:
                conn.execute("SELECT 3")

            self.assertFalse(conn._raw.closed)
        finally:
            conn.close()

    def test_idle_connection_recovers_when_the_server_ends_its_session(self):
        conn = db.connect(self.db_url)
        try:
            pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            conn.commit()  # end the read transaction: the connection is idle again
            admin = db.connect(self.db_url)
            try:
                admin.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()
            finally:
                admin.close()

            # The first statement after the server ended the session must still succeed.
            self.assertEqual(conn.execute("SELECT 5").fetchone()[0], 5)
            self.assertNotEqual(conn.execute("SELECT pg_backend_pid()").fetchone()[0], pid)
        finally:
            conn.close()

    def test_connection_inside_a_transaction_does_not_silently_retry(self):
        conn = db.connect(self.db_url)
        try:
            conn.execute("SELECT 6")  # opens a transaction (autocommit is off)
            pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
            admin = db.connect(self.db_url)
            try:
                admin.execute("SELECT pg_terminate_backend(%s)", (pid,)).fetchone()
            finally:
                admin.close()

            with self.assertRaises(psycopg.OperationalError):
                conn.execute("SELECT 7")
        finally:
            conn.close()

    def test_connection_without_url_keeps_the_original_error(self):
        conn = db.Connection(db.connect(self.db_url)._raw, url=None)
        conn._raw.close()

        with self.assertRaises(Exception):
            conn.execute("SELECT 4")
