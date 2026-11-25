"""Parked artwork: videos whose frame extraction exhausted the retry budget."""

from __future__ import annotations

import unittest

from common.artwork_parking import (
    ARTWORK_FRAME_FAILURE_CODE,
    ARTWORK_FRAME_FAILURE_LIMIT,
    parked_artwork_ids,
)


class _Connection:
    def __init__(self, rows, error=None):
        self.rows = rows
        self.error = error
        self.sql = ""
        self.params = ()

    def execute(self, sql, params=()):
        self.sql = sql
        self.params = params
        if self.error:
            raise self.error
        return self

    def fetchall(self):
        return self.rows


class ParkedArtworkTests(unittest.TestCase):
    def test_ids_come_from_confirmed_frame_failures_only(self):
        conn = _Connection([("telegram:a",), ("telegram:b",), (None,)])

        parked = parked_artwork_ids(conn)

        self.assertEqual(parked, {"telegram:a", "telegram:b"})
        self.assertIn("action = 'artwork_generate'", conn.sql)
        self.assertIn("state = 'failed'", conn.sql)
        self.assertIn("result LIKE %s", conn.sql)
        self.assertEqual(
            list(conn.params),
            [f'%"{ARTWORK_FRAME_FAILURE_CODE}"%', ARTWORK_FRAME_FAILURE_LIMIT],
        )

    def test_missing_queue_table_parking_degrades_to_nothing(self):
        conn = _Connection([], error=RuntimeError("relation does not exist"))

        self.assertEqual(parked_artwork_ids(conn), set())
