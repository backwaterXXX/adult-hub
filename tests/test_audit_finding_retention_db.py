"""Retention of audit findings against a real catalog."""

from __future__ import annotations

import json
import unittest

from common.audit_finding_retention import prune_superseded_findings
from pg_helper import PgTestCase


class AuditFindingRetentionDatabaseTests(PgTestCase):
    def _audit_job(self, job_id: str, batch_id: str, *, action: str = "consistency_audit",
                   state: str = "done", created_at: int = 1) -> None:
        result = json.dumps({"audit_batch_id": batch_id, "audit_mode": "full"})
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at)"
                " VALUES (%s,%s,%s,'{}',%s,%s,%s)",
                (job_id, action, state, result, created_at, created_at),
            )

    def _finding(self, job_id: str, video_id: str, kind: str = "missing_metadata") -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details,checked_at)"
                " VALUES (%s,%s,%s,'open','evidence',%s,to_timestamp(1))",
                (job_id, video_id, kind, json.dumps({"fields": ["plot"]})),
            )

    def _open_findings(self, job_id: str) -> int:
        return self.conn.execute(
            "SELECT count(*) FROM admin_audit_items WHERE job_id=%s", (job_id,),
        ).fetchone()[0]

    def _ten_passes(self) -> list[str]:
        job_ids = []
        for index in range(1, 11):
            consistency, metadata = f"consistency-{index}", f"metadata-{index}"
            self._audit_job(consistency, f"batch-{index}", created_at=index)
            self._audit_job(metadata, f"batch-{index}", action="metadata_quality_audit",
                            created_at=index)
            self._finding(consistency, f"telegram:{index}", kind="missing_stream")
            self._finding(metadata, f"telegram:{index}")
            job_ids += [consistency, metadata]
        return job_ids

    def test_only_the_quality_history_window_of_findings_survives(self):
        self._ten_passes()

        pruned = prune_superseded_findings(self.conn)

        self.assertEqual(pruned, 4)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM admin_audit_items").fetchone()[0],
                         16)
        for index in (1, 2):
            self.assertEqual(self._open_findings(f"consistency-{index}"), 0)
            self.assertEqual(self._open_findings(f"metadata-{index}"), 0)
        for index in range(3, 11):
            self.assertEqual(self._open_findings(f"consistency-{index}"), 1)
            self.assertEqual(self._open_findings(f"metadata-{index}"), 1)

    def test_a_running_audit_keeps_its_partial_findings(self):
        self._ten_passes()
        self._audit_job("consistency-running", "batch-running", state="processing", created_at=99)
        self._finding("consistency-running", "telegram:running", kind="missing_stream")

        prune_superseded_findings(self.conn)

        self.assertEqual(self._open_findings("consistency-running"), 1)

    def test_an_audit_without_a_batch_id_keeps_a_recent_window(self):
        self._ten_passes()
        self._audit_job("legacy-audit", "", created_at=2)
        self._finding("legacy-audit", "telegram:legacy", kind="missing_stream")
        with self.conn:
            self.conn.execute(
                "UPDATE video_duplicate_jobs SET created_at=CURRENT_TIMESTAMP - INTERVAL '30 days'"
                " WHERE id='legacy-audit'"
            )

        prune_superseded_findings(self.conn)

        self.assertEqual(self._open_findings("legacy-audit"), 0)

    def test_a_short_history_is_left_alone(self):
        self._audit_job("consistency-only", "batch-only", created_at=1)
        self._finding("consistency-only", "telegram:1", kind="missing_stream")

        self.assertEqual(prune_superseded_findings(self.conn), 0)
        self.assertEqual(self._open_findings("consistency-only"), 1)


if __name__ == "__main__":
    unittest.main()
