"""Request-level checks for resolver admin operation plans and mutations."""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from pg_helper import TEST_URL
from test_support import load_sync_module

import resolver.admin as admin_module
from tests.helpers.db_session import db_session
from tests.test_http_api import BaseHttpApiTestCase, get_job


class TestResolverHttpApiOperations(BaseHttpApiTestCase):
    def test_v2_quarantine_plan_preview_execute_and_replay(self):
        with tempfile.TemporaryDirectory(), self.admin_config():
            with db_session(TEST_URL) as conn:
                conn.execute("INSERT INTO videos (id,provider,title,performers,categories,tags,duration) VALUES ('telegram:99:plan','telegram','Plan video','[]','[]','[]',600)")
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                base_headers = {"Origin": "http://localhost"}
                denied = self.request("POST", "/admin/api/v2/operations/preview", base_url="http://localhost",
                                      headers=base_headers, json={"type": "quarantine", "video_ids": ["telegram:99:plan"]})
                headers = {**base_headers, "Authorization": "Bearer test-write-token"}
                preview = self.request("POST", "/admin/api/v2/operations/preview", base_url="http://localhost",
                                       headers=headers, json={"type": "quarantine", "video_ids": ["telegram:99:plan"]})
                plan_id = preview.json()["plan_id"]
                execute = self.request("POST", "/admin/api/v2/operations/execute", base_url="http://localhost",
                                       headers=headers, json={"plan_id": plan_id})
                replay = self.request("POST", "/admin/api/v2/operations/execute", base_url="http://localhost",
                                      headers=headers, json={"plan_id": plan_id})
                status = self.request("GET", f"/admin/api/v2/operations/{plan_id}", base_url="http://localhost")
                history = self.request("GET", "/admin/api/v2/operations?limit=10", base_url="http://localhost")
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(execute.status_code, 202, execute.text)
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertTrue(replay.json()["reused"])
            self.assertEqual(execute.json()["jobs"], replay.json()["jobs"])
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["operation"]["jobs"][0]["state"], "queued")
            self.assertEqual(history.status_code, 200, history.text)
            self.assertEqual(history.json()["items"][0]["plan_id"], plan_id)


    def test_v2_restore_plan_preview_is_authorized_and_queues_existing_undo_action(self):
        operation_id = "quarantine-plan-restore"
        snapshot = {"video": {"id": "telegram:99:restore", "title": "Restorable"},
                    "synced_files": [], "categories": [], "files": []}
        with self.admin_config():
            with db_session(TEST_URL) as conn:
                conn.execute(
                    "INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot) "
                    "VALUES ('telegram:99:restore','keeper',%s,'active',%s,%s)",
                    (operation_id, int(time.time()), json.dumps(snapshot)),
                )
            base_headers = {"Origin": "http://localhost"}
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                denied = self.request("POST", "/admin/api/v2/operations/preview", base_url="http://localhost",
                                      headers=base_headers, json={"type": "restore", "operation_id": operation_id})
                headers = {**base_headers, "Authorization": "Bearer test-write-token"}
                preview = self.request("POST", "/admin/api/v2/operations/preview", base_url="http://localhost",
                                       headers=headers, json={"type": "restore", "operation_id": operation_id})
                plan_id = preview.json()["plan_id"]
                queued = self.request("POST", "/admin/api/v2/operations/execute", base_url="http://localhost",
                                      headers=headers, json={"plan_id": plan_id})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["operation_type"], "restore")
        self.assertEqual(preview.json()["items"][0]["title"], "Restorable")
        self.assertEqual(queued.status_code, 202, queued.text)
        job = get_job(TEST_URL, queued.json()["jobs"][0]["job_id"])
        self.assertEqual(job["action"], "undo")
        self.assertEqual(job["payload"], {"operation_id": operation_id})


    def test_v2_duplicate_hide_uses_authorized_persisted_operation_plan(self):
        sync = load_sync_module()

        keeper, loser = "telegram:99:keeper", "telegram:99:hide"
        with tempfile.TemporaryDirectory(prefix="adult_hub_v2_hide_undo_") as directory, self.admin_config():
            root = Path(directory)
            media = root / "library"
            media.mkdir()
            with db_session(TEST_URL) as conn:
                for video_id, title in ((keeper, "Keeper"), (loser, "Hide me")):
                    sync.sync_unique_video({
                        "id": video_id, "provider": "telegram", "provider_video_id": video_id.split(":", 1)[1],
                        "title": title, "duration": 600, "performers": [],
                    }, set(), set(), media, conn)
                    (media / f"{sync.stable_media_basename(video_id)}.jpg").write_bytes(b"thumbnail")
            headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "_wake_worker") as wake:
                preview = self.request("POST", "/admin/api/v2/operations/preview", base_url="http://localhost",
                                       headers=headers, json={"type": "hide", "keeper_id": keeper,
                                                              "loser_ids": [loser]})
                queued = self.request("POST", "/admin/api/v2/operations/execute", base_url="http://localhost",
                                      headers=headers, json={"plan_id": preview.json()["plan_id"]})
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["operation_type"], "hide")
            self.assertEqual(preview.json()["items"][0]["title"], "Hide me")
            self.assertEqual(queued.status_code, 202, queued.text)
            job_id = queued.json()["jobs"][0]["job_id"]
            self.assertEqual(get_job(TEST_URL, job_id)["action"], "hide")
            self.assertEqual(get_job(TEST_URL, job_id)["payload"]["loser_ids"], [loser])
            with patch.object(sync, "DATA_DIR", root / "data"), \
                 patch.object(sync, "trigger_jellyfin_refresh", return_value=True) as refresh, \
                 patch.dict("os.environ", {"DATABASE_URL": TEST_URL, "JELLYFIN_URL": "http://jellyfin.invalid", "JELLYFIN_API_KEY": "test-key"}):
                with db_session(TEST_URL) as conn:
                    self.assertEqual(sync.process_video_duplicate_jobs(conn, media), 1)
            refresh.assert_called_once_with()
            with db_session(TEST_URL) as conn:
                self.assertIsNone(conn.execute("SELECT 1 FROM videos WHERE id=%s", (loser,)).fetchone())
            hidden = get_job(TEST_URL, job_id)
            self.assertEqual(hidden["state"], "done")
            self.assertEqual(hidden["result"]["local_state"], "hidden")
            quarantine = root / "data" / "duplicate-quarantine" / job_id
            self.assertTrue(list(quarantine.rglob("*.strm")))
            self.assertTrue(list(quarantine.rglob("*.nfo")))
            self.assertTrue(list(quarantine.rglob("*.jpg")))

            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "_wake_worker") as restore_wake:
                restore_preview = self.request("POST", "/admin/api/v2/operations/preview", base_url="http://localhost",
                                                headers=headers, json={"type": "restore", "operation_id": job_id})
                self.assertEqual(restore_preview.status_code, 200, restore_preview.text)
                restore_execute = self.request("POST", "/admin/api/v2/operations/execute", base_url="http://localhost",
                                               headers=headers,
                                               json={"plan_id": restore_preview.json()["plan_id"]})
                self.assertEqual(restore_execute.status_code, 202, restore_execute.text)
                restore_id = restore_execute.json()["jobs"][0]["job_id"]
                self.assertEqual(restore_wake.call_count, 1)
            with patch.object(sync, "DATA_DIR", root / "data"), \
                 patch.object(sync, "trigger_jellyfin_refresh", return_value=True), \
                 patch.dict("os.environ", {"DATABASE_URL": TEST_URL, "JELLYFIN_URL": "http://jellyfin.invalid", "JELLYFIN_API_KEY": "test-key"}):
                with db_session(TEST_URL) as conn:
                    self.assertEqual(sync.process_video_duplicate_jobs(conn, media), 1)
            with db_session(TEST_URL) as conn:
                self.assertIsNotNone(conn.execute("SELECT 1 FROM videos WHERE id=%s", (loser,)).fetchone())
            loser_base = sync.stable_media_basename(loser)
            self.assertTrue((media / f"{loser_base}.strm").exists())
            self.assertTrue((media / f"{loser_base}.nfo").exists())
            self.assertTrue((media / f"{loser_base}.jpg").exists())
            restored = get_job(TEST_URL, restore_id)
            self.assertEqual(restored["state"], "done")
            self.assertEqual(restored["result"]["local_state"], "restored")
            self.assertEqual(wake.call_count, 1)


    def test_v2_operation_plan_routes_reject_oversized_body_before_database_access(self):
        with self.admin_config():
            headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token",
                       "Content-Type": "application/json"}
            body = '{"type":"quarantine","video_ids":["' + ("x" * (16 * 1024)) + '"]}'
            preview = self.request("POST", "/admin/api/v2/operations/preview", base_url="http://localhost",
                                   headers=headers, content=body)
            execute = self.request("POST", "/admin/api/v2/operations/execute", base_url="http://localhost",
                                   headers=headers, content=body)
        self.assertEqual(preview.status_code, 413)
        self.assertEqual(execute.status_code, 413)


    def test_v2_metadata_editor_uses_persisted_operation_plan(self):
        with self.admin_config():
            with db_session(TEST_URL) as conn:
                conn.execute(
                    "INSERT INTO videos (id,provider,title,performers,categories,tags,duration) "
                    "VALUES ('telegram:99:metadata-plan','telegram','Source title','[]','[]','[]',600)"
                )
            headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                preview = self.request("POST", "/admin/api/v2/operations/preview", base_url="http://localhost",
                                       headers=headers, json={"type": "metadata_update",
                                           "video_id": "telegram:99:metadata-plan", "expected_revision": 0,
                                           "changes": {"title": "Manual title"}, "reset_fields": []})
                queued = self.request("POST", "/admin/api/v2/operations/execute", base_url="http://localhost",
                                      headers=headers, json={"plan_id": preview.json()["plan_id"]})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["fields"]["title"], {"before": "Source title", "after": "Manual title"})
        self.assertEqual(queued.status_code, 202, queued.text)
        job = get_job(TEST_URL, queued.json()["jobs"][0]["job_id"])
        self.assertEqual(job["action"], "metadata_update")
        self.assertIn("expected_source_fingerprint", job["payload"])


    def test_v2_auto_merge_exact_requires_complete_bounded_preview(self):
        sync = load_sync_module()

        keeper = "telegram:-1001:10:exactplanfid"
        loser = "telegram:-1002:20:exactplanfid"
        with tempfile.TemporaryDirectory(prefix="adult_hub_v2_exact_merge_") as directory, self.admin_config():
            root = Path(directory)
            media = root / "library"
            media.mkdir()
            with db_session(TEST_URL) as conn:
                for video_id, title, performers, studio in (
                    (keeper, "Keeper", ["Actor 1", "Actor 2"], "Studio"),
                    (loser, "Loser", ["Actor 1"], ""),
                ):
                    sync.sync_unique_video({
                        "id": video_id, "provider": "telegram",
                        "provider_video_id": video_id.split(":", 1)[1],
                        "title": title, "slug": "exactplanfid", "duration": 600,
                        "performers": performers, "studio": studio,
                    }, set(), set(), media, conn)
                    (media / f"{sync.stable_media_basename(video_id)}.jpg").write_bytes(b"thumbnail")
                # An automatic hide needs direct evidence of sameness; equal OSHASH values qualify the pair.
                with conn:
                    conn.executemany(
                        "INSERT INTO video_fingerprints (video_id,algorithm,fingerprint) "
                        "VALUES (%s,'OSHASH','0123456789abcdef')",
                        [(keeper,), (loser,)])
            headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "_wake_worker") as wake:
                preview = self.request("POST", "/admin/api/v2/operations/preview", base_url="http://localhost",
                                       headers=headers, json={"type": "auto_merge_exact"})
                queued = self.request("POST", "/admin/api/v2/operations/execute", base_url="http://localhost",
                                      headers=headers, json={"plan_id": preview.json()["plan_id"]})
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(preview.json()["operation_type"], "hide_batch")
                self.assertEqual(preview.json()["items"][0]["keeper_id"], keeper)
                self.assertEqual(preview.json()["items"][0]["video_id"], loser)
                self.assertEqual(queued.status_code, 202, queued.text)
                job_id = queued.json()["jobs"][0]["job_id"]
                job = get_job(TEST_URL, job_id)
                self.assertEqual(job["action"], "hide")
                self.assertEqual(job["payload"]["loser_ids"], [loser])
                self.assertEqual(wake.call_count, 1)
            with patch.object(sync, "DATA_DIR", root / "data"), \
                 patch.object(sync, "trigger_jellyfin_refresh", return_value=True) as refresh, \
                 patch.dict("os.environ", {"DATABASE_URL": TEST_URL, "JELLYFIN_URL": "http://jellyfin.invalid", "JELLYFIN_API_KEY": "test-key"}):
                with db_session(TEST_URL) as conn:
                    self.assertEqual(sync.process_video_duplicate_jobs(conn, media), 1)
            refresh.assert_called_once_with()
            with db_session(TEST_URL) as conn:
                self.assertIsNotNone(conn.execute("SELECT 1 FROM videos WHERE id=%s", (keeper,)).fetchone())
                self.assertIsNone(conn.execute("SELECT 1 FROM videos WHERE id=%s", (loser,)).fetchone())
                active = conn.execute(
                    "SELECT state FROM video_duplicate_decisions WHERE operation_id=%s AND loser_id=%s",
                    (job_id, loser),
                ).fetchone()
            completed = get_job(TEST_URL, job_id)
            self.assertEqual(active[0], "active")
            self.assertEqual(completed["state"], "done")
            self.assertEqual(completed["result"]["local_state"], "hidden")
            quarantine = root / "data" / "duplicate-quarantine" / job_id
            self.assertTrue(list(quarantine.rglob("*.strm")))
            self.assertTrue(list(quarantine.rglob("*.nfo")))
            self.assertTrue(list(quarantine.rglob("*.jpg")))


    def test_v2_exact_auto_merge_rejects_stale_member_without_queueing(self):
        keeper = "telegram:-1001:10:stalemergefid"
        loser = "telegram:-1002:20:stalemergefid"
        with self.admin_config():
            with db_session(TEST_URL) as conn, conn:
                conn.execute(
                    "INSERT INTO videos (id,provider,title,slug,duration,performers,studio,categories,tags) "
                    "VALUES (%s,'telegram','Keeper','stalemergefid',600,'[\"Actor 1\",\"Actor 2\"]','Studio','[]','[]'),"
                    "(%s,'telegram','Loser','stalemergefid',600,'[\"Actor 1\"]','','[]','[]')", (keeper, loser)
                )
                conn.execute(
                    "INSERT INTO synced_files (video_id,file_path,created_at) VALUES "
                    "(%s,'Keeper.strm',now()),(%s,'Loser.strm',now())", (keeper, loser)
                )
                # An automatic hide needs direct evidence of sameness; equal OSHASH values qualify the pair.
                conn.executemany(
                    "INSERT INTO video_fingerprints (video_id,algorithm,fingerprint) "
                    "VALUES (%s,'OSHASH','0123456789abcdef')",
                    [(keeper,), (loser,)])
            headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "_wake_worker") as wake:
                preview = self.request("POST", "/admin/api/v2/operations/preview", base_url="http://localhost",
                                       headers=headers, json={"type": "auto_merge_exact"})
                self.assertEqual(preview.status_code, 200, preview.text)
                self.assertEqual(preview.json()["items"][0]["video_id"], loser)
                with db_session(TEST_URL) as conn:
                    with conn:
                        conn.execute("UPDATE videos SET title='Changed after preview' WHERE id=%s", (loser,))
                before = self._job_count()
                execute = self.request("POST", "/admin/api/v2/operations/execute", base_url="http://localhost",
                                       headers=headers, json={"plan_id": preview.json()["plan_id"]})
            with db_session(TEST_URL) as conn:
                plan_state = conn.execute(
                    "SELECT state FROM admin_operation_plans WHERE plan_id=%s", (preview.json()["plan_id"],)
                ).fetchone()[0]
            self.assertEqual(execute.status_code, 409, execute.text)
            self.assertEqual(execute.json()["stale_ids"], [loser])
            self.assertEqual(self._job_count(), before)
            self.assertEqual(plan_state, "previewed")
            wake.assert_not_called()


    def test_v2_metadata_preview_and_authorized_execute_queue_durable_job(self):
        with tempfile.TemporaryDirectory(), self.admin_config():
            with db_session(TEST_URL) as conn:
                conn.execute(
                    "INSERT INTO videos (id,provider,title,plot,performers,categories,tags) "
                    "VALUES ('telegram:99:metadata','telegram','Source title','Source plot','[]','[]','[]')"
                )
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                body = {"video_id": "telegram:99:metadata", "expected_revision": 0,
                        "changes": {"title": "Manual title", "plot": ""}, "reset_fields": []}
                preview = self.request("POST", "/admin/api/v2/metadata/preview", base_url="http://localhost", json=body)
                denied = self.request("POST", "/admin/api/v2/metadata/execute", base_url="http://localhost",
                                      headers={"Origin": "http://localhost"}, json=body)
                headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
                queued = self.request("POST", "/admin/api/v2/metadata/execute", base_url="http://localhost",
                                      headers=headers, json=body)
                job = get_job(TEST_URL, queued.json()["job_id"])
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["fields"]["plot"], {"before": "Source plot", "after": ""})
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(queued.status_code, 202, queued.text)
            self.assertEqual(job["action"], "metadata_update")
            self.assertEqual(job["payload"]["changes"], {"title": "Manual title", "plot": ""})


    def test_v2_availability_preview_is_read_only_and_execute_is_authorized(self):
        sync = load_sync_module()

        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            root = Path(directory)
            with db_session(TEST_URL) as conn:
                conn.execute("INSERT INTO videos (id,provider,title,performers,categories,tags,duration) VALUES ('v-check','test','Check me','[]','[]','[]',600)")
                conn.execute("INSERT INTO dead_streams (video_id,reason,detected_at) VALUES ('v-check','old failure',1)")
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "_wake_worker") as wake:
                before = self._job_count()
                body = {"video_ids": ["v-check"]}
                preview = self.request("POST", "/admin/api/v2/availability/preview", base_url="http://localhost", json=body)
                denied = self.request("POST", "/admin/api/v2/availability/execute", base_url="http://localhost",
                                      headers={"Origin": "http://localhost"}, json=body)
                allowed = self.request("POST", "/admin/api/v2/availability/execute", base_url="http://localhost",
                                       headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json=body)
                job = get_job(TEST_URL, allowed.json()["job_id"])
                after = self._job_count()
            with patch.object(sync, "DATA_DIR", root / "data"), \
                 patch.object(sync, "probe_stream_via_resolver_outcome", return_value="available") as probe:
                with db_session(TEST_URL) as conn:
                    processed = sync.process_video_duplicate_jobs(conn, root / "media")
            completed = get_job(TEST_URL, job["id"])
            with db_session(TEST_URL) as evidence:
                check = evidence.execute(
                    "SELECT status,evidence FROM video_availability_checks WHERE job_id=%s AND video_id='v-check'",
                    (job["id"],),
                ).fetchone()
                playable = evidence.execute("SELECT playable FROM videos WHERE id='v-check'").fetchone()[0]
                dead_count = evidence.execute("SELECT count(*) FROM dead_streams WHERE video_id='v-check'").fetchone()[0]
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()["items"][0]["current"], "unavailable")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 202, allowed.text)
        self.assertEqual(after, before + 1)
        self.assertEqual(job["action"], "availability_check")
        self.assertEqual(job["payload"], {"video_ids": ["v-check"]})
        self.assertEqual(processed, 1)
        probe.assert_called_once_with("v-check")
        self.assertEqual((check[0], playable, dead_count), ("available", 1, 0))
        self.assertEqual(check[1], "A media byte range was served by the resolver playback chain.")
        self.assertEqual(completed["state"], "done")
        self.assertEqual(completed["result"]["local_state"], "availability_checked")
        wake.assert_called_once_with("jobs")


    def test_v2_quarantine_report_pages_more_than_one_thousand_active_items(self):
        snapshot_template = {"video": {"provider": "telegram"},
                            "files": [{"category": "video", "path": "/private/quarantine/media.strm"}]}
        rows = [
            (f"quarantine-video-{index:04d}", f"quarantine-video-{index:04d}",
             f"quarantine-operation-{index:04d}", "active", index,
             json.dumps({**snapshot_template, "video": {"provider": "telegram", "title": f"Video {index}"}}))
            for index in range(1001)
        ]
        with self.admin_config():
            with db_session(TEST_URL) as conn:
                with conn:
                    conn.executemany(
                        "INSERT INTO video_duplicate_decisions "
                        "(loser_id,keeper_id,operation_id,state,created_at,snapshot) "
                        "VALUES (%s,%s,%s,%s,%s,%s)", rows,
                    )
            pages = []
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                for offset in range(0, 1001, 100):
                    response = self.request(
                        "GET", f"/admin/api/v2/organize/quarantine?limit=100&offset={offset}",
                        base_url="http://localhost",
                    )
                    self.assertEqual(response.status_code, 200, response.text)
                    page = response.json()
                    self.assertEqual(page["total"], 1001)
                    self.assertEqual(page["offset"], offset)
                    self.assertEqual(page["available"], True)
                    pages.extend(page["items"])
                    self.assertEqual(page["truncated"], offset + len(page["items"]) < 1001)
                first_page = self.request(
                    "GET", "/admin/api/v2/organize/quarantine?limit=100&offset=0",
                    base_url="http://localhost",
                ).json()
                with db_session(TEST_URL) as conn:
                    with conn:
                        conn.execute(
                            "UPDATE video_duplicate_decisions SET state='undone' WHERE loser_id=%s",
                            ("quarantine-video-1000",),
                        )
                changed_page = self.request(
                    "GET", "/admin/api/v2/organize/quarantine?limit=100&offset=100",
                    base_url="http://localhost",
                ).json()
                self.assertEqual(first_page["total"], 1001)
                self.assertEqual(changed_page["total"], 1000)

        self.assertEqual(len(pages), 1001)
        self.assertEqual(len({(item["id"], item["operation_id"]) for item in pages}), 1001)
        self.assertEqual((pages[0]["id"], pages[-1]["id"]),
                         ("quarantine-video-1000", "quarantine-video-0000"))
        self.assertTrue(all(item["files"] == 1 for item in pages))
        self.assertNotIn("/private/quarantine", json.dumps(pages))



if __name__ == "__main__":
    unittest.main()
