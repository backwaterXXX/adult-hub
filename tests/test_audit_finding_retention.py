"""Retention of superseded audit finding snapshots."""

from __future__ import annotations

import unittest

from common.admin_quality import QUALITY_HISTORY_LIMIT
from common.audit_finding_retention import (
    KEEP_COMPLETE_AUDITS,
    prune_superseded_findings,
)

AUDIT_ACTIONS = ("consistency_audit", "metadata_quality_audit")


class FakeCursor:
    def __init__(self, rows=(), rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    """Answers the retention queries and records the pruning statement."""

    def __init__(self, audits, deleted=0):
        self.audits = audits
        self.deleted = deleted
        self.calls = []

    def execute(self, sql, params=None):
        statement = " ".join(sql.split())
        self.calls.append((statement, params))
        if statement.startswith("SELECT COALESCE(result::jsonb->>'audit_batch_id', ''), max(created_at)"):
            batches = {}
            for audit in self.audits:
                batch = audit["batch_id"]
                if batch:
                    batches[batch] = max(batches.get(batch, 0), audit["created_at"])
            kept = sorted(batches, key=lambda batch: (-batches[batch], batch))[:params[1]]
            return FakeCursor([(batch, batches[batch]) for batch in kept])
        if "COALESCE(result::jsonb->>'audit_batch_id', '') = ANY" in statement:
            batches = params[1]
            return FakeCursor([(audit["id"],) for audit in self.audits
                               if audit["batch_id"] in batches])
        if "make_interval(days =>" in statement:
            days = params[1]
            return FakeCursor([(audit["id"],) for audit in self.audits
                               if not audit["batch_id"] and audit["created_at"] >= days])
        if statement.startswith("DELETE FROM admin_audit_items"):
            return FakeCursor([], rowcount=self.deleted)
        raise AssertionError(f"unexpected statement: {statement}")


def audit(job_id, batch_id, created_at=0):
    return {"id": job_id, "batch_id": batch_id, "created_at": created_at}


class PruneSupersededFindingsTests(unittest.TestCase):
    def test_the_window_matches_the_quality_centre_history(self):
        self.assertEqual(KEEP_COMPLETE_AUDITS, QUALITY_HISTORY_LIMIT)

    def test_only_the_newest_batches_survive(self):
        audits = []
        for index in range(1, 11):
            batch = f"batch-{index}"
            audits.append(audit(f"consistency-{index}", batch, created_at=index))
            audits.append(audit(f"metadata-{index}", batch, created_at=index))

        conn = FakeConnection(audits, deleted=42)
        pruned = prune_superseded_findings(conn)

        self.assertEqual(pruned, 42)
        statement, params = conn.calls[-1]
        self.assertTrue(statement.startswith("DELETE FROM admin_audit_items"))
        kept = set(params[0])
        for index in range(3, 11):
            self.assertIn(f"consistency-{index}", kept)
            self.assertIn(f"metadata-{index}", kept)
        for index in (1, 2):
            self.assertNotIn(f"consistency-{index}", kept)
            self.assertNotIn(f"metadata-{index}", kept)

    def test_a_queued_or_running_audit_is_never_pruned(self):
        conn = FakeConnection([audit("consistency-1", "batch-1", created_at=1)], deleted=0)

        prune_superseded_findings(conn)

        statement, _params = conn.calls[-1]
        self.assertIn("j.state IN ('queued','processing')", statement)

    def test_audits_without_a_batch_id_keep_a_recent_window(self):
        audits = [
            audit(f"legacy-{index}", "", created_at=-index) for index in range(1, 4)
        ] + [audit("consistency-new", "batch-new", created_at=100)]

        conn = FakeConnection(audits)
        prune_superseded_findings(conn)

        kept = set(conn.calls[-1][1][0])
        self.assertIn("consistency-new", kept)
        self.assertNotIn("legacy-1", kept, "an old audit without a batch id is superseded")

    def test_a_non_positive_window_is_rejected(self):
        for keep in (0, -1, 2.5, "8", None):
            with self.assertRaises(ValueError):
                prune_superseded_findings(FakeConnection([]), keep=keep)

    def test_result_counts_survive_a_missing_rowcount(self):
        class NoRowcount(FakeConnection):
            def execute(self, sql, params=None):
                cursor = super().execute(sql, params)
                del cursor.rowcount
                return cursor

        self.assertEqual(prune_superseded_findings(NoRowcount([])), 0)


class BatchIdSelectionTests(unittest.TestCase):
    def test_batches_are_ranked_by_their_newest_job(self):
        audits = [
            audit("consistency-old", "batch-old", created_at=5),
            audit("metadata-old", "batch-old", created_at=5),
            audit("consistency-new", "batch-new", created_at=9),
            audit("metadata-new", "batch-new", created_at=9),
        ]
        conn = FakeConnection(audits)
        prune_superseded_findings(conn, keep=1)

        kept = conn.calls[-1][1][0]
        self.assertEqual(kept, ["consistency-new", "metadata-new"])


if __name__ == "__main__":
    unittest.main()
