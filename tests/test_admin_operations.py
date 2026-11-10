"""PostgreSQL coverage for persisted preview/execute operations."""

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from pg_helper import PgTestCase

from common.admin_audit_events import list_change_events
from common.admin_operations import execute_operation, list_operations, operation_status, preview_audit_import_refresh, preview_audit_repair, preview_exact_merge_plan, preview_hide, preview_metadata_batch_plan, preview_metadata_plan, preview_performer_merge_plan, preview_quarantine, preview_restore


class TestAdminOperations(PgTestCase):
    def add_video(self, video_id, title):
        with self.conn:
            self.conn.execute("INSERT INTO videos (id,provider,title,performers,categories,tags) VALUES (%s,'telegram',%s,'[]','[]','[]')",
                              (video_id, title))

    def add_active_decision(self, operation_id, video_id, title="Restorable"):
        snapshot = {"video": {"id": video_id, "title": title}, "synced_files": [],
                    "categories": [], "files": [{"source": "/library/hidden.strm",
                                                    "quarantine": "/quarantine/hidden.strm"}]}
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot) "
                "VALUES (%s,'keeper',%s,'active',%s,%s)",
                (video_id, operation_id, int(time.time()), json.dumps(snapshot)),
            )

    def add_audit_finding(self, video_id, kind="missing_nfo"):
        audit_id = uuid.uuid4().hex
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                "VALUES (%s,'consistency_audit','done','{}','{}',%s,%s)",
                (audit_id, int(time.time()), int(time.time())),
            )
            self.conn.execute(
                "INSERT INTO synced_files (file_path,video_id,category) VALUES (%s,%s,'video')",
                (f"Library/{video_id.split(':')[-1]}.strm", video_id),
            )
            finding = self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence) "
                "VALUES (%s,%s,%s,'open','test') RETURNING id", (audit_id, video_id, kind)
            ).fetchone()[0]
        return audit_id, int(finding)

    def test_audit_nfo_repair_preview_is_scoped_and_execute_queues_worker(self):
        video_id = "telegram:10:audit-nfo"
        self.add_video(video_id, "NFO title")
        audit_id, finding_id = self.add_audit_finding(video_id)
        plan = preview_audit_repair(self.db_url, audit_id, [finding_id])
        self.assertEqual(plan["outcome"], "previewed")
        self.assertEqual(plan["items"][0]["video_id"], video_id)
        self.assertNotIn("file_path", json.dumps(plan))
        queued = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(queued["outcome"], "queued")
        audit = list_change_events(self.conn)
        self.assertEqual(audit["total"], 1)
        event = audit["items"][0]
        self.assertEqual(event["event_type"], "admin_operation_queued")
        self.assertEqual(event["changes"]["state"], {"before": "previewed", "after": "queued"})
        self.assertEqual(event["changes"]["operation_type"]["after"], "audit_repair_nfo")
        self.assertNotIn(video_id, repr(event))
        job = self.conn.execute("SELECT action,payload FROM video_duplicate_jobs WHERE id=%s",
                                (queued["jobs"][0]["job_id"],)).fetchone()
        self.assertEqual(job[0], "audit_repair_nfo")
        self.assertEqual(json.loads(job[1]), {"plan_id": plan["plan_id"]})

    def test_metadata_batch_preview_and_execute_queues_per_video_atomically(self):
        video_ids = ["telegram:10:bulk-metadata-a", "telegram:10:bulk-metadata-b"]
        self.add_video(video_ids[0], "Before A")
        self.add_video(video_ids[1], "Before B")
        plan = preview_metadata_batch_plan(
            self.db_url, video_ids, {"studio": "Shared Studio", "plot": ""}, ["tags"]
        )
        self.assertEqual(plan["outcome"], "previewed")
        self.assertEqual([item["video_id"] for item in plan["items"]], video_ids)
        self.assertEqual(plan["items"][0]["fields"]["studio"], {"before": None, "after": "Shared Studio"})
        self.assertEqual(plan["items"][0]["fields"]["plot"]["after"], "")
        self.assertEqual(plan["items"][1]["fields"]["tags"]["after"], [])

        queued = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(queued["outcome"], "queued")
        self.assertEqual([job["video_id"] for job in queued["jobs"]], video_ids)
        operation_audit = list_change_events(self.conn)
        queued_event = next(item for item in operation_audit["items"]
                            if item["changes"].get("operation_type", {}).get("after") == "metadata_update_batch")
        self.assertEqual(queued_event["changes"]["requested_count"]["after"], 2)
        self.assertNotIn(video_ids[0], repr(operation_audit))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM video_duplicate_jobs WHERE action='metadata_update' AND state='queued'"
        ).fetchone()[0], 2)
        payloads = [json.loads(row[0]) for row in self.conn.execute(
            "SELECT payload FROM video_duplicate_jobs WHERE action='metadata_update' ORDER BY id"
        ).fetchall()]
        self.assertEqual({payload["video_id"] for payload in payloads}, set(video_ids))
        self.assertTrue(all(payload["changes"] == {"studio": "Shared Studio", "plot": ""}
                            and payload["reset_fields"] == ["tags"] for payload in payloads))

    def test_metadata_batch_stale_member_prevents_every_job_from_being_queued(self):
        video_ids = ["telegram:10:bulk-stale-a", "telegram:10:bulk-stale-b"]
        self.add_video(video_ids[0], "Before A")
        self.add_video(video_ids[1], "Before B")
        plan = preview_metadata_batch_plan(self.db_url, video_ids, {"studio": "Shared"}, [])
        with self.conn:
            self.conn.execute("UPDATE videos SET title='Changed' WHERE id=%s", (video_ids[1],))
        stale = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(stale["outcome"], "stale")
        self.assertEqual(stale["stale_ids"], [video_ids[1]])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM video_duplicate_jobs WHERE action='metadata_update'"
        ).fetchone()[0], 0)

    def test_metadata_job_deduplication_requires_identical_requested_patch(self):
        video_id = "telegram:10:metadata-dedup-patch"
        self.add_video(video_id, "Deduplication")
        first = preview_metadata_plan(self.db_url, video_id, 0, {"studio": "First"}, [])
        first_job = execute_operation(self.db_url, first["plan_id"])["jobs"][0]["job_id"]
        second = preview_metadata_plan(self.db_url, video_id, 0, {"plot": "Different patch"}, [])
        second_result = execute_operation(self.db_url, second["plan_id"])
        second_job = second_result["jobs"][0]["job_id"]
        self.assertNotEqual(second_job, first_job,
                            "a queued job for the same video/revision must not swallow a different patch")
        identical = preview_metadata_plan(self.db_url, video_id, 0, {"plot": "Different patch"}, [])
        identical_result = execute_operation(self.db_url, identical["plan_id"])
        self.assertEqual(identical_result["jobs"][0]["job_id"], second_job)
        self.assertTrue(identical_result["jobs"][0]["reused"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM video_duplicate_jobs WHERE action='metadata_update'"
        ).fetchone()[0], 2)

    def test_concurrent_metadata_batch_execute_consumes_one_plan_once(self):
        video_ids = ["telegram:10:bulk-concurrent-a", "telegram:10:bulk-concurrent-b"]
        self.add_video(video_ids[0], "Concurrent A")
        self.add_video(video_ids[1], "Concurrent B")
        plan = preview_metadata_batch_plan(self.db_url, video_ids, {"studio": "Shared"}, [])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: execute_operation(self.db_url, plan["plan_id"]), range(2)))
        self.assertEqual([result["outcome"] for result in results], ["queued", "queued"])
        first_jobs = [job["job_id"] for job in results[0]["jobs"]]
        second_jobs = [job["job_id"] for job in results[1]["jobs"]]
        self.assertEqual(first_jobs, second_jobs)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM video_duplicate_jobs WHERE action='metadata_update'"
        ).fetchone()[0], 2)

    def test_audit_nfo_repair_rejects_stale_catalog_or_ineligible_findings(self):
        video_id = "telegram:10:audit-stale"
        self.add_video(video_id, "Before")
        audit_id, finding_id = self.add_audit_finding(video_id)
        plan = preview_audit_repair(self.db_url, audit_id, [finding_id])
        with self.conn:
            self.conn.execute("UPDATE videos SET title='After' WHERE id=%s", (video_id,))
        stale = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(stale["outcome"], "stale")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs WHERE action='audit_repair_nfo'").fetchone()[0], 0)
        self.assertEqual(preview_audit_repair(self.db_url, audit_id, [finding_id, finding_id])["outcome"], "invalid")

    def test_audit_import_refresh_plan_is_revision_checked_and_queues_durable_worker(self):
        video_id = "telegram:10:audit-import"
        self.add_video(video_id, "Import title")
        audit_id, finding_id = self.add_audit_finding(video_id, "nfo_import_state_mismatch")
        plan = preview_audit_import_refresh(self.db_url, audit_id, [finding_id])
        self.assertEqual(plan["outcome"], "previewed")
        self.assertNotIn("file_path", json.dumps(plan))
        with self.conn:
            self.conn.execute("UPDATE videos SET title='Changed after preview' WHERE id=%s", (video_id,))
        stale = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(stale["outcome"], "stale")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs WHERE action='audit_import_refresh'").fetchone()[0], 0)
        plan = preview_audit_import_refresh(self.db_url, audit_id, [finding_id])
        self.assertEqual(plan["outcome"], "previewed")
        queued = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(queued["outcome"], "queued")
        job = self.conn.execute("SELECT action,payload FROM video_duplicate_jobs WHERE id=%s",
                                (queued["jobs"][0]["job_id"],)).fetchone()
        self.assertEqual(job[0], "audit_import_refresh")
        self.assertEqual(json.loads(job[1]), {"plan_id": plan["plan_id"]})

    def test_preview_is_persisted_and_has_explicit_safe_scope(self):
        self.add_video("telegram:10:one", "One")
        result = preview_quarantine(self.db_url, ["telegram:10:one"])
        self.assertEqual(result["outcome"], "previewed")
        self.assertEqual(result["items"][0]["title"], "One")
        self.assertEqual(result["items"][0]["file_count"], 0)
        row = self.conn.execute("SELECT operation_type,state,scope FROM admin_operation_plans WHERE plan_id=%s",
                                (result["plan_id"],)).fetchone()
        self.assertEqual(row[0:2], ("quarantine", "previewed"))
        self.assertIn("telegram:10:one", row[2])

    def test_quarantine_reuses_active_job_across_plans_and_reports_reused(self):
        video_id = "telegram:10:quarantine-reuse"
        self.add_video(video_id, "Quarantine reuse")
        first = preview_quarantine(self.db_url, [video_id])
        second = preview_quarantine(self.db_url, [video_id])
        first_result = execute_operation(self.db_url, first["plan_id"])
        second_result = execute_operation(self.db_url, second["plan_id"])

        self.assertFalse(first_result["reused"])
        self.assertTrue(second_result["jobs"][0]["reused"])
        self.assertTrue(second_result["reused"])
        self.assertEqual(second_result["jobs"][0]["job_id"], first_result["jobs"][0]["job_id"])
        audit = list_change_events(self.conn)
        reused_event = next(item for item in audit["items"] if item["object_id"] == second["plan_id"])
        self.assertTrue(reused_event["changes"]["reused"]["after"])

    def test_invalid_or_duplicate_scope_never_creates_plan(self):
        for scope in ([], ["x", "x"], ["x"] * 26):
            with self.subTest(size=len(scope)):
                result = preview_quarantine(self.db_url, scope)
                self.assertEqual(result["outcome"], "invalid")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM admin_operation_plans").fetchone()[0], 0)

    def test_changed_catalog_revision_rejects_execute_without_queueing(self):
        self.add_video("telegram:10:stale", "Before")
        plan = preview_quarantine(self.db_url, ["telegram:10:stale"])
        with self.conn:
            self.conn.execute("UPDATE videos SET title='After' WHERE id=%s", ("telegram:10:stale",))
        result = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(result["outcome"], "stale")
        self.assertEqual(result["stale_ids"], ["telegram:10:stale"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT state FROM admin_operation_plans WHERE plan_id=%s", (plan["plan_id"],)).fetchone()[0], "previewed")

    def test_changed_file_scope_rejects_execute(self):
        self.add_video("telegram:10:file-change", "File change")
        with self.conn:
            self.conn.execute("INSERT INTO synced_files (file_path,video_id,category) VALUES ('Library/one.strm','telegram:10:file-change','video')")
        plan = preview_quarantine(self.db_url, ["telegram:10:file-change"])
        with self.conn:
            self.conn.execute("UPDATE synced_files SET file_path='Library/two.strm' WHERE video_id='telegram:10:file-change'")
        result = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(result["outcome"], "stale")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 0)

    def test_hide_plan_preview_and_execute_use_revision_checked_existing_worker(self):
        self.add_video("telegram:10:keeper", "Keeper")
        self.add_video("telegram:10:hide", "Hide me")
        plan = preview_hide(self.db_url, "telegram:10:keeper", ["telegram:10:hide"])
        self.assertEqual(plan["outcome"], "previewed")
        self.assertEqual(plan["operation_type"], "hide")
        self.assertEqual(plan["items"][0]["title"], "Hide me")
        self.assertEqual(plan["items"][0]["evidence"], {"type": "operator_review"})
        queued = execute_operation(self.db_url, plan["plan_id"])
        job = self.conn.execute("SELECT action,payload FROM video_duplicate_jobs WHERE id=%s",
                                (queued["jobs"][0]["job_id"],)).fetchone()
        self.assertEqual(job[0], "hide")
        self.assertTrue(json.loads(job[1])["operator_review"])
        self.assertEqual(json.loads(job[1])["expected_revisions"].keys(),
                         {"telegram:10:keeper", "telegram:10:hide"})

    def test_hide_plan_rejects_keeper_or_loser_changes_before_queueing(self):
        self.add_video("telegram:10:keeper-stale", "Keeper")
        self.add_video("telegram:10:hide-stale", "Hide me")
        plan = preview_hide(self.db_url, "telegram:10:keeper-stale", ["telegram:10:hide-stale"])
        with self.conn:
            self.conn.execute("UPDATE videos SET title='Changed' WHERE id='telegram:10:keeper-stale'")
        result = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(result["outcome"], "stale")
        self.assertEqual(result["stale_ids"], ["telegram:10:keeper-stale"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 0)

    def test_exact_file_merge_plan_is_bounded_and_queues_existing_hide_worker(self):
        keeper = "telegram:-1001:10:sharedfid123"
        loser = "telegram:-1002:20:sharedfid123"
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id,provider,title,slug,duration,performers,studio,categories,tags) "
                "VALUES (%s,'telegram','Keeper','slug-a',600,'[\"Actor 1\",\"Actor 2\"]','Studio','[]','[]'),"
                "(%s,'telegram','Loser','slug-b',600,'[\"Actor 1\"]','','[]','[]')", (keeper, loser)
            )
            self.conn.execute("INSERT INTO synced_files (video_id,file_path,created_at) VALUES (%s,'Keeper.strm',12345)",
                              (keeper,))
            self.conn.executemany(
                "INSERT INTO video_fingerprints (video_id,algorithm,fingerprint) VALUES (%s,'OSHASH','0123456789abcdef')",
                [(keeper,), (loser,)],
            )
        plan = preview_exact_merge_plan(self.db_url)
        self.assertEqual(plan["outcome"], "previewed")
        self.assertEqual(plan["operation_type"], "hide_batch")
        self.assertEqual(plan["items"][0]["keeper_id"], keeper)
        queued = execute_operation(self.db_url, plan["plan_id"])
        replayed = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(queued["outcome"], "queued")
        self.assertTrue(replayed["reused"])
        self.assertEqual(replayed["jobs"], queued["jobs"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs WHERE action='hide'").fetchone()[0], 1)
        job = self.conn.execute("SELECT action,payload FROM video_duplicate_jobs WHERE id=%s",
                                (queued["jobs"][0]["job_id"],)).fetchone()
        self.assertEqual(job[0], "hide")
        payload = json.loads(job[1])
        self.assertEqual(payload["keeper_id"], keeper)
        self.assertEqual(payload["loser_ids"], [loser])
        self.assertEqual(set(payload["expected_revisions"]), {keeper, loser})

    def test_exact_file_merge_refuses_incomplete_candidate_scan(self):
        with patch("resolver.video_duplicates.scan_duplicate_index", return_value={"complete": False, "pairs": []}):
            result = preview_exact_merge_plan(self.db_url)
        self.assertEqual(result["outcome"], "conflict")
        self.assertIn("incomplete", result["message"])

    def test_metadata_plan_is_persisted_revision_checked_and_idempotent(self):
        self.add_video("telegram:10:metadata-plan", "Source title")
        plan = preview_metadata_plan(self.db_url, "telegram:10:metadata-plan", 0,
                                     {"title": "Manual title"}, [])
        self.assertEqual(plan["outcome"], "previewed")
        self.assertEqual(plan["fields"]["title"], {"before": "Source title", "after": "Manual title"})
        first = execute_operation(self.db_url, plan["plan_id"])
        replay = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(first["outcome"], "queued")
        self.assertEqual(replay["jobs"], first["jobs"])
        self.assertTrue(replay["reused"])
        job = self.conn.execute("SELECT action,payload FROM video_duplicate_jobs WHERE id=%s",
                                (first["jobs"][0]["job_id"],)).fetchone()
        self.assertEqual(job[0], "metadata_update")
        self.assertEqual(json.loads(job[1])["changes"], {"title": "Manual title"})

    def test_metadata_plan_rejects_changed_revision_before_enqueue(self):
        self.add_video("telegram:10:metadata-stale", "Source title")
        plan = preview_metadata_plan(self.db_url, "telegram:10:metadata-stale", 0,
                                     {"title": "Manual title"}, [])
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_metadata_overrides (video_id,fields,revision,updated_at) "
                "VALUES ('telegram:10:metadata-stale',%s,1,CURRENT_TIMESTAMP)",
                (json.dumps({"title": "Newer edit"}),),
            )
        result = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(result["outcome"], "stale")
        self.assertEqual(result["stale_ids"], ["telegram:10:metadata-stale"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 0)

    def test_performer_merge_plan_is_durable_revision_checked_and_idempotent(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id,provider,title,performers,categories,tags) "
                "VALUES ('telegram:10:performer-plan','telegram','Performer','[\"Alias Name\"]','[]','[]')"
            )
        plan = preview_performer_merge_plan(self.db_url, "Alias Name", "Canonical Name")
        self.assertEqual(plan["outcome"], "previewed")
        first = execute_operation(self.db_url, plan["plan_id"])
        replay = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(first["outcome"], "queued")
        self.assertEqual(replay["jobs"], first["jobs"])
        self.assertTrue(replay["reused"])
        job = self.conn.execute("SELECT action,payload FROM video_duplicate_jobs WHERE id=%s",
                                (first["jobs"][0]["job_id"],)).fetchone()
        payload = json.loads(job[1])
        self.assertEqual(job[0], "performer_merge")
        self.assertTrue(payload["expected_revision"].startswith("v2:"))
        self.assertEqual(len(payload["expected_revision"]), 67)
        self.assertEqual(payload["merges"], [{"alias": "Alias Name", "canonical": "Canonical Name"}])

    def test_performer_merge_plan_rejects_catalog_changes_before_enqueue(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id,provider,title,performers,categories,tags) "
                "VALUES ('telegram:10:performer-stale','telegram','Performer','[\"Alias Name\"]','[]','[]')"
            )
        plan = preview_performer_merge_plan(self.db_url, "Alias Name", "Canonical Name")
        with self.conn:
            self.conn.execute("UPDATE videos SET performers='[\"Alias Name\",\"Canonical Name\"]' "
                               "WHERE id='telegram:10:performer-stale'")
        result = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(result["outcome"], "stale")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 0)

    def test_performer_merge_plan_rejects_provider_and_local_alias_changes(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO performer_bios (name,bio,aliases) VALUES ('Alias Name','Bio','[\"Provider Alias\"]')"
            )
            self.conn.execute(
                "INSERT INTO performer_aliases (alias,canonical) VALUES ('Local Alias','Canonical Name')"
            )
        plan = preview_performer_merge_plan(self.db_url, "Alias Name", "Canonical Name")
        with self.conn:
            self.conn.execute(
                "UPDATE performer_bios SET aliases='[\"Changed Provider Alias\"]' WHERE name='Alias Name'"
            )
            self.conn.execute(
                "UPDATE performer_aliases SET alias='Changed Local Alias' WHERE canonical='Canonical Name'"
            )
        result = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(result["outcome"], "stale")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 0)

    def test_execute_is_atomic_and_repeat_returns_same_job_ids(self):
        self.add_video("telegram:10:a", "A")
        self.add_video("telegram:10:b", "B")
        plan = preview_quarantine(self.db_url, ["telegram:10:a", "telegram:10:b"])
        first = execute_operation(self.db_url, plan["plan_id"])
        second = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(first["outcome"], "queued")
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual([job["job_id"] for job in first["jobs"]], [job["job_id"] for job in second["jobs"]])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs WHERE action='quarantine'").fetchone()[0], 2)

    def test_concurrent_execute_returns_one_stable_job_group(self):
        self.add_video("telegram:10:concurrent", "Concurrent")
        plan = preview_quarantine(self.db_url, ["telegram:10:concurrent"])
        start = threading.Barrier(2)

        def execute():
            start.wait(timeout=5)
            return execute_operation(self.db_url, plan["plan_id"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [future.result(timeout=10) for future in (pool.submit(execute), pool.submit(execute))]
        self.assertEqual(results[0]["jobs"], results[1]["jobs"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs WHERE action='quarantine'").fetchone()[0], 1)

    def test_partial_enqueue_failure_rolls_back_entire_plan(self):
        self.add_video("telegram:10:rollback-a", "Rollback A")
        self.add_video("telegram:10:rollback-b", "Rollback B")
        plan = preview_quarantine(self.db_url, ["telegram:10:rollback-a", "telegram:10:rollback-b"])
        with patch("common.admin_operations.uuid.uuid4", side_effect=[uuid.UUID(int=1), RuntimeError("injected failure")]):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT state FROM admin_operation_plans WHERE plan_id=%s", (plan["plan_id"],)).fetchone()[0], "previewed")

    def test_expired_plan_cannot_enqueue(self):
        self.add_video("telegram:10:expired", "Expired")
        plan = preview_quarantine(self.db_url, ["telegram:10:expired"])
        with self.conn:
            self.conn.execute("UPDATE admin_operation_plans SET expires_at=to_timestamp(%s) WHERE plan_id=%s",
                              (int(time.time()) - 2, plan["plan_id"]))
        self.assertEqual(execute_operation(self.db_url, plan["plan_id"])["outcome"], "expired")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 0)

    def test_status_reports_independent_item_failures(self):
        self.add_video("telegram:10:ok", "OK")
        self.add_video("telegram:10:failed", "Failed")
        plan = preview_quarantine(self.db_url, ["telegram:10:ok", "telegram:10:failed"])
        queued = execute_operation(self.db_url, plan["plan_id"])
        with self.conn:
            for index, job in enumerate(queued["jobs"]):
                self.conn.execute("UPDATE video_duplicate_jobs SET state=%s WHERE id=%s",
                                  ("done" if index == 0 else "failed", job["job_id"]))
        status = operation_status(self.db_url, plan["plan_id"])
        self.assertEqual([item["state"] for item in status["jobs"]], ["done", "failed"])

    def test_operation_history_is_paginated_and_does_not_expose_scope_payloads(self):
        self.add_video("telegram:10:history-a", "History A")
        self.add_video("telegram:10:history-b", "History B")
        first = preview_quarantine(self.db_url, ["telegram:10:history-a"])
        second = preview_quarantine(self.db_url, ["telegram:10:history-b"])
        with self.conn:
            self.conn.execute("UPDATE admin_operation_plans SET created_at=created_at + interval '2 seconds' WHERE plan_id=%s",
                              (second["plan_id"],))
        execute_operation(self.db_url, second["plan_id"])

        page = list_operations(self.db_url, limit=1, offset=0)
        previous = list_operations(self.db_url, limit=1, offset=1)
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["items"][0]["plan_id"], second["plan_id"])
        self.assertEqual(page["items"][0]["task_states"], {"queued": 1})
        self.assertEqual(previous["items"][0]["plan_id"], first["plan_id"])
        self.assertNotIn("scope", page["items"][0])
        self.assertNotIn("preview", page["items"][0])

    def test_restore_preview_and_execute_are_revision_checked_and_idempotent(self):
        operation_id = "hide-group-restore"
        self.add_active_decision(operation_id, "telegram:10:restore-a", "Restore A")
        self.add_active_decision(operation_id, "telegram:10:restore-b", "Restore B")
        first = preview_restore(self.db_url, operation_id)
        second = preview_restore(self.db_url, operation_id)
        self.assertEqual(first["outcome"], "previewed")
        self.assertEqual(first["operation_type"], "restore")
        self.assertEqual([item["title"] for item in first["items"]], ["Restore A", "Restore B"])
        self.assertTrue(all("source" not in str(item) for item in first["items"]))

        queued = execute_operation(self.db_url, first["plan_id"])
        repeated = execute_operation(self.db_url, first["plan_id"])
        second_plan = execute_operation(self.db_url, second["plan_id"])
        self.assertEqual(queued["outcome"], "queued")
        self.assertEqual(repeated["jobs"], queued["jobs"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(second_plan["jobs"][0]["job_id"], queued["jobs"][0]["job_id"])
        job = self.conn.execute(
            "SELECT action,payload FROM video_duplicate_jobs WHERE id=%s", (queued["jobs"][0]["job_id"],)
        ).fetchone()
        self.assertEqual(job[0], "undo")
        self.assertEqual(json.loads(job[1]), {"operation_id": operation_id})

    def test_changed_restore_snapshot_invalidates_preview(self):
        operation_id = "hide-group-stale"
        self.add_active_decision(operation_id, "telegram:10:restore-stale", "Before")
        plan = preview_restore(self.db_url, operation_id)
        self.add_active_decision("different", "telegram:10:other")
        with self.conn:
            self.conn.execute(
                "UPDATE video_duplicate_decisions SET snapshot=%s WHERE operation_id=%s",
                (json.dumps({"video": {"id": "telegram:10:restore-stale", "title": "Changed"}, "files": []}),
                 operation_id),
            )
        result = execute_operation(self.db_url, plan["plan_id"])
        self.assertEqual(result["outcome"], "stale")
        self.assertEqual(result["stale_ids"], ["telegram:10:restore-stale"])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0], 0)

    def test_restore_preview_rejects_invalid_or_oversized_snapshots(self):
        for index in range(26):
            self.add_active_decision("restore-too-large", f"telegram:10:restore-{index}")
        self.assertEqual(preview_restore(self.db_url, "restore-too-large")["outcome"], "not_found")
        self.add_active_decision("restore-malformed", "telegram:10:restore-bad")
        with self.conn:
            self.conn.execute(
                "UPDATE video_duplicate_decisions SET snapshot=%s WHERE operation_id='restore-malformed'",
                (json.dumps({"video": {"id": "telegram:10:restore-bad"}, "files": []}),),
            )
        self.assertEqual(preview_restore(self.db_url, "restore-malformed")["outcome"], "not_found")


if __name__ == "__main__":
    unittest.main()
