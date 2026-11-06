"""Behavioral unit tests for Admin v2 source endpoints and concurrency (W2, W3)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from starlette.requests import Request
from starlette.responses import JSONResponse

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resolver"))

try:
    from resolver import admin_v2_sources
except (ImportError, ModuleNotFoundError):
    import admin_v2_sources

from common.managed_sources import ManagedConfigConflict, ManagedConfigLockTimeout


class InlineAsyncio:
    @staticmethod
    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    @staticmethod
    async def gather(*awaitables):
        return tuple([await item for item in awaitables])


def make_post_request(path: str, body_dict: dict) -> Request:
    body_bytes = json.dumps(body_dict).encode("utf-8")
    messages = [
        {"type": "http.request", "body": body_bytes, "more_body": False},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body_bytes)).encode("ascii")),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "root_path": "",
    }
    return Request(scope, receive)


def make_admin(blocked=False):
    return SimpleNamespace(
        HUB_DB_URL="postgresql://test/hub",
        CONFIG_PATH="tests/fixtures/config_telegram_only.yaml",
        _admin_blocked=lambda _req: blocked,
        _write_denied=lambda _req: None,
        _wake_worker=lambda _queue: None,
        JSONResponse=JSONResponse,
        asyncio=InlineAsyncio,
    )


class TestAdminV2SourcesConcurrency(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.overlay_path = Path(self.temp_dir.name) / "admin-source-overrides.json"
        self.prev_env = os.environ.get("ADMIN_SOURCE_OVERRIDES_PATH")
        os.environ["ADMIN_SOURCE_OVERRIDES_PATH"] = str(self.overlay_path)
        self.addCleanup(self._cleanup)

        self.admin = make_admin()
        self.router = admin_v2_sources.build_router(self.admin)
        self.routes = {route.path: route.endpoint for route in self.router.routes}

    def _cleanup(self):
        if self.prev_env is None:
            os.environ.pop("ADMIN_SOURCE_OVERRIDES_PATH", None)
        else:
            os.environ["ADMIN_SOURCE_OVERRIDES_PATH"] = self.prev_env
        self.temp_dir.cleanup()

    async def test_safe_error_returns_503_on_managed_config_lock_timeout(self):
        req = make_post_request("/api/v2/sources/save", {"expected_revision": 1, "overrides": {}})
        with patch("common.managed_sources.read_overrides", side_effect=ManagedConfigLockTimeout("Locked")):
            resp = await self.routes["/api/v2/sources/save"](req)
        self.assertEqual(resp.status_code, 503)
        body = json.loads(resp.body)
        self.assertIn("temporarily locked", body["error"])

    async def test_remove_source_commits_config_before_enqueue(self):
        call_order = []

        def mock_update(config_path, candidate, expected_revision, **kwargs):
            call_order.append(("update_overrides", expected_revision, candidate))
            return {"revision": expected_revision + 1}

        def mock_enqueue(db_url, action, payload):
            call_order.append(("enqueue_job", action, payload))
            return "job-123"

        with patch("common.managed_sources.read_overrides", return_value={"revision": 2, "overrides": {}}), \
             patch("common.managed_sources.read_pending_source_audit", return_value=None), \
             patch("common.managed_sources.preview_overrides", return_value={"changes": [], "truncated": False}), \
             patch("common.managed_sources.update_overrides", side_effect=mock_update), \
             patch("common.job_queue.enqueue_job", side_effect=mock_enqueue), \
             patch.object(admin_v2_sources, "is_channel_removal_active", return_value=False):

            req = make_post_request(
                "/api/v2/sources/remove",
                {"expected_revision": 2, "channel_id": "-1001"},
            )
            resp = await self.routes["/api/v2/sources/remove"](req)

        self.assertEqual(resp.get("ok"), True)
        self.assertEqual(resp.get("job_id"), "job-123")
        # Assert update_overrides was called BEFORE enqueue_job (W3)
        self.assertEqual(len(call_order), 2)
        self.assertEqual(call_order[0][0], "update_overrides")
        self.assertEqual(call_order[1][0], "enqueue_job")

    async def test_remove_source_failed_enqueue_triggers_cas_rollback(self):
        call_order = []

        def mock_update(config_path, candidate, expected_revision, **kwargs):
            call_order.append(("update_overrides", expected_revision, candidate))
            return {"revision": expected_revision + 1}

        def mock_enqueue(db_url, action, payload):
            call_order.append(("enqueue_job", action))
            raise RuntimeError("DB connection failure")

        with patch("common.managed_sources.read_overrides", return_value={"revision": 2, "overrides": {}}), \
             patch("common.managed_sources.read_pending_source_audit", return_value=None), \
             patch("common.managed_sources.preview_overrides", return_value={"changes": [], "truncated": False}), \
             patch("common.managed_sources.update_overrides", side_effect=mock_update), \
             patch("common.job_queue.enqueue_job", side_effect=mock_enqueue), \
             patch.object(admin_v2_sources, "is_channel_removal_active", return_value=False):

            req = make_post_request(
                "/api/v2/sources/remove",
                {"expected_revision": 2, "channel_id": "-1001"},
            )
            resp = await self.routes["/api/v2/sources/remove"](req)

        self.assertEqual(resp.status_code, 503)
        # Should have called:
        # 1. update_overrides (expected_revision=2) adding channel
        # 2. enqueue_job (fails)
        # 3. update_overrides (expected_revision=3) compensating rollback
        self.assertEqual(len(call_order), 3)
        self.assertEqual(call_order[0][0], "update_overrides")
        self.assertEqual(call_order[0][1], 2)
        self.assertEqual(call_order[1][0], "enqueue_job")
        self.assertEqual(call_order[2][0], "update_overrides")
        self.assertEqual(call_order[2][1], 3)
        # Rollback candidate should have removed_channels stripped of -1001
        rollback_candidate = call_order[2][2]
        self.assertNotIn("-1001", rollback_candidate.get("telegram", {}).get("removed_channels", []))

    async def test_remove_source_when_already_active_returns_409(self):
        with patch("common.managed_sources.read_overrides", return_value={"revision": 2, "overrides": {}}), \
             patch("common.managed_sources.read_pending_source_audit", return_value=None), \
             patch.object(admin_v2_sources, "is_channel_removal_active", return_value=True):

            req = make_post_request(
                "/api/v2/sources/remove",
                {"expected_revision": 2, "channel_id": "-1001"},
            )
            resp = await self.routes["/api/v2/sources/remove"](req)

        self.assertEqual(resp.status_code, 409)
        body = json.loads(resp.body)
        self.assertIn("already queued or in progress", body["error"])

    async def test_add_telegram_channel_when_deletion_active_returns_409(self):
        with patch("common.managed_sources.read_overrides", return_value={"revision": 2, "overrides": {"telegram": {"removed_channels": ["-100999"]}}}), \
             patch("common.managed_sources.read_pending_source_audit", return_value=None), \
             patch.object(admin_v2_sources, "is_channel_removal_active", return_value=True):

            req = make_post_request(
                "/api/v2/sources",
                {"expected_revision": 2, "channel": {"id": "-100999", "studio": "Studio"}},
            )
            resp = await self.routes["/api/v2/sources"](req)

        self.assertEqual(resp.status_code, 409)
        body = json.loads(resp.body)
        self.assertIn("Channel deletion is in progress", body["error"])

    async def test_is_channel_removal_active_detects_processing_state(self):
        class MockConn:
            def __init__(self):
                self.sql = ""
            def execute(self, sql, params=()):
                self.sql = sql
                return self
            def fetchall(self):
                return [(json.dumps({"channel_id": "-100999"}),)]
            def close(self):
                pass

        mock_conn = MockConn()
        with patch("common.job_queue.connect_queue", return_value=mock_conn):
            self.assertTrue(admin_v2_sources.is_channel_removal_active(self.admin, "-100999"))
            self.assertIn("'processing'", mock_conn.sql)
            self.assertIn("'queued'", mock_conn.sql)
            self.assertNotIn("'running'", mock_conn.sql)

            # Re-adding a removed channel while processing returns 409 Conflict
            with patch("common.managed_sources.read_overrides", return_value={"revision": 0, "overrides": {"telegram": {"removed_channels": ["-100999"]}}}):
                req = make_post_request(
                    "/api/v2/sources",
                    {"expected_revision": 0, "channel": {"id": "-100999", "studio": "Studio"}},
                )
                resp = await self.routes["/api/v2/sources"](req)
                self.assertEqual(resp.status_code, 409)

    async def test_is_channel_removal_active_db_failure_fails_closed_with_503(self):
        with patch("common.job_queue.connect_queue", side_effect=RuntimeError("DB lookup failed")):
            with self.assertRaises(admin_v2_sources.ChannelRemovalLookupError):
                admin_v2_sources.is_channel_removal_active(self.admin, "-100777")

            # Re-adding a removed channel fails closed with 503 retryable error
            with patch("common.managed_sources.read_overrides", return_value={"revision": 0, "overrides": {"telegram": {"removed_channels": ["-100777"]}}}):
                req = make_post_request(
                    "/api/v2/sources",
                    {"expected_revision": 0, "channel": {"id": "-100777", "studio": "Studio"}},
                )
                resp = await self.routes["/api/v2/sources"](req)
                self.assertEqual(resp.status_code, 503)
                body = json.loads(resp.body)
                self.assertIn("Could not verify active channel operations", body["error"])

    async def test_remove_source_real_compensation_and_outbox_on_enqueue_failure(self):
        from common.managed_sources import read_overrides
        self.assertEqual(read_overrides(self.overlay_path)["revision"], 0)

        with patch("common.job_queue.enqueue_job", side_effect=RuntimeError("Job queue down")), \
             patch.object(admin_v2_sources, "is_channel_removal_active", return_value=False):

            req = make_post_request(
                "/api/v2/sources/remove",
                {"expected_revision": 0, "channel_id": "-1001"},
            )
            resp = await self.routes["/api/v2/sources/remove"](req)

        self.assertEqual(resp.status_code, 503)
        body = json.loads(resp.body)
        self.assertIn("left unchanged", body["error"])

        # Real overlay on disk: revision was compensated from 1 to 2
        overlay = read_overrides(self.overlay_path)
        self.assertEqual(overlay["revision"], 2)
        # Channel is NOT removed from configuration
        self.assertNotIn("-1001", overlay["overrides"].get("telegram", {}).get("removed_channels", []))

    async def test_remove_source_concurrent_conflict_during_compensation_returns_honest_error(self):
        from common.managed_sources import read_overrides, update_overrides
        self.assertEqual(read_overrides(self.overlay_path)["revision"], 0)

        def enqueue_with_concurrent_edit(db_url, action, payload):
            # Concurrent process increments revision from 1 to 2
            update_overrides(
                self.admin.CONFIG_PATH,
                {"telegram": {"channels": {"-1002": {"studio": "Other"}}}},
                expected_revision=1,
                path=self.overlay_path,
                allow_compensation=True,
            )
            raise RuntimeError("Queue failure")

        with patch("common.job_queue.enqueue_job", side_effect=enqueue_with_concurrent_edit), \
             patch.object(admin_v2_sources, "is_channel_removal_active", return_value=False):

            req = make_post_request(
                "/api/v2/sources/remove",
                {"expected_revision": 0, "channel_id": "-1001"},
            )
            resp = await self.routes["/api/v2/sources/remove"](req)

        self.assertEqual(resp.status_code, 500)
        body = json.loads(resp.body)
        self.assertIn("compensating configuration rollback failed", body["error"])
        self.assertNotIn("left unchanged", body["error"])

    async def test_remove_source_aborts_when_prior_audit_pending(self):
        from common.managed_sources import audit_outbox_path, update_overrides
        update_overrides(
            self.admin.CONFIG_PATH, {}, expected_revision=0, path=self.overlay_path,
        )
        audit_outbox_path(self.overlay_path).write_text(
            json.dumps({
                "revision": 1,
                "changes": [{"path": ["telegram", "enabled"], "before": True, "after": False}],
                "truncated": False,
            }),
            encoding="utf-8",
        )
        with patch.object(admin_v2_sources, "is_channel_removal_active", return_value=False):
            req = make_post_request(
                "/api/v2/sources/remove",
                {"expected_revision": 1, "channel_id": "-1001"},
            )
            resp = await self.routes["/api/v2/sources/remove"](req)
        self.assertEqual(resp.status_code, 503)
        body = json.loads(resp.body)
        self.assertIn("is still syncing", body["error"])

    async def test_save_sources_when_restoring_channel_under_deletion_returns_409(self):
        current_overrides = {"telegram": {"removed_channels": ["-100999"]}}
        candidate_overrides = {"telegram": {"channels": {"-100999": {"studio": "Studio"}}}}

        with patch("common.managed_sources.read_overrides", return_value={"revision": 2, "overrides": current_overrides}), \
             patch("common.managed_sources.read_pending_source_audit", return_value=None), \
             patch.object(admin_v2_sources, "is_channel_removal_active", return_value=True):

            req = make_post_request(
                "/api/v2/sources/save",
                {"expected_revision": 2, "overrides": candidate_overrides},
            )
            resp = await self.routes["/api/v2/sources/save"](req)

        self.assertEqual(resp.status_code, 409)
        body = json.loads(resp.body)
        self.assertIn("Channel deletion is in progress", body["error"])

    async def test_new_channel_creation_offline_when_db_down(self):
        from common.managed_sources import read_overrides, read_pending_source_audit
        self.admin.HUB_DB_URL = "postgresql://127.0.0.1:1/down"

        req = make_post_request(
            "/api/v2/sources",
            {"expected_revision": 0, "channel": {"id": "-100888", "studio": "Studio New"}},
        )
        resp = await self.routes["/api/v2/sources"](req)

        self.assertIsInstance(resp, dict)
        self.assertTrue(resp.get("ok"))
        self.assertEqual(resp.get("revision"), 1)
        self.assertTrue(resp.get("audit_pending"))

        overlay = read_overrides(self.overlay_path)
        self.assertEqual(overlay["revision"], 1)
        self.assertIn("-100888", overlay["overrides"]["telegram"]["channels"])
        self.assertIsNotNone(read_pending_source_audit(self.overlay_path))

    async def test_save_sources_offline_when_db_down(self):
        from common.managed_sources import read_overrides, read_pending_source_audit
        self.admin.HUB_DB_URL = "postgresql://127.0.0.1:1/down"

        req = make_post_request(
            "/api/v2/sources/save",
            {
                "expected_revision": 0,
                "overrides": {"telegram": {"channels": {"-100111": {"studio": "Managed"}}}}
            },
        )
        resp = await self.routes["/api/v2/sources/save"](req)

        self.assertIsInstance(resp, dict)
        self.assertTrue(resp.get("ok"))
        self.assertEqual(resp.get("revision"), 1)
        self.assertTrue(resp.get("audit_pending"))

        overlay = read_overrides(self.overlay_path)
        self.assertEqual(overlay["revision"], 1)
        self.assertEqual(overlay["overrides"]["telegram"]["channels"]["-100111"]["studio"], "Managed")
        self.assertIsNotNone(read_pending_source_audit(self.overlay_path))

    async def test_save_sources_restoring_removed_channel_when_db_down_fails_closed(self):
        from common.managed_sources import update_overrides, read_overrides
        update_overrides(
            self.admin.CONFIG_PATH,
            {"telegram": {"removed_channels": ["-100999"]}},
            expected_revision=0,
            path=self.overlay_path,
        )
        self.admin.HUB_DB_URL = "postgresql://127.0.0.1:1/down"

        # Operator attempts to restore -100999 by dropping removed_channels while DB is down
        req = make_post_request(
            "/api/v2/sources/save",
            {
                "expected_revision": 1,
                "overrides": {"telegram": {"channels": {"-100999": {"studio": "Restored"}}}}
            },
        )
        resp = await self.routes["/api/v2/sources/save"](req)

        self.assertEqual(resp.status_code, 503)
        body = json.loads(resp.body)
        self.assertIn("Could not verify active channel operations", body["error"])

        # Overlay revision must remain unchanged
        self.assertEqual(read_overrides(self.overlay_path)["revision"], 1)

    async def test_active_channel_lock_blocks_readd_immediately_with_409(self):
        import fcntl
        from common.managed_sources import channel_lock_path
        self.admin.HUB_DB_URL = "postgresql://127.0.0.1:1/down"
        lock_p = channel_lock_path("-100999", path=self.overlay_path)
        lock_p.parent.mkdir(parents=True, exist_ok=True)
        with lock_p.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            # create_source blocks with 409 even when DB is down
            req = make_post_request(
                "/api/v2/sources",
                {"expected_revision": 0, "channel": {"id": "-100999", "studio": "Studio"}},
            )
            resp = await self.routes["/api/v2/sources"](req)
            self.assertEqual(resp.status_code, 409)
            body = json.loads(resp.body)
            self.assertIn("Channel deletion is in progress", body["error"])

            # Alias '999' also blocked with 409
            req_alias = make_post_request(
                "/api/v2/sources",
                {"expected_revision": 0, "channel": {"id": "999", "studio": "Studio"}},
            )
            resp_alias = await self.routes["/api/v2/sources"](req_alias)
            self.assertEqual(resp_alias.status_code, 409)

            # save_sources with -100999 also blocks with 409
            req_save = make_post_request(
                "/api/v2/sources/save",
                {
                    "expected_revision": 0,
                    "overrides": {"telegram": {"channels": {"-100999": {"studio": "Studio"}}}}
                },
            )
            resp_save = await self.routes["/api/v2/sources/save"](req_save)
            self.assertEqual(resp_save.status_code, 409)

    async def test_aliases_handled_for_removal_and_readd(self):
        from common.managed_sources import update_overrides, read_overrides
        update_overrides(
            self.admin.CONFIG_PATH,
            {"telegram": {"removed_channels": ["-100999"]}},
            expected_revision=0,
            path=self.overlay_path,
        )

        class MockConn:
            def __init__(self, rows):
                self.rows = rows
            def execute(self, sql, params=()):
                return self
            def fetchall(self):
                return self.rows
            def close(self):
                pass

        # 1. Job in queue stored under alias '-100999' blocks re-adding alias '999'
        with patch("common.job_queue.connect_queue", return_value=MockConn([(json.dumps({"channel_id": "-100999"}),)])):
            req = make_post_request(
                "/api/v2/sources",
                {"expected_revision": 1, "channel": {"id": "999", "studio": "Studio"}},
            )
            resp = await self.routes["/api/v2/sources"](req)
            self.assertEqual(resp.status_code, 409)

        # 2. After job finishes, re-adding '999' succeeds and clears '-100999' from removed_channels
        with patch("common.job_queue.connect_queue", return_value=MockConn([])), \
             patch.object(self.router, "routes", self.routes):
            req = make_post_request(
                "/api/v2/sources",
                {"expected_revision": 1, "channel": {"id": "999", "studio": "Studio"}},
            )
            resp = await self.routes["/api/v2/sources"](req)
            self.assertIsInstance(resp, dict)
            self.assertTrue(resp.get("ok"))
            self.assertEqual(resp.get("revision"), 2)

            overlay = read_overrides(self.overlay_path)
            self.assertNotIn("-100999", overlay["overrides"].get("telegram", {}).get("removed_channels", []))
            self.assertNotIn("999", overlay["overrides"].get("telegram", {}).get("removed_channels", []))

    async def test_save_sources_renaming_other_channel_while_another_channel_is_removed_succeeds(self):
        import fcntl
        from common.managed_sources import update_overrides, channel_lock_path
        update_overrides(
            self.admin.CONFIG_PATH,
            {"telegram": {"channels": {"-100111": {"studio": "Chan 1"}}, "removed_channels": ["-100999"]}},
            expected_revision=0,
            path=self.overlay_path,
        )
        lock_file = channel_lock_path("-100999", path=self.overlay_path)
        with lock_file.open("a+b") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                req_save = make_post_request(
                    "/api/v2/sources/save",
                    {
                        "expected_revision": 1,
                        "overrides": {
                            "telegram": {
                                "channels": {"-100111": {"studio": "Renamed Chan 1"}},
                                "removed_channels": ["-100999"],
                            }
                        }
                    },
                )
                resp = await self.routes["/api/v2/sources/save"](req_save)
                self.assertIsInstance(resp, dict)
                self.assertTrue(resp.get("ok"))
                self.assertEqual(resp.get("revision"), 2)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    async def test_save_sources_rename_while_removal_active_is_queued_and_executed_after(self):
        import fcntl
        from common.managed_sources import update_overrides, read_overrides, channel_lock_path
        from sync.duplicate_job_external_handlers import (
            execute_external_operation,
            ChannelRemovalStillActive,
        )

        update_overrides(
            self.admin.CONFIG_PATH,
            {
                "telegram": {
                    "channels": {"-100999": {"studio": "Original Name"}},
                    "removed_channels": ["-100999"],
                }
            },
            expected_revision=0,
            path=self.overlay_path,
        )

        enqueued = []
        def mock_enqueue(db_url, action, payload):
            job_id = f"job-{len(enqueued) + 1}"
            enqueued.append((action, payload, job_id))
            return job_id

        lock_file = channel_lock_path("-100999", path=self.overlay_path)
        with lock_file.open("a+b") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                with patch("common.job_queue.enqueue_job", side_effect=mock_enqueue):
                    req_save = make_post_request(
                        "/api/v2/sources/save",
                        {
                            "expected_revision": 1,
                            "overrides": {
                                "telegram": {
                                    "channels": {"-100999": {"studio": "Renamed Studio"}},
                                    "removed_channels": [],
                                }
                            },
                        },
                    )
                    resp = await self.routes["/api/v2/sources/save"](req_save)

                # HTTP response is 202 Accepted, queued without timeout or 409
                self.assertEqual(resp.status_code, 202)
                body = json.loads(resp.body)
                self.assertTrue(body.get("ok"))
                self.assertTrue(body.get("queued"))
                self.assertEqual(body.get("job_id"), "job-1")
                self.assertEqual(len(enqueued), 1)
                action, payload, jid = enqueued[0]
                self.assertEqual(action, "source_readd")
                self.assertEqual(payload["channel_id"], "-100999")
                self.assertEqual(payload["studio"], "Renamed Studio")

                # Payload execution during active lock raises ChannelRemovalStillActive (re-queue)
                payload["overrides_path"] = self.overlay_path
                payload["config_path"] = self.admin.CONFIG_PATH

                class MockConn:
                    def execute(self, sql, params=()):
                        class MockResult:
                            def fetchall(self): return []
                            def fetchone(self): return None
                        return MockResult()

                with self.assertRaises(ChannelRemovalStillActive):
                    execute_external_operation(
                        action, MockConn(), jid, payload, Path("/tmp"), None
                    )

            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        # After removal lock is released, worker execution succeeds and updates configuration
        result, committed = execute_external_operation(
            action, MockConn(), jid, payload, Path("/tmp"), None
        )
        self.assertTrue(committed)
        self.assertEqual(result["local_state"], "source_readded")
        self.assertEqual(result["channel_id"], "-100999")

        # Overrides verify: removed_channels is empty, channels[-100999] updated with new studio
        overlay = read_overrides(self.overlay_path)
        self.assertEqual(overlay["revision"], 2)
        tg = overlay["overrides"]["telegram"]
        self.assertNotIn("-100999", tg.get("removed_channels", []))
        self.assertIn("-100999", tg["channels"])
        self.assertEqual(tg["channels"]["-100999"]["studio"], "Renamed Studio")
        self.assertTrue(tg["channels"]["-100999"]["enabled"])

    async def test_create_source_readd_while_removal_active_is_queued_and_executed_after(self):
        import fcntl
        from common.managed_sources import update_overrides, read_overrides, channel_lock_path
        from sync.duplicate_job_external_handlers import (
            execute_external_operation,
            ChannelRemovalStillActive,
        )

        update_overrides(
            self.admin.CONFIG_PATH,
            {
                "telegram": {
                    "channels": {},
                    "removed_channels": ["-100555"],
                }
            },
            expected_revision=0,
            path=self.overlay_path,
        )

        enqueued = []
        def mock_enqueue(db_url, action, payload):
            job_id = f"job-{len(enqueued) + 1}"
            enqueued.append((action, payload, job_id))
            return job_id

        lock_file = channel_lock_path("-100555", path=self.overlay_path)
        with lock_file.open("a+b") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                with patch("common.job_queue.enqueue_job", side_effect=mock_enqueue):
                    req_create = make_post_request(
                        "/api/v2/sources",
                        {
                            "expected_revision": 1,
                            "channel": {"id": "-100555", "studio": "Readded Studio"},
                        },
                    )
                    resp = await self.routes["/api/v2/sources"](req_create)

                self.assertEqual(resp.status_code, 202)
                body = json.loads(resp.body)
                self.assertTrue(body.get("ok"))
                self.assertTrue(body.get("queued"))
                self.assertEqual(body.get("job_id"), "job-1")
                self.assertEqual(len(enqueued), 1)
                action, payload, jid = enqueued[0]
                self.assertEqual(action, "source_readd")
                self.assertEqual(payload["channel_id"], "-100555")
                self.assertEqual(payload["studio"], "Readded Studio")

                payload["overrides_path"] = self.overlay_path
                payload["config_path"] = self.admin.CONFIG_PATH

                class MockConn:
                    def execute(self, sql, params=()):
                        class MockResult:
                            def fetchall(self): return []
                            def fetchone(self): return None
                        return MockResult()

                with self.assertRaises(ChannelRemovalStillActive):
                    execute_external_operation(
                        action, MockConn(), jid, payload, Path("/tmp"), None
                    )
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        result, committed = execute_external_operation(
            action, MockConn(), jid, payload, Path("/tmp"), None
        )
        self.assertTrue(committed)
        self.assertEqual(result["local_state"], "source_readded")
        self.assertEqual(result["channel_id"], "-100555")

        overlay = read_overrides(self.overlay_path)
        self.assertEqual(overlay["revision"], 2)
        tg = overlay["overrides"]["telegram"]
        self.assertNotIn("-100555", tg.get("removed_channels", []))
        self.assertIn("-100555", tg["channels"])
        self.assertEqual(tg["channels"]["-100555"]["studio"], "Readded Studio")
        self.assertTrue(tg["channels"]["-100555"]["enabled"])


REPO_ROOT = Path(__file__).resolve().parents[1]


class TestChannelRollbackAndReaddRevision(unittest.IsolatedAsyncioTestCase):
    """Rollback restores the pre-removal overrides; a queued re-add refuses a stale revision."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.prev_env = os.environ.get("ADMIN_SOURCE_OVERRIDES_PATH")
        os.environ["ADMIN_SOURCE_OVERRIDES_PATH"] = str(Path(self.temp_dir.name) / "overrides.json")
        self.addCleanup(self._restore_env)
        self.admin = make_admin()
        self.routes = {route.path: route.endpoint for route in admin_v2_sources.build_router(self.admin).routes}

    def _restore_env(self):
        if self.prev_env is None:
            os.environ.pop("ADMIN_SOURCE_OVERRIDES_PATH", None)
        else:
            os.environ["ADMIN_SOURCE_OVERRIDES_PATH"] = self.prev_env

    async def test_failed_enqueue_rollback_restores_channel_settings(self):
        existing = {"telegram": {"channels": {"-1001": {"studio": "Studio A", "enabled": True}}}}
        writes = []

        def mock_update(config_path, candidate, expected_revision, **kwargs):
            writes.append(copy.deepcopy(candidate))
            return {"revision": expected_revision + 1}

        def failing_enqueue(db_url, action, payload):
            raise RuntimeError("DB connection failure")

        with patch("common.managed_sources.read_overrides",
                   return_value={"revision": 2, "overrides": copy.deepcopy(existing)}), \
             patch("common.managed_sources.read_pending_source_audit", return_value=None), \
             patch("common.managed_sources.preview_overrides", return_value={"changes": [], "truncated": False}), \
             patch("common.managed_sources.update_overrides", side_effect=mock_update), \
             patch("common.job_queue.enqueue_job", side_effect=failing_enqueue), \
             patch.object(admin_v2_sources, "is_channel_removal_active", return_value=False):
            req = make_post_request("/api/v2/sources/remove", {"expected_revision": 2, "channel_id": "-1001"})
            resp = await self.routes["/api/v2/sources/remove"](req)

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(len(writes), 2)
        # The staged write removes the channel entry and marks it removed.
        self.assertEqual(writes[0]["telegram"]["removed_channels"], ["-1001"])
        self.assertNotIn("-1001", writes[0]["telegram"]["channels"])
        # The rollback writes the pre-removal overrides, including the channel settings.
        self.assertEqual(writes[1]["telegram"]["channels"], existing["telegram"]["channels"])
        self.assertNotIn("removed_channels", writes[1]["telegram"])


class TestSourceReaddBaseRevision(unittest.TestCase):
    """The queued save or restore applies only when the config revision still matches the request."""

    def _payload(self, **extra):
        payload = {
            "channel_id": "-1001",
            "studio": "Studio B",
            "action_type": "save",
            "config_path": str(REPO_ROOT / "tests" / "fixtures" / "config_telegram_only.yaml"),
            "overrides": {"telegram": {"channels": {"-1002": {"studio": "Other", "enabled": True}}}},
        }
        payload.update(extra)
        return payload

    def _patches(self, current_revision):
        from contextlib import ExitStack
        update = MagicMock(return_value={"revision": current_revision + 1})
        stack = ExitStack()
        stack.enter_context(patch("common.managed_sources.is_channel_locked", return_value=False))
        stack.enter_context(patch("common.managed_sources.read_overrides",
                                  return_value={"revision": current_revision, "overrides": {}}))
        stack.enter_context(patch("common.managed_sources.preview_overrides",
                                  return_value={"changes": [], "truncated": False}))
        stack.enter_context(patch("common.managed_sources.update_overrides", update))
        return stack, update

    def _run(self, payload):
        from sync.duplicate_job_external_handlers import _action_source_readd
        conn = MagicMock()
        conn.execute.return_value.fetchall.return_value = []
        return _action_source_readd(conn, "job-1", payload, None, None)

    def test_stale_base_revision_is_rejected_without_writing(self):
        stack, update = self._patches(current_revision=5)
        with stack, self.assertRaises(ManagedConfigConflict):
            self._run(self._payload(base_revision=4))
        update.assert_not_called()

    def test_matching_base_revision_applies_the_update(self):
        stack, update = self._patches(current_revision=5)
        with stack:
            result, _ = self._run(self._payload(base_revision=5))
        self.assertEqual(result["local_state"], "source_readded")
        update.assert_called_once()

    def test_job_without_base_revision_keeps_previous_behaviour(self):
        stack, update = self._patches(current_revision=5)
        with stack:
            result, _ = self._run(self._payload())
        self.assertEqual(result["local_state"], "source_readded")
        update.assert_called_once()


if __name__ == "__main__":
    unittest.main()
