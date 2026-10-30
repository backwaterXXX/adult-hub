"""DST and durable-dispatch contracts for Admin maintenance schedules."""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

from common import admin_schedules
from common.admin_audit_events import list_change_events
from common.migrations.m0055_duplicate_reconcile_opt_in import current_migration as reconcile_opt_in
from tests.pg_helper import PgTestCase


class TestScheduleTimeRules(unittest.TestCase):
    def test_cron_supports_steps_ranges_and_sunday(self):
        now = datetime(2026, 10, 30, 12, 1, tzinfo=timezone.utc)
        self.assertEqual(admin_schedules.next_cron('*/15 * * * *', after=now),
                         datetime(2026, 10, 30, 12, 15, tzinfo=timezone.utc))
        self.assertEqual(admin_schedules.next_cron('0 3 * * 0,7', after=now),
                         datetime(2026, 11, 1, 2, 0, tzinfo=timezone.utc))
        with self.assertRaises(ValueError):
            admin_schedules.next_cron('61 * * * *', after=now)
        with self.assertRaises(ValueError):
            admin_schedules.next_cron('* * * *', after=now)

    def test_cron_dst_gap_and_fold_match_daily_rules(self):
        self.assertEqual(admin_schedules.next_cron('30 2 * * *', after=datetime(2026, 3, 28, 12, tzinfo=timezone.utc)),
                         datetime(2026, 3, 30, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(admin_schedules.next_cron('30 2 * * *', after=datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)),
                         datetime(2026, 10, 26, 1, 30, tzinfo=timezone.utc))

    def test_epoch_zero_is_a_valid_instant_not_missing_time(self):
        self.assertEqual(
            admin_schedules.next_run("interval", 15, None, after=0),
            datetime(1970, 1, 1, 0, 15, tzinfo=timezone.utc),
        )

    def test_daily_schedule_skips_nonexistent_spring_wall_time(self):
        after = datetime(2026, 3, 28, 12, tzinfo=timezone.utc)
        self.assertEqual(
            admin_schedules.next_run("daily", None, "02:30", after=after),
            datetime(2026, 3, 29, 0, 30, tzinfo=timezone.utc) + timedelta(days=1),
        )

    def test_daily_schedule_skips_exact_start_of_spring_gap(self):
        after = datetime(2026, 3, 28, 12, tzinfo=timezone.utc)
        self.assertEqual(
            admin_schedules.next_run("daily", None, "02:00", after=after),
            datetime(2026, 3, 30, 0, 0, tzinfo=timezone.utc),
        )

    def test_daily_schedule_runs_same_day_after_spring_gap(self):
        after = datetime(2026, 3, 28, 12, tzinfo=timezone.utc)
        self.assertEqual(
            admin_schedules.next_run("daily", None, "03:00", after=after),
            datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc),
        )

    def test_daily_schedule_uses_first_autumn_fold(self):
        after = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
        self.assertEqual(
            admin_schedules.next_run("daily", None, "02:30", after=after),
            datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc),
        )

    def test_daily_schedule_does_not_repeat_after_first_autumn_fold(self):
        after = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(
            admin_schedules.next_run("daily", None, "02:30", after=after),
            datetime(2026, 10, 26, 1, 30, tzinfo=timezone.utc),
        )

    def test_interval_has_safe_minimum_and_maximum(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for minutes in (14, 43201, True):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                admin_schedules.next_run("interval", minutes, None, after=now)


class TestScheduleDispatch(PgTestCase):
    def test_catalog_audit_cron_updates_without_enabling_or_resetting_history(self):
        now = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        admin_schedules.dispatch_due_schedules(self.db_url, now=now)
        result = admin_schedules.set_reconcile_cron(self.db_url, 'catalog-health-full', expression='30 5 * * 1', revision=1, now=now)
        self.assertEqual(result['outcome'], 'updated')
        item = self._listed('catalog-health-full')
        self.assertTrue(item['enabled'])
        self.assertEqual(item['cron_expression'], '30 5 * * 1')
        self.assertEqual(item['next_run_at'], '2026-01-05T04:30:00+00:00')
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=datetime(2026, 1, 5, 4, 30, tzinfo=timezone.utc)), 1)
        item = self._listed('catalog-health-full')
        self.assertEqual(item['next_run_at'], '2026-01-12T04:30:00+00:00')

    def _make_due_schedule(self, now):
        created = admin_schedules.create_schedule(
            self.db_url, name="Concurrent audit", action="consistency_audit",
            schedule_type="interval", interval_minutes=15, now=now,
        )
        self.conn.execute(
            "UPDATE admin_schedules SET next_run_at=%s WHERE id=%s",
            (now, created["id"]),
        )
        self.conn.commit()
        return created

    def _listed(self, schedule_id):
        # The dispatcher's built-in rows share the fixture's created_at, so the
        # row order in the listing depends on a random schedule ID.
        return next(item for item in admin_schedules.list_schedules(self.db_url)
                    if item["id"] == schedule_id)

    def test_dispatch_is_durable_deduplicated_and_skips_active_overlap(self):
        now = datetime(2026, 1, 1, 0, tzinfo=timezone.utc)
        created = admin_schedules.create_schedule(
            self.db_url, name="Nightly audit", action="consistency_audit",
            schedule_type="interval", interval_minutes=15, now=now,
        )
        self.conn.execute(
            "UPDATE admin_schedules SET next_run_at=%s WHERE id=%s",
            (now, created["id"]),
        )
        self.conn.commit()

        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=now), 1)
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=now), 0)
        schedule = self._listed(created["id"])
        self.assertEqual(schedule["last_result"], "queued")
        job = self.conn.execute(
            "SELECT action,state,payload FROM video_duplicate_jobs WHERE id=%s", (schedule["last_job_id"],)
        ).fetchone()
        self.assertEqual(tuple(job[:2]), ("consistency_audit", "queued"))
        self.assertIn(created["id"], job[2])

        self.conn.execute("UPDATE admin_schedules SET next_run_at=%s WHERE id=%s", (now, created["id"]))
        self.conn.commit()
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=now), 0)
        schedule = self._listed(created["id"])
        self.assertEqual(schedule["last_result"], "skipped_overlap")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 1)

    def test_processing_overlap_is_skipped_and_future_due_run_resumes_after_completion(self):
        now = datetime(2026, 1, 1, 0, tzinfo=timezone.utc)
        created = self._make_due_schedule(now)
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=now), 1)
        first_job_id = self._listed(created["id"])["last_job_id"]

        self.conn.execute("UPDATE video_duplicate_jobs SET state='processing' WHERE id=%s", (first_job_id,))
        self.conn.execute("UPDATE admin_schedules SET next_run_at=%s WHERE id=%s", (now, created["id"]))
        self.conn.commit()
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=now), 0)
        self.assertEqual(self._listed(created["id"])["last_result"], "skipped_overlap")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 1)

        self.conn.execute("UPDATE video_duplicate_jobs SET state='done' WHERE id=%s", (first_job_id,))
        self.conn.execute("UPDATE admin_schedules SET next_run_at=%s WHERE id=%s", (now, created["id"]))
        self.conn.commit()
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=now), 1)
        schedule = self._listed(created["id"])
        self.assertEqual(schedule["last_result"], "queued")
        self.assertNotEqual(schedule["last_job_id"], first_job_id)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 2)

    def test_availability_schedule_enforces_distinct_twenty_id_limit(self):
        with self.assertRaises(ValueError):
            admin_schedules.create_schedule(
                self.db_url, name="Check batch", action="availability_check",
                schedule_type="interval", interval_minutes=15,
                payload={"video_ids": ["one"] * 21},
            )
        created = admin_schedules.create_schedule(
            self.db_url, name="Check batch", action="availability_check",
            schedule_type="daily", daily_at="03:00", payload={"video_ids": ["one", "two"]},
        )
        self.assertTrue(created["id"])

    def test_disable_clears_due_time_and_reenable_recomputes_it(self):
        created = admin_schedules.create_schedule(
            self.db_url, name="Daily backup", action="backup_create",
            schedule_type="daily", daily_at="03:00",
        )
        disabled = admin_schedules.set_enabled(self.db_url, created["id"], enabled=False, revision=1)
        self.assertEqual(disabled["outcome"], "updated")
        schedule = admin_schedules.list_schedules(self.db_url)[0]
        self.assertFalse(schedule["enabled"])
        self.assertIsNone(schedule["next_run_at"])
        enabled = admin_schedules.set_enabled(self.db_url, created["id"], enabled=True, revision=2)
        self.assertEqual(enabled["outcome"], "updated")
        self.assertIsNotNone(admin_schedules.list_schedules(self.db_url)[0]["next_run_at"])
        audit = list_change_events(self.conn)
        self.assertEqual([item["event_type"] for item in reversed(audit["items"])],
                         ["schedule_created", "schedule_enabled", "schedule_enabled"])
        create_event = audit["items"][-1]
        self.assertEqual(create_event["changes"]["enabled"], {"before": None, "after": True})
        self.assertNotIn("Daily backup", repr(audit))
        self.assertNotIn("video_ids", repr(audit))

    def test_two_dispatchers_enqueue_a_due_schedule_only_once(self):
        now = datetime(2026, 1, 1, 0, tzinfo=timezone.utc)
        created = self._make_due_schedule(now)
        barrier = Barrier(2)

        def dispatch():
            barrier.wait(timeout=3)
            return admin_schedules.dispatch_due_schedules(self.db_url, now=now)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: dispatch(), range(2)))

        self.assertEqual(sum(results), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM video_duplicate_jobs WHERE payload::text LIKE %s",
                (f'%{created["id"]}%',),
            ).fetchone()[0],
            1,
        )

    def test_dispatch_limit_drains_due_backlog_across_polls_without_duplicates(self):
        now = datetime(2026, 1, 1, 0, tzinfo=timezone.utc)
        schedule_ids = []
        for index in range(3):
            created = admin_schedules.create_schedule(
                self.db_url, name=f"Batch audit {index}", action="consistency_audit",
                schedule_type="interval", interval_minutes=15, now=now,
            )
            schedule_ids.append(created["id"])
            self.conn.execute(
                "UPDATE admin_schedules SET next_run_at=%s WHERE id=%s",
                (now, created["id"]),
            )
        self.conn.commit()

        # A bounded poll persists only its first batch; unprocessed schedules
        # remain due so the next runner poll can drain the backlog.
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=now, limit=2), 2)
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) FROM admin_schedules WHERE enabled=TRUE AND next_run_at<=%s",
                (now,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=now, limit=2), 1)
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=now, limit=2), 0)

        rows = self.conn.execute(
            "SELECT count(*), count(DISTINCT payload::jsonb->>'_schedule_id') "
            "FROM video_duplicate_jobs WHERE action='consistency_audit'"
        ).fetchone()
        self.assertEqual(rows, (3, 3))
        self.assertEqual(
            {row[0] for row in self.conn.execute(
                "SELECT payload::jsonb->>'_schedule_id' FROM video_duplicate_jobs "
                "WHERE action='consistency_audit'"
            ).fetchall()},
            set(schedule_ids),
        )

    def test_dispatch_rolls_back_job_when_schedule_update_fails(self):
        now = datetime(2026, 1, 1, 0, tzinfo=timezone.utc)
        created = self._make_due_schedule(now)
        self.conn.execute(
            "CREATE FUNCTION fail_schedule_update() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'simulated schedule update failure'; END $$"
        )
        self.conn.execute(
            "CREATE TRIGGER fail_schedule_update BEFORE UPDATE ON admin_schedules "
            "FOR EACH ROW EXECUTE FUNCTION fail_schedule_update()"
        )
        self.conn.commit()
        try:
            with self.assertRaises(Exception):
                admin_schedules.dispatch_due_schedules(self.db_url, now=now)
            self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 0)
            self.assertEqual(
                self.conn.execute("SELECT next_run_at FROM admin_schedules WHERE id=%s", (created["id"],)).fetchone()[0],
                int(now.timestamp()),
            )
        finally:
            self.conn.execute("DROP TRIGGER IF EXISTS fail_schedule_update ON admin_schedules")
            self.conn.execute("DROP FUNCTION IF EXISTS fail_schedule_update()")
            self.conn.commit()


class TestReconcileScheduleOptIn(PgTestCase):
    """Automatic duplicate merges are an explicit operator choice."""

    SCHEDULE_ID = "duplicate-reconcile"

    def test_cron_edit_preserves_enabled_state_and_dispatches_without_private_config(self):
        now = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        with self.conn:
            reconcile_opt_in.apply(self.conn)
        result = admin_schedules.set_reconcile_cron(self.db_url, self.SCHEDULE_ID, expression='0 3 * * *', revision=1, now=now)
        self.assertEqual(result['outcome'], 'updated')
        row = next(item for item in admin_schedules.list_schedules(self.db_url) if item['id'] == self.SCHEDULE_ID)
        self.assertEqual((row['enabled'], row['next_run_at'], row['cron_expression']), (False, None, '0 3 * * *'))
        self.assertEqual(admin_schedules.set_reconcile_cron(self.db_url, self.SCHEDULE_ID, expression='*/15 * * * *', revision=1)['outcome'], 'conflict')
        admin_schedules.set_enabled(self.db_url, self.SCHEDULE_ID, enabled=True, revision=2, now=now)
        row = next(item for item in admin_schedules.list_schedules(self.db_url) if item['id'] == self.SCHEDULE_ID)
        self.assertEqual(row['next_run_at'], '2026-01-01T02:00:00+00:00')
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=datetime(2026, 1, 1, 2, tzinfo=timezone.utc)), 1)
        payload = self.conn.execute("SELECT payload FROM video_duplicate_jobs WHERE action='duplicate_reconcile'").fetchone()[0]
        self.assertNotIn('_cron', payload)
        row = next(item for item in admin_schedules.list_schedules(self.db_url) if item['id'] == self.SCHEDULE_ID)
        self.assertEqual(row['next_run_at'], '2026-01-02T02:00:00+00:00')

    def row(self):
        return self.conn.execute(
            "SELECT enabled,next_run_at,revision FROM admin_schedules WHERE id=%s", (self.SCHEDULE_ID,)
        ).fetchone()

    def test_dispatch_no_longer_provisions_the_reconcile_schedule(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(admin_schedules.dispatch_due_schedules(self.db_url, now=now), 0)
        self.assertIsNone(self.row())

    def test_migration_seeds_the_schedule_disabled_and_repeats_itself(self):
        with self.conn:
            reconcile_opt_in.apply(self.conn)
        self.assertEqual(self.row(), (False, None, 1))
        with self.conn:
            reconcile_opt_in.apply(self.conn)
        self.assertEqual(self.row(), (False, None, 1))

    def test_migration_revokes_a_schedule_an_earlier_release_enabled(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO admin_schedules (id,name,action,payload,schedule_type,interval_minutes,"
                "timezone,enabled,revision,next_run_at) VALUES "
                "(%s,'Duplicate reconciliation','duplicate_reconcile','{}','interval',15,"
                "'Europe/Warsaw',TRUE,1,CURRENT_TIMESTAMP + INTERVAL '15 minutes')", (self.SCHEDULE_ID,))
        with self.conn:
            reconcile_opt_in.apply(self.conn)
        self.assertEqual(self.row(), (False, None, 2))
