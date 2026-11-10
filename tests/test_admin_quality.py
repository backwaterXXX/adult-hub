"""PostgreSQL tests for the Admin v2 catalog quality projection."""

from __future__ import annotations

import time
import unittest

from psycopg.types.json import Jsonb
from tests.pg_helper import PgTestCase


from common.admin_quality import read_quality_summary
from common.admin_quality import _complete_audit
from common.job_queue import enqueue_job


class TestAdminQuality(PgTestCase):
    def add_audit(self, *, checked=1, total=1, inventory="complete", state="done", created=None,
                  record_total=True, action="consistency_audit", mode=None, batch_id=None):
        payload = {"mode": mode} if mode else {}
        if batch_id:
            payload["audit_batch_id"] = batch_id
        job_id = enqueue_job(self.db_url, action, payload)
        now = int(created if created is not None else time.time())
        result = {"checked": checked, "library_inventory_status": inventory,
                  "jellyfin_import_state": "unknown"}
        if record_total:
            result["total"] = total
        if mode:
            result["audit_mode"] = mode
        if action == "metadata_quality_audit":
            result["audit_mode"] = "metadata_quality"
        if batch_id:
            result["audit_batch_id"] = batch_id
        with self.conn:
            self.conn.execute("UPDATE video_duplicate_jobs SET state=%s,result=%s,created_at=%s WHERE id=%s",
                              (state, Jsonb(result), now, job_id))
        return job_id

    def test_metadata_only_audit_is_complete_and_file_only_is_not_quality_baseline(self):
        metadata = self.add_audit(action="metadata_quality_audit", mode="metadata_quality", inventory=None)
        self.add_item(metadata, "telegram:quality:metadata-only", "missing_metadata", {"fields": ["plot"]})
        summary = read_quality_summary(self.db_url)
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["latest_audit"]["mode"], "metadata_quality")
        self.assertEqual(summary["counts"]["missing_metadata"], 1)

        file_only = self.add_audit(created=200, mode="file_consistency")
        self.assertEqual(_complete_audit(self.conn)[0][0], metadata)
        with self.conn:
            self.conn.execute("DELETE FROM video_duplicate_jobs WHERE id=%s", (file_only,))

    def test_full_audit_pairs_distinct_jobs_for_summary_history_and_findings(self):
        batch_id = "a" * 32
        metadata = self.add_audit(action="metadata_quality_audit", mode="metadata_quality",
                                  inventory=None, created=100, batch_id=batch_id)
        files = self.add_audit(mode="file_consistency", created=101, batch_id=batch_id)
        self.add_item(metadata, "telegram:quality:full-metadata", "missing_metadata", {"fields": ["plot"]})
        self.add_item(files, "telegram:quality:full-file", "nfo_metadata_mismatch", {
            "diffs": [{"field": "Title", "current": "Old", "expected": "New"}],
        })

        summary = read_quality_summary(self.db_url)
        self.assertEqual(summary["latest_audit"]["mode"], "full")
        self.assertEqual(set(summary["latest_audit"]["job_ids"]), {metadata, files})
        self.assertEqual(summary["counts"]["missing_metadata"], 1)
        self.assertEqual(summary["counts"]["nfo_metadata_mismatch"], 1)

    def test_full_audit_is_in_progress_until_both_child_jobs_finish(self):
        baseline = self.add_audit(created=50)
        batch_id = "b" * 32
        metadata = self.add_audit(action="metadata_quality_audit", mode="metadata_quality",
                                  inventory=None, created=100, batch_id=batch_id)
        self.add_audit(mode="file_consistency", state="processing", created=101, batch_id=batch_id)
        summary = read_quality_summary(self.db_url)
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["latest_audit"]["id"], baseline)
        self.assertEqual(summary["current_audit_id"], metadata)
        self.assertEqual(summary["current_audit_state"], "processing")

    def add_item(self, job_id, video_id, kind, details=None):
        with self.conn:
            self.conn.execute("INSERT INTO videos (id,provider,title,performers,categories,tags) "
                              "VALUES (%s,'telegram',%s,'[]','[]','[]') ON CONFLICT(id) DO NOTHING",
                              (video_id, "Quality title"))
            return int(self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details) "
                "VALUES (%s,%s,%s,'open','Quality evidence',%s) RETURNING id",
                (job_id, video_id, kind, Jsonb(details or {})),
            ).fetchone()[0])

    def test_no_complete_audit_is_not_reported_as_empty_success(self):
        self.add_audit(checked=3, total=4)
        summary = read_quality_summary(self.db_url)
        self.assertEqual(summary["status"], "no_complete_audit")
        self.assertIsNone(summary["latest_audit"])
        self.assertEqual(summary["total"], 0)

    def test_partial_filesystem_does_not_hide_an_incomplete_catalog_pass(self):
        self.add_audit(checked=3, total=4, inventory="partial")
        summary = read_quality_summary(self.db_url)
        self.assertEqual(summary["status"], "no_complete_audit")
        self.assertEqual(summary["status_reason"], "audit_not_complete")
        self.assertIsNone(summary["latest_audit"])

    def test_legacy_complete_inventory_without_total_explains_missing_baseline(self):
        self.add_audit(checked=3, record_total=False)
        summary = read_quality_summary(self.db_url)
        self.assertEqual(summary["status"], "no_complete_audit")
        self.assertEqual(summary["status_reason"], "legacy_missing_catalog_total")
        self.assertIsNone(summary["latest_audit"])
        self.assertEqual(summary["counts"], {})

    def test_latest_complete_audit_and_counts_ignore_incomplete_newer_run(self):
        older = self.add_audit(created=50)
        self.add_item(older, "telegram:quality:old-1", "missing_nfo")
        self.add_item(older, "telegram:quality:old-2", "missing_nfo")
        self.add_item(older, "telegram:quality:old-3", "missing_metadata", {"fields": ["plot"]})
        complete = self.add_audit(created=100)
        self.add_item(complete, "telegram:quality:1", "nfo_metadata_mismatch", {
            "diffs": [{"field": "Title", "current": "Old", "expected": "New"}],
        })
        self.add_item(complete, "telegram:quality:2", "missing_metadata", {"fields": ["performers"]})
        self.add_item(complete, "telegram:quality:3", "missing_metadata", {"fields": ["plot", "studio"]})
        self.add_audit(checked=2, total=3, inventory="partial", created=200)
        summary = read_quality_summary(self.db_url)
        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["latest_audit"]["id"], complete)
        self.assertEqual(summary["latest_audit"]["jellyfin_import_state"], "unknown")
        self.assertEqual(summary["counts"]["nfo_metadata_mismatch"], 1)
        self.assertEqual(summary["counts"]["missing_metadata"], 2)
        self.assertEqual(summary["missing_fields"]["performers"], 1)
        self.assertEqual(summary["missing_fields"]["plot"], 1)
        self.assertEqual(summary["missing_fields"]["studio"], 1)
        self.assertEqual(summary["total"], 3)
        self.assertEqual([audit["id"] for audit in summary["history"]], [complete, older])
        self.assertEqual([audit["open_total"] for audit in summary["history"]], [3, 3])
        self.assertEqual(summary["history"][1]["counts"]["missing_nfo"], 2)

    def test_partial_filesystem_inventory_keeps_complete_catalog_quality_findings_visible(self):
        audit = self.add_audit(checked=2, total=2, inventory="partial")
        self.add_item(audit, "telegram:quality:partial-inventory", "nfo_metadata_mismatch", {
            "diffs": [{"field": "Plot", "current": "Old description", "expected": "Current description"}],
        })

        summary = read_quality_summary(self.db_url)

        self.assertEqual(summary["status"], "ready")
        self.assertEqual(summary["latest_audit"]["checked"], 2)
        self.assertEqual(summary["latest_audit"]["total"], 2)
        self.assertEqual(summary["latest_audit"]["library_inventory_status"], "partial")
        self.assertEqual(summary["history"][0]["library_inventory_status"], "partial")
        self.assertEqual(summary["counts"]["nfo_metadata_mismatch"], 1)


if __name__ == "__main__":
    unittest.main()
