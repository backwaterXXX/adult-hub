"""Stage state for work that runs inside a system schedule pass."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from common import schedule_stages, system_schedules
from common.admin_audit_events import list_change_events
from tests.pg_helper import PgTestCase

START = datetime(2026, 2, 1, 3, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=12)


class TestStageRegistry(unittest.TestCase):
    def test_stage_ids_unique_and_parented(self):
        ids = [stage.id for stage in schedule_stages.registry()]
        self.assertEqual(len(ids), len(set(ids)))
        for stage in schedule_stages.registry():
            self.assertIn(stage.schedule_id, {"library-sync", "database-backup"})
            self.assertTrue(stage.name and stage.details)
            self.assertEqual(schedule_stages.get_stage(stage.id), stage)
            runnable = stage.run_action is not None
            self.assertEqual(runnable, stage.run_unavailable_reason is None)

    def test_every_run_action_is_a_worker_action(self):
        from common.job_queue import WORKER_ACTIONS
        for stage in schedule_stages.registry():
            if stage.run_action:
                self.assertIn(stage.run_action, WORKER_ACTIONS)


class TestStageState(PgTestCase):
    def setUp(self):
        super().setUp()
        system_schedules.ensure_system_schedules(self.db_url)

    def row(self, stage_id):
        return next(item for item in schedule_stages.list_stage_runs(self.db_url)
                    if item["id"] == stage_id)

    def test_unrecorded_stage_defaults_to_enabled_with_first_revision(self):
        stage = self.row("history-cleanup")
        self.assertTrue(stage["enabled"])
        self.assertEqual(stage["revision"], 1)
        self.assertIsNone(stage["last_result"])
        self.assertIsNone(stage["last_run_at"])
        self.assertIsNone(stage["last_finished_at"])

    def test_finish_stores_the_real_end_time_and_keeps_the_start(self):
        schedule_stages.record_stage_start(self.conn, "database-backup", "history-cleanup", now=START)
        running = self.row("history-cleanup")
        self.assertEqual(running["last_result"], "running")
        self.assertEqual(running["last_run_at"], START.isoformat())
        self.assertIsNone(running["last_finished_at"])
        schedule_stages.record_stage_finish(self.conn, "database-backup", "history-cleanup",
                                            success=True, now=END)
        done = self.row("history-cleanup")
        self.assertEqual(done["last_result"], "done")
        self.assertEqual(done["last_run_at"], START.isoformat())
        self.assertEqual(done["last_finished_at"], END.isoformat())
        self.assertIsNone(done["last_error"])

    def test_failure_records_a_bounded_single_line_error(self):
        schedule_stages.record_stage_finish(self.conn, "library-sync", "scene-identification",
                                            success=False, now=END,
                                            error="boom\n" + "x" * 400 + " token=supersecret")
        stage = self.row("scene-identification")
        self.assertEqual(stage["last_result"], "failed")
        self.assertEqual(stage["last_finished_at"], END.isoformat())
        self.assertLessEqual(len(stage["last_error"]), schedule_stages.MAX_ERROR_LENGTH)
        self.assertNotIn("\n", stage["last_error"])

    def test_recording_survives_an_unknown_stage_and_never_raises(self):
        schedule_stages.record_stage_start(self.conn, "library-sync", "not-a-stage", now=START)
        schedule_stages.record_stage_finish(self.conn, "library-sync", "not-a-stage",
                                            success=False, now=END, error="ignored")
        self.assertEqual(len(self.row_ids()), len(schedule_stages.registry()))

    def row_ids(self):
        return [item["id"] for item in schedule_stages.list_stage_runs(self.db_url)]

    def test_states_report_only_recorded_stages_for_one_parent(self):
        self.assertEqual(schedule_stages.stage_states(self.conn, "library-sync"), {})
        schedule_stages.record_stage_start(self.conn, "library-sync", "performer-catalog", now=START)
        states = schedule_stages.stage_states(self.conn, "library-sync")
        self.assertEqual(list(states), ["performer-catalog"])
        self.assertTrue(states["performer-catalog"]["enabled"])
        self.assertEqual(schedule_stages.stage_states(self.conn, "database-backup"), {})
        self.assertTrue(schedule_stages.stage_enabled(self.conn, "library-sync", "metadata-backfill"))

    def test_disable_and_enable_bump_revision_and_audit_the_change(self):
        self.assertEqual(
            schedule_stages.set_stage_enabled(self.db_url, "library-sync", "library-maintenance",
                                              enabled=False, revision=1, now=START)["outcome"],
            "updated")
        stage = self.row("library-maintenance")
        self.assertFalse(stage["enabled"])
        self.assertEqual(stage["revision"], 2)
        self.assertEqual(
            schedule_stages.set_stage_enabled(self.db_url, "library-sync", "library-maintenance",
                                              enabled=True, revision=2, now=END)["revision"], 3)
        self.assertTrue(self.row("library-maintenance")["enabled"])
        self.assertEqual(self.row("library-maintenance")["revision"], 3)
        audit = list_change_events(self.conn, query="system-library-sync/library-maintenance")
        self.assertEqual(audit["total"], 2)
        self.assertEqual([item["changes"]["enabled"] for item in reversed(audit["items"])],
                         [{"before": True, "after": False}, {"before": False, "after": True}])

    def test_stale_revision_is_refused_and_leaves_the_stage_untouched(self):
        schedule_stages.set_stage_enabled(self.db_url, "library-sync", "source-scans",
                                          enabled=False, revision=1, now=START)
        conflict = schedule_stages.set_stage_enabled(self.db_url, "library-sync", "source-scans",
                                                     enabled=True, revision=1, now=END)
        self.assertEqual(conflict["outcome"], "conflict")
        self.assertEqual(conflict["revision"], 2)
        self.assertFalse(self.row("source-scans")["enabled"])

    def test_unknown_stage_and_wrong_parent_are_not_found(self):
        self.assertEqual(schedule_stages.set_stage_enabled(
            self.db_url, "library-sync", "nope", enabled=False, revision=1, now=START)["outcome"],
            "not_found")
        self.assertEqual(schedule_stages.set_stage_enabled(
            self.db_url, "library-sync", "history-cleanup", enabled=False, revision=1,
            now=START)["outcome"], "not_found")
        with self.assertRaises(ValueError):
            schedule_stages.set_stage_enabled(self.db_url, "library-sync", "source-scans",
                                              enabled="no", revision=1, now=START)
        with self.assertRaises(ValueError):
            schedule_stages.set_stage_enabled(self.db_url, "library-sync", "source-scans",
                                              enabled=False, revision=0, now=START)

    def test_request_run_queues_once_and_refuses_overlap(self):
        self.assertEqual(
            schedule_stages.request_stage_run(self.db_url, "library-sync", "jellyfin-refresh",
                                              now=START),
            {"outcome": "queued", "action": "collections_refresh"})
        self.assertEqual(self.row("jellyfin-refresh")["last_result"], "queued")
        self.assertEqual(self.row("jellyfin-refresh")["last_run_at"], START.isoformat())
        self.assertEqual(schedule_stages.request_stage_run(
            self.db_url, "library-sync", "jellyfin-refresh", now=END)["outcome"], "conflict")
        schedule_stages.record_stage_finish(self.conn, "library-sync", "jellyfin-refresh",
                                            success=True, now=END)
        self.assertEqual(schedule_stages.request_stage_run(
            self.db_url, "library-sync", "jellyfin-refresh", now=END)["outcome"], "queued")

    def test_request_run_needs_a_trigger_and_an_enabled_stage(self):
        with self.assertRaises(ValueError):
            schedule_stages.request_stage_run(self.db_url, "library-sync", "source-scans", now=START)
        schedule_stages.set_stage_enabled(self.db_url, "library-sync", "metadata-backfill",
                                          enabled=False, revision=1, now=START)
        self.assertEqual(schedule_stages.request_stage_run(
            self.db_url, "library-sync", "metadata-backfill", now=START)["outcome"], "disabled")
        self.assertEqual(schedule_stages.request_stage_run(
            self.db_url, "library-sync", "missing", now=START)["outcome"], "not_found")


class TestParentFinishTime(PgTestCase):
    def test_system_pass_stores_when_it_actually_finished(self):
        system_schedules.ensure_system_schedules(self.db_url)
        self.conn.execute("UPDATE system_schedules SET next_run_at=%s WHERE id='database-backup'", (START,))
        self.conn.commit()
        self.assertTrue(system_schedules.claim_due_system_schedule(
            self.db_url, "database-backup", now=START))
        system_schedules.finish_system_schedule(self.db_url, "database-backup",
                                                success=True, now=END)
        item = next(x for x in system_schedules.list_system_schedules(self.db_url)
                    if x["id"] == "database-backup")
        self.assertEqual(item["last_run_at"], START.isoformat())
        self.assertEqual(item["last_finished_at"], END.isoformat())

    def test_failed_pass_also_reports_its_end_time(self):
        system_schedules.ensure_system_schedules(self.db_url)
        self.conn.execute("UPDATE system_schedules SET next_run_at=%s WHERE id='library-sync'", (START,))
        self.conn.commit()
        self.assertTrue(system_schedules.claim_due_system_schedule(
            self.db_url, "library-sync", now=START))
        system_schedules.finish_system_schedule(self.db_url, "library-sync",
                                                success=False, now=END)
        item = next(x for x in system_schedules.list_system_schedules(self.db_url)
                    if x["id"] == "library-sync")
        self.assertEqual(item["last_result"], "failed")
        self.assertEqual(item["last_finished_at"], END.isoformat())


if __name__ == "__main__":
    unittest.main()
