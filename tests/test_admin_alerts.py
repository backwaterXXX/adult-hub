"""Threshold alerts for failed jobs and open metadata findings."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from common.admin_alerts import build_alerts, read_alerts
from tests.pg_helper import PgTestCase


class AdminAlertTests(PgTestCase):
    def _job(self, job_id: str, action: str, state: str, *, updated_at: int | None = None):
        stamp = int(time.time()) if updated_at is None else int(updated_at)
        self.conn.execute(
            "INSERT INTO video_duplicate_jobs (id,action,state,payload,created_at,updated_at) "
            "VALUES (%s,%s,%s,'{}',to_timestamp(%s),to_timestamp(%s))",
            (job_id, action, state, stamp, stamp),
        )

    def test_failed_job_and_metadata_thresholds_raise_alerts(self):
        now = 1_800_000_000
        self._job("failed-1", "artwork_generate", "failed", updated_at=now - 60)
        self._job("audit-1", "metadata_quality_audit", "done", updated_at=now - 60)
        for index in range(3):
            self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details) "
                "VALUES ('audit-1',%s,'missing_metadata','open','missing','{}')",
                (f"telegram:test:{index}:{index}",),
            )
        self.conn.commit()

        with patch.dict("os.environ", {
            "ADMIN_FAILED_JOB_ALERT_THRESHOLD": "0",
            "ADMIN_METADATA_BACKLOG_ALERT_THRESHOLD": "2",
        }):
            alerts = build_alerts(self.conn, now=now)

        self.assertTrue(alerts["available"])
        self.assertEqual(alerts["failed_jobs_24h"]["count"], 1)
        self.assertEqual(alerts["failed_jobs_24h"]["by_action"], {"artwork_generate": 1})
        self.assertEqual(alerts["metadata_backlog"]["count"], 3)
        self.assertEqual({item["kind"] for item in alerts["items"]},
                         {"failed_jobs", "metadata_backlog"})
        self.assertTrue(all(item["severity"] == "warning" for item in alerts["items"]))

    def test_thresholds_below_the_counts_do_not_alert_and_missing_db_is_non_fatal(self):
        with patch.dict("os.environ", {
            "ADMIN_FAILED_JOB_ALERT_THRESHOLD": "10",
            "ADMIN_METADATA_BACKLOG_ALERT_THRESHOLD": "10",
        }):
            self.assertEqual(build_alerts(self.conn)["items"], [])
        self.assertFalse(read_alerts(None)["available"])


if __name__ == "__main__":
    unittest.main()
