"""Monthly history retention keeps what the hub still needs.

``prune_history()`` is the only code that trims administrative and job history,
and the backup scheduler runs it after a verified backup. These tests pin both
halves of its contract: expired, already-reversed history goes, while active
decisions, running jobs and anything a quarantine still points at are retained.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import PgTestCase

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.history_retention import BATCH_SIZE, prune_history

OLD = "CURRENT_TIMESTAMP - INTERVAL '40 days'"
RECENT = "CURRENT_TIMESTAMP - INTERVAL '10 days'"


class HistoryRetentionTest(PgTestCase):
    def _video(self, video_id: str) -> str:
        self.conn.execute(
            "INSERT INTO videos (id,provider,title,duration,first_seen,last_seen)"
            " VALUES (%s,'telegram',%s,600,1,1)", (video_id, video_id))
        return video_id

    def _job(self, job_id: str, state: str, *, created_at: str = OLD) -> str:
        self.conn.execute(
            "INSERT INTO video_duplicate_jobs (id,action,state,payload,created_at,updated_at)"
            f" VALUES (%s,'consistency_audit',{state!r}::text,'{{}}',{created_at},{created_at})",
            (job_id,))
        return job_id

    def _audit_item(self, job_id: str, *, created_at: str = OLD) -> None:
        self.conn.execute(
            "INSERT INTO admin_audit_items (job_id,kind,status,checked_at)"
            f" VALUES (%s,'orphan_file','open',{created_at})", (job_id,))

    def _quarantine(self, operation_id: str, audit_id: str, state: str,
                    *, created_at: str = OLD) -> None:
        self.conn.execute(
            "INSERT INTO admin_orphan_quarantine (operation_id,audit_id,state,files,created_at,updated_at)"
            f" VALUES (%s,%s,{state!r}::text,'{{}}',{created_at},{created_at})",
            (operation_id, audit_id))

    def _ids(self, table: str, column: str = "id") -> set:
        return {row[0] for row in
                self.conn.execute(f"SELECT {column} FROM {table}").fetchall()}

    def test_expired_terminal_history_is_pruned(self):
        self._video("telegram:-100:10:1")
        old_job = self._job("job-old", "done")
        self._audit_item(old_job)
        self.conn.execute(
            "INSERT INTO admin_action_events (method,route,status_code,outcome,created_at)"
            f" VALUES ('POST','/admin/api/v2/x',200,'completed',{OLD})")
        self.conn.execute(
            "INSERT INTO admin_audit_events (event_key,actor_type,event_type,object_type,object_id,changes,created_at)"
            f" VALUES ('k-old','shared_admin','metadata_update','video','telegram:-100:10:1','{{}}',{OLD})")
        self.conn.execute(
            "INSERT INTO admin_operation_plans (plan_id,operation_type,state,scope,revisions,preview,expires_at,created_at)"
            f" VALUES ('plan-old','merge_dupes','queued','{{}}','{{}}','{{}}',{OLD},{OLD})")
        self.conn.execute(
            "INSERT INTO external_metadata_attempts (entity_type,entity_key,source,outcome,"
            "last_attempt_at,next_attempt_at,updated_at)"
            f" VALUES ('performer','p1','stashdb','found',{OLD},{OLD},{OLD})")
        self.conn.execute(
            "INSERT INTO video_availability_checks (job_id,video_id,status,checked_at)"
            f" VALUES (%s,'telegram:-100:10:1','available',{OLD})", (old_job,))
        self.conn.execute(
            "INSERT INTO admin_schedules (id,name,action,schedule_type,daily_at,enabled,created_at,updated_at)"
            f" VALUES ('sch-1','audit','consistency_audit','daily','04:30',true,{OLD},{RECENT})")
        self.conn.execute("UPDATE admin_schedules SET last_job_id=%s WHERE id='sch-1'", (old_job,))
        self.conn.commit()

        counts = prune_history(self.conn)

        self.assertEqual(counts["video_duplicate_jobs"], 1)
        self.assertEqual(counts["admin_action_events"], 1)
        self.assertEqual(counts["admin_audit_events"], 1)
        self.assertEqual(counts["admin_operation_plans"], 1)
        self.assertEqual(counts["external_metadata_attempts"], 1)
        self.assertEqual(counts["video_availability_checks"], 1)
        self.assertNotIn(old_job, self._ids("video_duplicate_jobs"))
        # Audit findings ride the job's cascade, and the schedule stops pointing
        # at a job that no longer exists.
        self.assertEqual(self._ids("admin_audit_items", "job_id"), set())
        self.assertEqual(self._ids("video_availability_checks"), set())
        self.assertIsNone(self.conn.execute(
            "SELECT last_job_id FROM admin_schedules WHERE id='sch-1'").fetchone()[0])

    def test_active_and_reversible_history_is_kept(self):
        running = self._job("job-running", "processing")
        pending = self._job("job-pending", "queued")
        self.conn.execute(
            "INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot)"
            f" VALUES ('telegram:-100:10:2','telegram:-100:10:3','op-live','active',{OLD},'x')")
        self.conn.execute(
            "INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot)"
            f" VALUES ('telegram:-100:10:4','telegram:-100:10:5','op-undone','undone',{RECENT},'x')")
        self.conn.execute(
            "INSERT INTO video_duplicate_dismissals (left_id,right_id,active,operation_id,created_at)"
            f" VALUES ('telegram:-100:10:6','telegram:-100:10:7',1,'op-dismiss',{OLD})")
        self.conn.commit()

        counts = prune_history(self.conn)

        self.assertEqual(counts["video_duplicate_jobs"], 0)
        self.assertEqual(counts["video_duplicate_decisions"], 0)
        self.assertEqual(counts["video_duplicate_dismissals"], 0)
        self.assertIn(running, self._ids("video_duplicate_jobs"))
        self.assertIn(pending, self._ids("video_duplicate_jobs"))
        self.assertIn("op-live", self._ids("video_duplicate_decisions", "operation_id"))
        self.assertIn("op-undone", self._ids("video_duplicate_decisions", "operation_id"))
        self.assertIn("op-dismiss", self._ids("video_duplicate_dismissals", "operation_id"))

    def test_an_undoable_decision_is_pruned_only_once_it_is_already_undone_and_aged(self):
        self.conn.execute(
            "INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot)"
            f" VALUES ('telegram:-100:10:8','telegram:-100:10:9','op-old-undone','undone',{OLD},'x')")
        self.conn.commit()

        counts = prune_history(self.conn)

        self.assertEqual(counts["video_duplicate_decisions"], 1)
        self.assertEqual(self._ids("video_duplicate_decisions", "loser_id"), set())

    def test_a_quarantine_keeps_its_source_audit_job(self):
        job = self._job("job-audited", "done")
        self._audit_item(job)
        self._quarantine("op-quarantine-live", job, "active")
        self.conn.commit()

        counts = prune_history(self.conn)

        self.assertEqual(counts["admin_orphan_quarantine"], 0)
        self.assertEqual(counts["video_duplicate_jobs"], 0)
        self.assertIn(job, self._ids("video_duplicate_jobs"))
        self.assertEqual(len(self._ids("admin_audit_items", "job_id")), 1)

    def test_a_restored_quarantine_and_its_job_expire_together(self):
        job = self._job("job-restored", "done")
        self._audit_item(job)
        self._quarantine("op-quarantine-restored", job, "restored")
        self.conn.commit()

        counts = prune_history(self.conn)

        self.assertEqual(counts["admin_orphan_quarantine"], 1)
        self.assertEqual(counts["video_duplicate_jobs"], 1)
        self.assertEqual(self._ids("video_duplicate_jobs"), set())
        self.assertEqual(self._ids("admin_audit_items", "job_id"), set())

    def test_pruning_continues_past_one_batch(self):
        for index in range(BATCH_SIZE + 5):
            self.conn.execute(
                "INSERT INTO admin_action_events (method,route,status_code,outcome,created_at)"
                f" VALUES ('POST',%s,200,'completed',{OLD})", (f"/admin/api/v2/x{index}",))
        self.conn.commit()

        counts = prune_history(self.conn)

        self.assertEqual(counts["admin_action_events"], BATCH_SIZE + 5)
        self.assertEqual(self._ids("admin_action_events"), set(),
                         "pruning keeps going past the first batch")


if __name__ == "__main__":
    unittest.main()
