"""Focused service tests for shared legacy/Admin V2 proposal operations."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sync"))
sys.path.insert(0, str(ROOT / "resolver"))

from common import performer_proposals


class Cursor:
    def __init__(self, row=None, rows=None):
        self.row, self.rows = row, rows or []

    def fetchone(self):
        return self.row

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), params))
        response = self.responses.pop(0) if self.responses else None
        if isinstance(response, Cursor):
            return response
        return Cursor(row=response)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None
        return False

    def close(self):
        self.closed = True


class TestPerformerProposalService(unittest.TestCase):
    def test_list_and_search_clamp_limits_and_return_shared_contract(self):
        proposals_conn = FakeConnection([
            Cursor(row=(501,)),
            Cursor(rows=[(1, "tg:1", "A", "A", "caption", "exact", "p1", "Video")]),
        ])
        catalog_conn = FakeConnection([
            Cursor(rows=[("p1", "Performer")]),  # performer_catalog
            Cursor(rows=[]),                       # approved aliases
            Cursor(rows=[]),                       # legacy bios
            Cursor(rows=[]),                       # legacy aliases
            Cursor(rows=[("p1",)]),               # active IDs
        ])
        with patch.object(performer_proposals.db, "connect", side_effect=[proposals_conn, catalog_conn]) as connect:
            proposals = performer_proposals.list_pending_proposals("db", 900, -5)
            catalog = performer_proposals.search_catalog("db", "  performer  ", 0)
        self.assertEqual(proposals["items"][0]["title"], "Video")
        self.assertEqual(proposals, {
            "items": [{"id": 1, "video_id": "tg:1", "candidate": "A", "matched_text": "A",
                       "source_field": "caption", "reason": "exact", "performer_id": "p1", "title": "Video"}],
            "total": 501, "limit": 500, "offset": 0,
        })
        self.assertEqual(proposals_conn.calls[1][1], (500, 0))
        self.assertEqual(catalog, {"items": [{"id": "p1", "name": "Performer"}]})
        self.assertIn("FROM performer_catalog WHERE active=TRUE", catalog_conn.calls[0][0])
        self.assertEqual(len(catalog_conn.calls), 5)
        self.assertEqual(connect.call_count, 2)
        self.assertTrue(proposals_conn.closed and catalog_conn.closed)

    def test_reject_commits_pending_decision_without_queueing_refresh(self):
        conn = FakeConnection([("video-1", "Alias", "pending", False), None])
        wake = []
        with patch.object(performer_proposals.db, "connect", return_value=conn):
            result = performer_proposals.review_proposal(
                "db", 7, {"action": "reject"}, wake_worker=wake.append,
            )
        self.assertEqual(result, {"ok": True, "status": "rejected"})
        self.assertTrue(conn.committed)
        self.assertTrue(conn.closed)
        self.assertEqual(wake, [])
        self.assertIn("status='rejected'", conn.calls[-1][0])

    def test_approve_updates_alias_and_catalog_then_queues_refresh(self):
        conn = FakeConnection([
            ("video-1", "Alias", "pending", False),
            ("Canonical Name",),
            None,
            None,
            None,
            (["Alias", "Other"],),
            None,
            None,
        ])
        wake = []
        with patch.object(performer_proposals.db, "connect", return_value=conn), \
             patch("video_duplicates.enqueue_job", return_value="job-9") as enqueue:
            result = performer_proposals.review_proposal(
                "db", 8, {"action": "approve", "performer_id": "p9"}, wake_worker=wake.append,
            )
        self.assertEqual(result, {
            "ok": True, "status": "approved", "performer": "Canonical Name",
            "refresh_job_id": "job-9", "refresh_pending": True,
        })
        enqueue.assert_called_once_with("db", "performer_refresh", {"video_ids": ["video-1"]})
        self.assertEqual(wake, ["jobs"])
        updates = [params for sql, params in conn.calls if sql.startswith("UPDATE videos SET performers")]
        self.assertEqual(updates, [("[\"Canonical Name\", \"Other\"]", "video-1")])
        self.assertTrue(conn.committed and conn.closed)

    def test_manual_override_conflict_rolls_back_without_queueing(self):
        conn = FakeConnection([("video-1", "Alias", "pending", True)])
        wake = []
        with patch.object(performer_proposals.db, "connect", return_value=conn):
            with self.assertRaises(performer_proposals.ProposalError) as raised:
                performer_proposals.review_proposal(
                    "db", 9, {"action": "approve", "performer_id": "p1"}, wake_worker=wake.append,
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(conn.rolled_back and conn.closed)
        self.assertEqual(wake, [])


if __name__ == "__main__":
    unittest.main()
