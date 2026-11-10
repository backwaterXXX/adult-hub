"""Durable retry windows for external metadata work."""

import unittest

from pg_helper import PgTestCase

from common.external_metadata import (
    ERROR_RETRY_BASE_SECONDS,
    FOUND_REFRESH_SECONDS,
    NO_MATCH_REFRESH_SECONDS,
    record_attempt,
)


class TestExternalMetadataAttempts(PgTestCase):
    def test_outcomes_set_bounded_retry_windows_without_storing_error_text(self):
        now = 1_800_000_000
        record_attempt(self.conn, "performer", "alice", "tpdb", "no_match", now=now)
        row = self.conn.execute(
            "SELECT outcome,attempt_count,EXTRACT(EPOCH FROM next_attempt_at),last_error_code "
            "FROM external_metadata_attempts WHERE entity_key='alice'"
        ).fetchone()
        self.assertEqual(row[:2], ("no_match", 1))
        self.assertEqual(float(row[2]), now + NO_MATCH_REFRESH_SECONDS)
        self.assertEqual(row[3], "")

        record_attempt(self.conn, "performer", "alice", "tpdb", "error",
                       error=TimeoutError("secret response token must not be stored"), now=now + 10)
        row = self.conn.execute(
            "SELECT outcome,attempt_count,EXTRACT(EPOCH FROM next_attempt_at),last_error_code "
            "FROM external_metadata_attempts WHERE entity_key='alice'"
        ).fetchone()
        self.assertEqual(row[:2], ("error", 2))
        self.assertEqual(float(row[2]), now + 10 + ERROR_RETRY_BASE_SECONDS)
        self.assertEqual(row[3], "TimeoutError")
        record_attempt(self.conn, "performer", "alice", "tpdb", "error",
                       error=TimeoutError("sensitive detail"), now=now + 20)
        row = self.conn.execute(
            "SELECT attempt_count,EXTRACT(EPOCH FROM next_attempt_at) FROM external_metadata_attempts "
            "WHERE entity_key='alice'"
        ).fetchone()
        self.assertEqual(row[0], 3)
        self.assertEqual(float(row[1]), now + 20 + ERROR_RETRY_BASE_SECONDS * 2)

        record_attempt(self.conn, "video", "telegram:1:2", "studio_scene", "found", now=now)
        row = self.conn.execute(
            "SELECT EXTRACT(EPOCH FROM next_attempt_at) FROM external_metadata_attempts "
            "WHERE entity_type='video'"
        ).fetchone()
        self.assertEqual(float(row[0]), now + FOUND_REFRESH_SECONDS)


if __name__ == "__main__":
    unittest.main()
