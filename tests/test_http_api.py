"""Request-level checks for critical resolver routes (no sockets or network)."""

import asyncio
from contextlib import ExitStack
import json
import os
import sys
import tempfile
import re
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from pg_helper import TEST_URL, reset_db
from test_support import load_flat_sync_module

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resolver"))
sys.path.insert(0, str(ROOT))

import admin as admin_module
import app as resolver_app
from common import db
from common.job_queue import read_job


BAD_URL = "postgresql://127.0.0.1:1/missing"


def get_job(db_url, job_id):
    """Read one durable job for assertions (payload and result decoded)."""
    row = read_job(db_url, job_id)
    if row is None:
        return None
    payload = row.get("payload")
    result = row.get("result")
    row["payload"] = json.loads(payload) if isinstance(payload, str) else (payload or {})
    row["result"] = json.loads(result) if isinstance(result, str) else result
    return row


class TestResolverHttpApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pg_helper import init_test_db
        initial_connection = init_test_db()
        initial_connection.close()

    def setUp(self):
        reset_db()

    def _job_count(self):
        conn = db.connect(TEST_URL)
        try:
            return conn.execute("SELECT count(*) FROM video_duplicate_jobs").fetchone()[0]
        finally:
            conn.close()
    def request(self, method, url, *, base_url="http://media.example.org", **kwargs):
        async def send():
            transport = httpx.ASGITransport(app=resolver_app.app)
            async with httpx.AsyncClient(
                transport=transport, base_url=base_url
            ) as client:
                # Some sandbox runtimes do not wake the selector on worker-thread
                # completion. A bounded timer also keeps HTTP failures from hanging CI.
                request = asyncio.create_task(client.request(method, url, **kwargs))
                # Source access checks may legitimately wait up to 15 seconds
                # in the route; leave room for that bounded API timeout.
                deadline = asyncio.get_running_loop().time() + 20
                while not request.done():
                    if asyncio.get_running_loop().time() >= deadline:
                        request.cancel()
                        raise TimeoutError(f"ASGI request timed out: {method} {url}")
                    await asyncio.sleep(0.01)
                return await request

        async def run():
            async def heartbeat():
                while True:
                    await asyncio.sleep(0.01)
            timer = asyncio.create_task(heartbeat())
            try:
                return await send()
            finally:
                # Keep the selector awake while asyncio joins worker threads too.
                await asyncio.get_running_loop().shutdown_default_executor()
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)

        return asyncio.run(run())

    def test_public_host_blocks_admin_json_but_keeps_health_route(self):
        with patch.object(admin_module, "ADMIN_PUBLIC", False), \
             patch.object(admin_module, "ENABLE_ADMIN", True), \
             patch.object(admin_module, "PUBLIC_DOMAIN", "media.example.org"):
            status = self.request("GET", "/admin/api/v2/status")
            health = self.request("GET", "/admin/health")

        self.assertEqual(status.status_code, 403)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["version"], resolver_app.APP_VERSION)

    def test_admin_page_loads_extracted_html_file(self):
        with patch.object(admin_module, "ADMIN_PUBLIC", False), \
             patch.object(admin_module, "ENABLE_ADMIN", True):
            response = self.request("GET", "/admin/", base_url="http://127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Adult Hub — Admin", response.text)
        self.assertIn('href="/admin/assets-v2/admin-v2.css"', response.text)
        self.assertNotIn('/admin/assets/admin.css', response.text)

    def test_admin_v2_page_exposes_quarantine_plan_controls(self):
        with patch.object(admin_module, "ADMIN_PUBLIC", False), \
             patch.object(admin_module, "ENABLE_ADMIN", True):
            response = self.request("GET", "/admin/v2/", base_url="http://127.0.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Review quarantine", response.text)
        self.assertIn('id="adminAccessMode"', response.text)
        self.assertIn('id="activeTaskCount"', response.text)
        self.assertIn('/admin/assets-v2/header-status.js', response.text)
        self.assertIn('id="operationDialog"', response.text)
        self.assertIn('id="bulkSelectionCount"', response.text)
        self.assertIn('id="checkAvailabilityBulk"', response.text)
        self.assertIn('id="quarantineExport"', response.text)
        self.assertIn('id="qualityIssuesPane"', response.text)
        self.assertIn('id="systemBackupView"', response.text)
        self.assertIn('id="systemDiagnosticsView"', response.text)
        self.assertIn('id="systemMaintenanceView"', response.text)
        self.assertNotIn('id="systemSyncActivityView"', response.text)
        self.assertIn('data-system-tab="maintenance"', response.text)
        self.assertIn('id="systemActivityView"', response.text)
        self.assertIn('/admin/assets-v2/activity.js', response.text)
        self.assertIn('/admin/assets-v2/maintenance.js', response.text)
        self.assertIn('/admin/assets-v2/linkcheck.js', response.text)
        self.assertIn("Operation history", response.text)
        source_script = self.request("GET", "/admin/assets-v2/sources.js", base_url="http://127.0.0.1")
        self.assertEqual(source_script.status_code, 200, source_script.text)
        self.assertIn("const SOURCE_CHECK_URL = `${V2_API}/sources/check`;", source_script.text)
        maintenance_script = self.request("GET", "/admin/assets-v2/maintenance.js", base_url="http://127.0.0.1")
        self.assertEqual(maintenance_script.status_code, 200, maintenance_script.text)
        linkcheck_script = self.request("GET", "/admin/assets-v2/linkcheck.js", base_url="http://127.0.0.1")
        self.assertEqual(linkcheck_script.status_code, 200, linkcheck_script.text)
        self.assertIn("/admin/api/v2/linkcheck", linkcheck_script.text)
        activity_script = self.request("GET", "/admin/assets-v2/sync-activity.js", base_url="http://127.0.0.1")
        self.assertEqual(activity_script.status_code, 200, activity_script.text)
        self.assertIn("/admin/api/v2/sync/activity", activity_script.text)
        self.assertIn("`${api}/sync`", maintenance_script.text)
        quarantine_script = self.request("GET", "/admin/assets-v2/quarantine.js", base_url="http://127.0.0.1")
        self.assertEqual(quarantine_script.status_code, 200, quarantine_script.text)
        self.assertIn("organize/quarantine", quarantine_script.text)

    def test_admin_v2_overview_and_log_routes_are_registered_and_redacted(self):
        fixture_log = {
            "path": "/private/activity.log", "exists": True,
            "mtime_human": "2026-10-06 12:00",
            "tail": [
                "2026-10-06 12:00:00,001 [INFO] Starting Adult-Hub sync (deduplicated)...",
                "2026-10-06 12:00:01,001 [ERROR] Telegram sync failed for telegram:-100secret: token=hidden",
            ],
        }
        with patch.object(admin_module, "ADMIN_PUBLIC", False), \
             patch.object(admin_module, "ENABLE_ADMIN", True), \
             patch.object(admin_module, "_db_stats", return_value={"db_ok": True, "db_path": "/private/db", "videos_total": 3}), \
             patch.object(admin_module, "_sync_status", return_value={"running": True, "status_text": "secret token=hidden"}), \
             patch.object(admin_module, "_attention_summary", return_value={"pending_tasks": 1}), \
             patch.object(admin_module, "_sync_activity_tail", return_value=fixture_log):
            status = self.request("GET", "/admin/api/v2/status", base_url="http://127.0.0.1")
            activity = self.request("GET", "/admin/api/v2/sync/activity", base_url="http://127.0.0.1")

        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["db"]["videos_total"], 3)
        self.assertNotIn("db_path", status.json()["db"])
        self.assertEqual(status.json()["sync"]["status_text"], "Sync in progress")
        self.assertEqual(activity.status_code, 200, activity.text)
        self.assertEqual(activity.json()["entries"][0]["message"], "Sync started.")
        self.assertNotIn("telegram:-100secret", activity.text)
        self.assertNotIn("token=hidden", activity.text)

    def test_admin_v2_activity_records_accepted_action_without_payload(self):
        with self.admin_config(), patch.object(admin_module, "HUB_DB_URL", TEST_URL):
            queued = self.request(
                "POST", "/admin/api/v2/audits/execute", base_url="http://localhost",
                headers={"Authorization": "Bearer test-write-token", "Origin": "http://localhost"},
                json={"mode": "full"},
            )
            activity = self.request("GET", "/admin/api/v2/activity?limit=10", base_url="http://localhost")
            filtered = self.request(
                "GET", "/admin/api/v2/activity?method=POST&outcome=accepted&area=quality",
                base_url="http://localhost",
            )
            invalid_filter = self.request(
                "GET", "/admin/api/v2/activity?area=../../", base_url="http://localhost"
            )

        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(activity.status_code, 200, activity.text)
        self.assertEqual(filtered.status_code, 200, filtered.text)
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(invalid_filter.status_code, 400)
        item = activity.json()["items"][0]
        self.assertEqual(item["route"], "/admin/api/v2/audits/execute")
        self.assertEqual(item["method"], "POST")
        self.assertEqual(item["outcome"], "accepted")
        self.assertEqual(item["status_code"], 202)
        self.assertEqual(item["actor_type"], "shared_admin")
        self.assertNotIn("body", item)
        self.assertNotIn("payload", item)

    def test_admin_v2_activity_changes_exposes_persisted_metadata_diff(self):
        from common.admin_audit_events import record_metadata_change
        with self.admin_config(), patch.object(admin_module, "HUB_DB_URL", TEST_URL):
            with db.connect(TEST_URL) as conn:
                conn.execute(
                    "INSERT INTO video_duplicate_jobs (id,action,state,payload,created_at,updated_at) "
                    "VALUES ('activity-audit-job','metadata_update','done','{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                )
                record_metadata_change(
                    conn, job_id="activity-audit-job", video_id="telegram:99:activity-item",
                    before={"title": "Source title"}, after={"title": "Operator title"},
                    before_overrides={}, after_overrides={"title": "Operator title"},
                    fields=["title"],
                )
            response = self.request(
                "GET", "/admin/api/v2/activity/changes?limit=10&event_type=metadata_update&object_type=video&q=activity-item",
                base_url="http://localhost"
            )
        self.assertEqual(response.status_code, 200, response.text)
        event = response.json()["items"][0]
        self.assertEqual(event["event_type"], "metadata_update")
        self.assertEqual(event["changes"]["title"], {
            "before": "Source title", "after": "Operator title",
            "before_overridden": False, "after_overridden": True,
        })

    def test_admin_v2_errors_have_stable_envelope_and_correlation_header(self):
        with self.admin_config():
            invalid = self.request(
                "GET", "/admin/api/v2/jobs?state=not-a-state", base_url="http://localhost"
            )
            framework_invalid = self.request(
                "GET", "/admin/api/v2/jobs?limit=0", base_url="http://localhost"
            )
            denied = self.request(
                "GET", "/admin/api/v2/jobs", base_url="http://media.example.org"
            )
            with patch.object(admin_module, "_backup_status", side_effect=RuntimeError("private detail"), create=True):
                unavailable = self.request(
                    "GET", "/admin/api/v2/backups", base_url="http://localhost"
                )

        for response, status, code, retryable in (
            (invalid, 400, "invalid_request", False),
            (framework_invalid, 400, "invalid_request", False),
            (denied, 403, "forbidden", False),
            (unavailable, 503, "unavailable", True),
        ):
            self.assertEqual(response.status_code, status, response.text)
            error = response.json()["error"]
            self.assertEqual(set(error), {"code", "message", "retryable", "correlation_id"})
            self.assertEqual(error["code"], code)
            self.assertIs(error["retryable"], retryable)
            self.assertTrue(error["message"])
            self.assertEqual(response.headers["X-Correlation-ID"], error["correlation_id"])
            self.assertEqual(response.headers.get("cache-control"), "no-store")
            self.assertNotIn("private detail", response.text)

    def test_http_range_with_unknown_size_streams_full_resource_as_200(self):
        calls = []

        class FakeTelegram:
            async def resolve_stream(self, ref):
                return "tg://fake-reference"

            async def file_size(self, ref):
                return None

            async def download_range(self, ref, offset, length):
                calls.append((ref, offset, length))
                yield b"full-"
                yield b"video"

        with patch.object(resolver_app, "get_provider", return_value=FakeTelegram()):
            response = self.request(
                "GET", "/admin/play/telegram:fake-id/stream",
                headers={"Range": "bytes=100-199"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"full-video")
        self.assertEqual(calls, [("fake-reference", 0, None)])


    MUTATIONS = (
        ("POST", "/admin/api/v2/maintenance/cache/clear"),
        ("POST", "/admin/api/v2/operations/preview"),
        ("POST", "/admin/api/v2/operations/execute"),
        ("POST", "/admin/api/v2/duplicates/actions"),
        ("POST", "/admin/api/v2/duplicates/visual/verify"),
        ("POST", "/admin/api/v2/jobs/{job_id}/retry"),
        ("POST", "/admin/api/v2/jobs/{job_id}/cancel"),
        ("POST", "/admin/api/v2/metadata/execute"),
        ("POST", "/admin/api/v2/metadata/enrichment/scan"),
        ("POST", "/admin/api/v2/sources"),
        ("POST", "/admin/api/v2/sources/save"),
        ("POST", "/admin/api/v2/sources/check"),
        ("POST", "/admin/api/v2/availability/execute"),
        ("POST", "/admin/api/v2/audits/execute"),
        ("POST", "/admin/api/v2/audits/{job_id}/repairs/preview"),
        ("POST", "/admin/api/v2/audits/{job_id}/reimports/preview"),
        ("POST", "/admin/api/v2/backups/create"),
        ("POST", "/admin/api/v2/backups/restore-test"),
        ("POST", "/admin/api/v2/schedules"),
        ("POST", "/admin/api/v2/schedules/{schedule_id}/enabled"),
        ("POST", "/admin/api/v2/schedules/{schedule_id}/cron"),
        ("POST", "/admin/api/v2/schedules/{schedule_id}/run"),
        ("POST", "/admin/api/v2/schedules/system/{schedule_id}/run"),
        ("POST", "/admin/api/v2/schedules/system/{schedule_id}"),
        ("POST", "/admin/api/v2/diagnostics/playback/enable"),
        ("POST", "/admin/api/v2/diagnostics/playback/disable"),
        ("DELETE", "/admin/api/v2/diagnostics/playback/reports"),
        ("POST", "/admin/api/v2/quality/acknowledge"),
        ("POST", "/admin/api/v2/quality/reopen"),
        ("POST", "/admin/api/v2/quality/proposals/{proposal_id}/reject"),
        ("POST", "/admin/api/v2/quality/proposals/{proposal_id}/refresh"),
        ("POST", "/admin/api/v2/maintenance/cache/clear"),
        ("POST", "/admin/api/v2/maintenance/sync"),
        ("POST", "/admin/api/v2/maintenance/jellyfin/refresh"),
        ("POST", "/admin/api/v2/maintenance/collections/refresh"),
        ("POST", "/admin/api/v2/performers/proposals/{proposal_id}"),
        ("POST", "/admin/api/v2/performers/proposals/reconcile"),
        ("POST", "/admin/api/v2/performers/proposal-groups/{reason}/{key}"),
        ("PUT", "/admin/api/v2/organize/people/{name}/profile"),
        ("POST", "/admin/api/v2/library/videos/{video_id}/generate-artwork"),
        ("POST", "/admin/api/v2/library/videos/{video_id}/download-artwork"),
        ("POST", "/admin/api/v2/library/videos/{video_id}/generate-phash"),
    )
    CLIENT_INTEGRATION_MUTATIONS = {
        ("POST", "/admin/api/v2/video-phash/frames"),
        ("POST", "/admin/api/v2/video-phash/generate"),
        ("POST", "/admin/api/v2/phash-jobs/{job_id}/retry"),
        ("POST", "/admin/api/v2/phash-jobs/{job_id}/cancel"),
    }

    def admin_config(self, *, token="test-write-token", public=False, legacy="", worker_actions=None):
        stack = ExitStack()
        worker_dir = tempfile.TemporaryDirectory()
        stack.callback(worker_dir.cleanup)
        worker_path = Path(worker_dir.name) / "worker-capabilities.json"
        from common.job_queue import ALL_ACTIONS
        from common.worker_capabilities import publish_worker_capabilities
        publish_worker_capabilities(
            worker_path, version="test-worker",
            actions=ALL_ACTIONS if worker_actions is None else worker_actions,
        )
        stack.enter_context(patch.object(admin_module, "WORKER_CAPABILITIES_FILE", worker_path, create=True))
        for name, value in {"ADMIN_WRITE_TOKEN": token, "ADMIN_DUPLICATE_TOKEN": legacy,
                            "ADMIN_PUBLIC": public, "ENABLE_ADMIN": True,
                            "PUBLIC_DOMAIN": "media.example.org"}.items():
            stack.enter_context(patch.object(admin_module, name, value, create=True))
        return stack

    def test_every_admin_mutation_rejects_missing_authorization_before_parsing_body(self):
        with self.admin_config():
            for method, path in self.MUTATIONS:
                with self.subTest(path=path):
                    request_path = path.replace("{proposal_id}", "1").replace("{video_id}", "telegram:123")
                    response = self.request(method, request_path, base_url="http://localhost",
                                            content=b"invalid json",
                                            headers={"Origin": "http://localhost"})
                    self.assertEqual(response.status_code, 403, response.text)

    def test_every_admin_mutation_requires_origin_without_token(self):
        with self.admin_config(token="", public=True):
            for method, path in self.MUTATIONS:
                with self.subTest(path=path):
                    request_path = path.replace("{proposal_id}", "1").replace("{video_id}", "telegram:123")
                    response = self.request(method, request_path, base_url="https://media.example.org")
                    self.assertEqual(response.status_code, 403, response.text)

    def test_empty_token_allows_same_origin_writes_locally_and_publicly(self):
        for public, base in ((False, "http://localhost"), (True, "https://media.example.org")):
            with self.subTest(public=public), tempfile.TemporaryDirectory() as directory, self.admin_config(token="", public=public):
                root = Path(directory)
                clear = Mock(return_value=3)
                with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                     patch.object(admin_module, "SYNC_TRIGGER_FILE", root / "sync.trigger"), \
                     patch.object(resolver_app.app.state, "admin_cache_clear", clear):
                    cached = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url=base,
                        headers={"Origin": base})
                self.assertEqual(cached.status_code, 200, cached.text)
                clear.assert_called_once_with()

    def test_public_mode_requires_configured_token_and_accepts_valid_credentials(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config(public=True):
            root = Path(directory)
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "SYNC_TRIGGER_FILE", root / "sync.trigger"):
                body = {}
                for supplied in (None, "Bearer incorrect"):
                    headers = {"Origin": "https://media.example.org"}
                    if supplied:
                        headers["Authorization"] = supplied
                    denied = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="https://media.example.org", headers=headers, json=body)
                    self.assertEqual(denied.status_code, 403)
                self.assertEqual(self._job_count(), 0)
                allowed = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="https://media.example.org",
                    headers={"Origin": "https://media.example.org", "Authorization": "Bearer test-write-token"}, json=body)
                self.assertEqual(allowed.status_code, 200, allowed.text)

    def test_no_token_mode_preserves_admin_enable_and_public_host_gates(self):
        for enabled, public in ((False, True), (True, False)):
            with self.subTest(enabled=enabled), self.admin_config(token="", public=public), \
                 patch.object(admin_module, "ENABLE_ADMIN", enabled):
                clear = Mock(return_value=3)
                with patch.object(resolver_app.app.state, "admin_cache_clear", clear):
                    response = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="https://media.example.org", headers={"Origin": "https://media.example.org"})
                self.assertEqual(response.status_code, 403)
                clear.assert_not_called()

    def test_origin_must_match_exact_host_and_port(self):
        origins = (None, "null", "https://attacker.example", "http://localhost",
                   "http://localhost:8080.attacker.example", "ftp://localhost:8080")
        for token in ("", "test-write-token"):
            with self.admin_config(token=token, public=True):
                for origin in origins:
                    with self.subTest(origin=origin):
                        headers = {"Authorization": "Bearer test-write-token"}
                        if origin is not None:
                            headers["Origin"] = origin
                        response = self.request("POST", "/admin/api/v2/maintenance/cache/clear",
                            base_url="http://localhost:8080", headers=headers, json={"merges": []})
                        self.assertEqual(response.status_code, 403, response.text)

    def test_forwarded_host_cannot_authorize_foreign_origin(self):
        with self.admin_config(), patch.object(resolver_app.app.state, "admin_cache_clear", Mock(return_value=0)):
            response = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="http://localhost",
                headers={"Origin": "http://attacker.example", "X-Forwarded-Host": "attacker.example",
                         "Authorization": "Bearer test-write-token"})
        self.assertEqual(response.status_code, 403, response.text)

    def test_legacy_token_fallback_and_new_token_precedence(self):
        for primary, supplied, expected in (("", "legacy", 200),
                                            ("primary", "legacy", 403),
                                            ("primary", "primary", 200)):
            with self.subTest(primary=primary, supplied=supplied), self.admin_config(token=primary, legacy="legacy"):
                response = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": f"Bearer {supplied}"})
                self.assertEqual(response.status_code, expected, response.text)

    def test_mutation_matrix_covers_all_admin_router_writes(self):
        # The OpenAPI schema flattens routers included by newer FastAPI versions.
        schema = resolver_app.app.openapi()
        routes = {(method.upper(), path)
                  for path, operations in schema.get("paths", {}).items()
                  for method in operations
                  if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and path.startswith("/admin/")}
        # Preview is a POST for a structured diff, but deliberately read-only
        # and does not belong to the mutation authorization matrix.
        routes.discard(("POST", "/admin/api/v2/metadata/preview"))
        routes.discard(("POST", "/admin/api/v2/availability/preview"))
        routes.discard(("POST", "/admin/api/v2/sources/preview"))
        self.assertTrue(self.CLIENT_INTEGRATION_MUTATIONS <= routes)
        routes -= self.CLIENT_INTEGRATION_MUTATIONS
        self.assertEqual(routes, set(self.MUTATIONS))

    def test_client_phash_intake_is_disabled_by_default_and_does_not_accept_admin_token(self):
        path = "/admin/api/v2/video-phash/frames"
        with self.admin_config(), patch.dict(os.environ, {"PHASH_UPLOAD_ENABLED": "false"}):
            disabled = self.request(
                "POST", path, base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={},
            )
        self.assertEqual(disabled.status_code, 404)

        with self.admin_config(), patch.dict(os.environ, {
            "PHASH_UPLOAD_ENABLED": "true", "JELLYFIN_URL": "http://jellyfin.invalid",
        }):
            admin_token_only = self.request(
                "POST", path, base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={},
            )
        self.assertEqual(admin_token_only.status_code, 401)

    def test_performer_proposal_id_is_validated_after_write_authorization(self):
        path = "/admin/api/v2/performers/proposals/not-an-id"
        with self.admin_config():
            denied = self.request("POST", path, base_url="http://localhost",
                                  headers={"Origin": "http://localhost"}, json={"action": "reject"})
            allowed = self.request("POST", path, base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={"action": "reject"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 400)
        self.assertIn("positive integer", allowed.json()["error"]["message"])

    def test_authorized_cache_clear_executes_once_and_denied_request_has_no_effect(self):
        clear = Mock(return_value=7)
        with self.admin_config(), patch.object(resolver_app.app.state, "admin_cache_clear", clear):
            denied = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="http://localhost",
                                  headers={"Origin": "http://localhost"})
            clear.assert_not_called()
            allowed = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["cleared"], 7)
        clear.assert_called_once_with()

    def test_capabilities_disclose_optional_token_and_never_expose_its_value(self):
        for token, public in (("", False), ("", True), ("test-write-token", True), ("test-write-token", False)):
            with self.subTest(public=public, token=bool(token)), self.admin_config(token=token, public=public):
                response = self.request("GET", "/admin/api/v2/capabilities", base_url="http://localhost")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()["actions_enabled"])
                self.assertFalse(response.json()["read_only"])
                self.assertIsNone(response.json()["read_only_reason"])
                self.assertEqual(response.json()["token_required"], bool(token))
                self.assertTrue(response.json()["worker_ready"])
                self.assertEqual(response.json()["worker"]["missing_actions"], [])
                status = self.request("GET", "/admin/api/v2/status", base_url="http://localhost")
                self.assertEqual(status.json()["capabilities"]["token_required"], bool(token))
                self.assertNotIn("test-write-token", response.text)


    def test_worker_capability_snapshot_gates_admin_writes_when_missing(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            missing_snapshot = Path(directory) / "worker-capabilities.json"
            with patch.object(admin_module, "WORKER_CAPABILITIES_FILE", missing_snapshot):
                capabilities = self.request("GET", "/admin/api/v2/capabilities", base_url="http://localhost")
                denied = self.request(
                    "POST", "/admin/api/v2/audits/execute", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                )
        self.assertEqual(capabilities.status_code, 200)
        self.assertFalse(capabilities.json()["worker_ready"])
        self.assertEqual(capabilities.json()["read_only_reason"], "worker_unavailable")
        self.assertEqual(denied.status_code, 503)
        self.assertEqual(denied.json()["code"], "worker_unavailable")
        self.assertEqual(self._job_count(), 0)

    def test_partial_worker_action_list_keeps_panel_read_only(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            partial_snapshot = Path(directory) / "worker-capabilities.json"
            from common.worker_capabilities import publish_worker_capabilities
            publish_worker_capabilities(partial_snapshot, version="old-worker", actions=["sync"])
            with patch.object(admin_module, "WORKER_CAPABILITIES_FILE", partial_snapshot):
                capabilities = self.request("GET", "/admin/api/v2/capabilities", base_url="http://localhost")
                denied = self.request(
                    "POST", "/admin/api/v2/audits/execute", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                )
        self.assertEqual(capabilities.status_code, 200)
        self.assertFalse(capabilities.json()["worker_ready"])
        self.assertEqual(capabilities.json()["read_only_reason"], "worker_incompatible")
        self.assertIn("availability_check", capabilities.json()["worker"]["missing_actions"])
        self.assertEqual(denied.status_code, 503)
        self.assertEqual(denied.json()["code"], "worker_incompatible")
        self.assertEqual(self._job_count(), 0)



    def test_admin_page_referenced_assets_are_served_with_correct_types(self):
        with self.admin_config():
            page = self.request("GET", "/admin/", base_url="http://localhost")
            paths = re.findall(r'(?:src|href)="(/admin/assets(?:-v2)?/[^" ]+)"', page.text)
            self.assertTrue(paths)
            self.assertTrue(any(path.endswith(".css") for path in paths))
            self.assertTrue(any(path.endswith(".js") for path in paths))
            for path in paths:
                with self.subTest(path=path):
                    asset = self.request("GET", path, base_url="http://localhost")
                    self.assertEqual(asset.status_code, 200, asset.text[:100])
                    self.assertTrue(asset.content)
                    expected = "text/css" if path.endswith(".css") else "javascript"
                    self.assertIn(expected, asset.headers["content-type"])




    def test_admin_v2_jobs_return_active_count_or_explicit_unknown(self):
        from common.job_queue import enqueue_job

        enqueue_job(TEST_URL, "sync", {})
        with self.admin_config(), patch.object(admin_module, "HUB_DB_URL", TEST_URL):
            available = self.request(
                "GET", "/admin/api/v2/jobs?state=active&limit=1", base_url="http://localhost"
            )
        self.assertEqual(available.status_code, 200, available.text)
        self.assertTrue(available.json()["data"]["available"])
        self.assertEqual(available.json()["active_total"], 1)

        with self.admin_config(), patch.object(admin_module, "HUB_DB_URL", BAD_URL):
            unavailable = self.request(
                "GET", "/admin/api/v2/jobs?state=active&limit=1", base_url="http://localhost"
            )
        self.assertEqual(unavailable.status_code, 200, unavailable.text)
        self.assertFalse(unavailable.json()["data"]["available"])
        self.assertIsNone(unavailable.json()["active_total"])

    def test_admin_v2_unhandled_route_errors_use_redacted_envelope(self):
        async def unexpected_failure():
            raise RuntimeError("private source /srv/library token=secret")

        from admin_v2_errors import V2ErrorRoute
        route_class = resolver_app.app.router.route_class
        resolver_app.app.router.route_class = V2ErrorRoute
        try:
            resolver_app.app.add_api_route(
                "/admin/api/v2/test/http-unexpected-failure", unexpected_failure,
                methods=["GET"], include_in_schema=False,
            )
        finally:
            resolver_app.app.router.route_class = route_class
        route = next(route for route in resolver_app.app.router.routes
                     if getattr(route, "path", "").endswith("/test/http-unexpected-failure"))
        try:
            with self.admin_config():
                async def send():
                    transport = httpx.ASGITransport(app=resolver_app.app, raise_app_exceptions=False)
                    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
                        return await client.get("/admin/api/v2/test/http-unexpected-failure")
                response = asyncio.run(send())
        finally:
            resolver_app.app.router.routes.remove(route)

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "request_failed")
        self.assertEqual(response.json()["error"]["message"], "The request could not be completed.")
        self.assertTrue(response.json()["error"]["retryable"])
        self.assertEqual(response.headers["X-Correlation-ID"], response.json()["error"]["correlation_id"])
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertNotIn("private source", response.text)
        self.assertNotIn("secret", response.text)







    def test_v2_quarantine_plan_preview_execute_and_replay(self):
        with tempfile.TemporaryDirectory(), self.admin_config():
            conn = db.connect(TEST_URL)
            with conn:
                conn.execute("INSERT INTO videos (id,provider,title,performers,categories,tags) VALUES ('telegram:99:plan','telegram','Plan video','[]','[]','[]')")
            conn.close()
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
            conn = db.connect(TEST_URL)
            with conn:
                conn.execute(
                    "INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot) "
                    "VALUES ('telegram:99:restore','keeper',%s,'active',%s,%s)",
                    (operation_id, int(time.time()), json.dumps(snapshot)),
                )
            conn.close()
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
        sync = load_flat_sync_module()

        keeper, loser = "telegram:99:keeper", "telegram:99:hide"
        with tempfile.TemporaryDirectory(prefix="adult_hub_v2_hide_undo_") as directory, self.admin_config():
            root = Path(directory)
            media = root / "library"
            media.mkdir()
            with db.connect(TEST_URL) as conn:
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
                 patch.dict("os.environ", {"DATABASE_URL": TEST_URL}):
                with db.connect(TEST_URL) as conn:
                    self.assertEqual(sync.process_video_duplicate_jobs(conn, media), 1)
            refresh.assert_called_once_with()
            with db.connect(TEST_URL) as conn:
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
                 patch.dict("os.environ", {"DATABASE_URL": TEST_URL}):
                with db.connect(TEST_URL) as conn:
                    self.assertEqual(sync.process_video_duplicate_jobs(conn, media), 1)
            with db.connect(TEST_URL) as conn:
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
            with db.connect(TEST_URL) as conn:
                conn.execute(
                    "INSERT INTO videos (id,provider,title,performers,categories,tags) "
                    "VALUES ('telegram:99:metadata-plan','telegram','Source title','[]','[]','[]')"
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
        sync = load_flat_sync_module()

        keeper = "telegram:-1001:10:exactplanfid"
        loser = "telegram:-1002:20:exactplanfid"
        with tempfile.TemporaryDirectory(prefix="adult_hub_v2_exact_merge_") as directory, self.admin_config():
            root = Path(directory)
            media = root / "library"
            media.mkdir()
            with db.connect(TEST_URL) as conn:
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
                 patch.dict("os.environ", {"DATABASE_URL": TEST_URL}):
                with db.connect(TEST_URL) as conn:
                    self.assertEqual(sync.process_video_duplicate_jobs(conn, media), 1)
            refresh.assert_called_once_with()
            with db.connect(TEST_URL) as conn:
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
            with db.connect(TEST_URL) as conn, conn:
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
                with db.connect(TEST_URL) as conn:
                    with conn:
                        conn.execute("UPDATE videos SET title='Changed after preview' WHERE id=%s", (loser,))
                before = self._job_count()
                execute = self.request("POST", "/admin/api/v2/operations/execute", base_url="http://localhost",
                                       headers=headers, json={"plan_id": preview.json()["plan_id"]})
            with db.connect(TEST_URL) as conn:
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
            with db.connect(TEST_URL) as conn:
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

    def test_v2_people_catalog_paginates_and_exposes_safe_filmography(self):
        with tempfile.TemporaryDirectory(), self.admin_config():
            conn = db.connect(TEST_URL)
            with conn:
                conn.execute(
                    "INSERT INTO videos (id,provider,title,performers,categories,tags,studio) "
                    "VALUES ('telegram:99:actor','telegram','Actor scene','[\"Alex Actor\"]','[]','[]','Studio')"
                )
                conn.execute("INSERT INTO performer_bios (name,bio,aliases) VALUES ('Alex Actor','Biography',%s)",
                             (json.dumps(["Alexandra Stage"]),))
                conn.execute("INSERT INTO performer_aliases (alias,canonical) VALUES ('Alex A','Alex Actor')")
                conn.execute("INSERT INTO performer_photos (name,thumb) VALUES ('Alex Actor','https://example.org/private.jpg')")
                conn.execute(
                    "INSERT INTO video_duplicate_decisions (loser_id,keeper_id,operation_id,state,created_at,snapshot) "
                    "VALUES ('telegram:99:actor','telegram:99:actor','operation-safe','active',1,%s)",
                    (json.dumps({"video": {"title": "Actor scene", "provider": "telegram"}, "files": ["private/path"]}),)
                )
            conn.close()
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                page = self.request("GET", "/admin/api/v2/organize/people?q=Alex&limit=10", base_url="http://localhost")
                bio_alias_search = self.request("GET", "/admin/api/v2/organize/people?q=Alexandra%20Stage", base_url="http://localhost")
                mapped_alias_search = self.request("GET", "/admin/api/v2/organize/people?q=Alex%20A", base_url="http://localhost")
                detail = self.request("GET", "/admin/api/v2/organize/people/Alex%20Actor", base_url="http://localhost")
                quarantine = self.request("GET", "/admin/api/v2/organize/quarantine?limit=10", base_url="http://localhost")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual(page.json()["total"], 1)
        self.assertTrue(page.json()["items"][0]["has_bio"])
        self.assertTrue(page.json()["items"][0]["has_photo"])
        self.assertEqual(page.json()["items"][0]["video_count"], 1)
        self.assertEqual(page.json()["items"][0]["aliases"], ["Alexandra Stage", "Alex A"])
        for response in (bio_alias_search, mapped_alias_search):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total"], 1)
            self.assertEqual(response.json()["items"][0]["name"], "Alex Actor")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["filmography"][0]["id"], "telegram:99:actor")
        self.assertEqual(detail.json()["filmography"][0]["thumbnail"], None)
        self.assertEqual(detail.json()["total"], 1)
        self.assertEqual(detail.json()["offset"], 0)
        self.assertEqual(detail.json()["limit"], 50)
        self.assertEqual(quarantine.status_code, 200, quarantine.text)
        self.assertEqual(quarantine.json()["items"][0]["mode"], "quarantine")
        self.assertNotIn("private/path", quarantine.text)

    def test_v2_people_catalog_loads_mapped_aliases_once_per_page(self):
        """Aggregating aliases inside the grouped page query makes cost grow with OFFSET."""
        with tempfile.TemporaryDirectory(), self.admin_config():
            conn = db.connect(TEST_URL)
            with conn:
                for index in range(3):
                    name = f"Paginated Actor {index}"
                    conn.execute(
                        "INSERT INTO videos (id,provider,title,performers,categories,tags,studio) "
                        "VALUES (%s,'telegram','Scene','[]','[]','[]','Studio')",
                        (f"telegram:99:page-{index}",),
                    )
                    conn.execute(
                        "INSERT INTO video_performers (video_id,performer_name,position) VALUES (%s,%s,0)",
                        (f"telegram:99:page-{index}", name),
                    )
                    conn.execute("INSERT INTO performer_aliases (alias,canonical) VALUES (%s,%s)",
                                 (f"{name} alias", name))
            conn.close()

            executed = []
            real_connect = admin_module.admin_v2_organize.db.connect

            class RecordingConnection:
                def __init__(self, inner):
                    self._inner = inner

                def execute(self, sql, params=None):
                    executed.append(" ".join(str(sql).split()))
                    return self._inner.execute(sql, params)

                def close(self):
                    self._inner.close()

                def __getattr__(self, item):
                    return getattr(self._inner, item)

            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module.admin_v2_organize.db, "connect",
                              lambda url: RecordingConnection(real_connect(url))):
                page = self.request("GET", "/admin/api/v2/organize/people?limit=2&offset=1",
                                    base_url="http://localhost")

        self.assertEqual(page.status_code, 200, page.text)
        self.assertEqual([item["name"] for item in page.json()["items"]],
                         ["Paginated Actor 1", "Paginated Actor 2"])
        self.assertEqual(page.json()["items"][1]["aliases"], ["Paginated Actor 2 alias"],
                         "a later page carries its aliases like the first one")
        listing = [sql for sql in executed if "FROM video_performers p" in sql and "GROUP BY" in sql]
        self.assertEqual(len(listing), 1)
        self.assertNotIn("performer_aliases", listing[0],
                         "the page query must not aggregate aliases per row")
        self.assertEqual(len([sql for sql in executed if "jsonb_agg(pa.alias" in sql]), 1,
                         "one alias lookup covers the whole page")

    def test_v2_person_filmography_paginates_large_catalog_with_stable_order(self):
        with tempfile.TemporaryDirectory(), self.admin_config():
            with db.connect(TEST_URL) as conn:
                for index in range(105):
                    video_id = f"telegram:99:filmography-{index:03d}"
                    conn.execute(
                        "INSERT INTO videos (id,provider,title,performers,categories,tags,studio) "
                        "VALUES (%s,'telegram','Same title','[]','[]','[]','Studio')",
                        (video_id,),
                    )
                    conn.execute(
                        "INSERT INTO video_performers (video_id,performer_name,position) VALUES (%s,'Pagination Actor',0)",
                        (video_id,),
                    )
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                first = self.request("GET", "/admin/api/v2/organize/people/Pagination%20Actor?limit=50", base_url="http://localhost")
                second = self.request("GET", "/admin/api/v2/organize/people/Pagination%20Actor?offset=50&limit=50", base_url="http://localhost")
                last = self.request("GET", "/admin/api/v2/organize/people/Pagination%20Actor?offset=100&limit=50", base_url="http://localhost")
                invalid = self.request("GET", "/admin/api/v2/organize/people/Pagination%20Actor?offset=-1", base_url="http://localhost")
                too_large = self.request("GET", "/admin/api/v2/organize/people/Pagination%20Actor?limit=101", base_url="http://localhost")
        for response in (first, second, last):
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["total"], 105)
            self.assertEqual(response.json()["limit"], 50)
        self.assertEqual([len(first.json()["filmography"]), len(second.json()["filmography"]), len(last.json()["filmography"])], [50, 50, 5])
        ids = [video["id"] for response in (first, second, last) for video in response.json()["filmography"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, [f"telegram:99:filmography-{index:03d}" for index in range(105)])
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(too_large.status_code, 400)

    def test_v2_availability_preview_is_read_only_and_execute_is_authorized(self):
        sync = load_flat_sync_module()

        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            root = Path(directory)
            with db.connect(TEST_URL) as conn:
                conn.execute("INSERT INTO videos (id,provider,title,performers,categories,tags) VALUES ('v-check','test','Check me','[]','[]','[]')")
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
                with db.connect(TEST_URL) as conn:
                    processed = sync.process_video_duplicate_jobs(conn, root / "media")
            completed = get_job(TEST_URL, job["id"])
            with db.connect(TEST_URL) as evidence:
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

    def test_v2_consistency_audit_is_authorized_and_report_is_paginated(self):
        with tempfile.TemporaryDirectory(), self.admin_config():
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                denied = self.request("POST", "/admin/api/v2/audits/execute", base_url="http://localhost",
                                      headers={"Origin": "http://localhost"}, json={})
                allowed = self.request("POST", "/admin/api/v2/audits/execute", base_url="http://localhost",
                                       headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={})
                conn = db.connect(TEST_URL)
                try:
                    conn.execute("UPDATE video_duplicate_jobs SET result=%s WHERE id=%s",
                                 (json.dumps({"checked": 1, "total": 4,
                                              "jellyfin_import_state": "checked", "library_inventory_status": "partial",
                                              "library_inventory_entries": 50000, "raw_token": "secret"}),
                                  allowed.json()["job_id"]))
                    conn.commit()
                finally:
                    conn.close()
                history = self.request("GET", "/admin/api/v2/audits?limit=10&offset=0", base_url="http://localhost")
                report = self.request("GET", f"/admin/api/v2/audits/{allowed.json()['job_id']}?limit=10&offset=0",
                                      base_url="http://localhost")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 202, allowed.text)
        self.assertEqual(history.status_code, 200, history.text)
        history_item = next(item for item in history.json()["items"] if item["id"] == allowed.json()["job_id"])
        self.assertEqual(history_item["jellyfin_import_state"], "checked")
        self.assertEqual(history_item["library_inventory_status"], "partial")
        self.assertEqual(history_item["library_inventory_entries"], 50000)
        self.assertEqual(history_item["checked"], 1)
        self.assertEqual(history_item["catalog_total"], 4)
        self.assertEqual(len(allowed.json()["job_ids"]), 2)
        self.assertEqual({item["id"] for item in history.json()["items"]}, set(allowed.json()["job_ids"]))
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()["audit"]["jellyfin_import_state"], "checked")
        self.assertEqual(report.json()["audit"]["library_inventory_status"], "partial")
        self.assertEqual(report.json()["audit"]["library_inventory_entries"], 50000)
        self.assertEqual(report.json()["audit"]["checked"], 1)
        self.assertEqual(report.json()["audit"]["catalog_total"], 4)
        self.assertNotIn("raw_token", report.text)
        self.assertEqual(report.json()["items"], [])
        self.assertEqual(report.json()["total"], 0)

    def test_v2_audit_nfo_repair_preview_is_authorized_and_path_free(self):
        audit_id = "audit-http-repair"
        video_id = "telegram:10:http-repair"
        conn = db.connect(TEST_URL)
        try:
            conn.execute("INSERT INTO videos (id,provider,title,performers,categories,tags,duration) "
                         "VALUES (%s,'telegram','Repair title','[]','[]','[]',600)", (video_id,))
            conn.execute("INSERT INTO synced_files (file_path,video_id,category) "
                         "VALUES ('Library/http-repair.strm',%s,'video')", (video_id,))
            conn.execute("INSERT INTO video_duplicate_jobs (id,action,state,payload,result,created_at,updated_at) "
                         "VALUES (%s,'consistency_audit','done','{}','{}',1,1)", (audit_id,))
            finding_id = conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence) "
                "VALUES (%s,%s,'missing_nfo','open','Missing NFO') RETURNING id", (audit_id, video_id)
            ).fetchone()[0]
            import_finding_id = conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence) "
                "VALUES (%s,%s,'nfo_import_state_mismatch','open','Import hash mismatch') RETURNING id",
                (audit_id, video_id),
            ).fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        with self.admin_config(), patch.object(admin_module, "HUB_DB_URL", TEST_URL):
            response = self.request(
                "POST", f"/admin/api/v2/audits/{audit_id}/repairs/preview", base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={"finding_ids": [finding_id]},
            )
            reimport = self.request(
                "POST", f"/admin/api/v2/audits/{audit_id}/reimports/preview", base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={"finding_ids": [import_finding_id]},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["operation_type"], "audit_repair_nfo")
        self.assertEqual(response.json()["items"][0]["title"], "Repair title")
        self.assertNotIn("Library/", response.text)
        self.assertEqual(reimport.status_code, 200, reimport.text)
        self.assertEqual(reimport.json()["operation_type"], "audit_import_refresh")
        self.assertNotIn("Library/", reimport.text)

    def test_v2_backup_inventory_and_verification_are_read_only_and_path_safe(self):
        import hashlib
        import sqlite3
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            root = Path(directory)
            backup = root / "adult_hub-test.db"
            with sqlite3.connect(backup) as conn:
                conn.execute("CREATE TABLE sample (id INTEGER)")
                conn.execute("INSERT INTO sample VALUES (1)")
            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
            (root / f"{backup.name}.sha256").write_text(f"{digest}  {backup.name}\n", encoding="ascii")
            with patch.object(admin_module, "BACKUP_DIR", root), \
                 patch.object(admin_module, "BACKUP_RESTORE_DATABASE_URL", ""):
                inventory = self.request("GET", "/admin/api/v2/backups", base_url="http://localhost")
                valid = self.request("GET", f"/admin/api/v2/backups/verify?filename={backup.name}", base_url="http://localhost")
                traversal = self.request("GET", "/admin/api/v2/backups/verify?filename=..%2Fsecret.db", base_url="http://localhost")
        self.assertEqual(inventory.status_code, 200, inventory.text)
        self.assertFalse(inventory.json()["restore_test_available"])
        self.assertNotIn("DATABASE_URL", inventory.text)
        self.assertEqual(inventory.json()["items"][0]["scope"], "Legacy database snapshot; media and settings are not included")
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(valid.json()["archive_status"], "valid")
        self.assertEqual(valid.json()["checksum_status"], "valid")
        self.assertEqual(traversal.status_code, 404)

    def test_v2_backup_restore_test_queues_only_a_safe_filename(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config(), \
             patch.object(admin_module, "BACKUP_DIR", Path(directory)), \
             patch.object(admin_module, "BACKUP_RESTORE_DATABASE_URL", "postgresql://user:secret@localhost:5432/maintenance"), \
             patch.object(admin_module, "HUB_DB_URL", TEST_URL):
            backup = Path(directory) / "adulthub-test.dump.gz"
            backup.write_bytes(b"compressed backup placeholder")
            denied = self.request("POST", "/admin/api/v2/backups/restore-test", base_url="http://localhost",
                                  headers={"Origin": "http://localhost"}, json={"filename": backup.name})
            accepted = self.request("POST", "/admin/api/v2/backups/restore-test", base_url="http://localhost",
                                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                                    json={"filename": backup.name})
            job = get_job(TEST_URL, accepted.json()["job_id"])
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(job["action"], "backup_restore_test")
        self.assertEqual(job["payload"], {"filename": backup.name})

    def test_v2_manual_backup_queues_database_only_job_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            trigger = Path(directory) / "sync.trigger"
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "SYNC_TRIGGER_FILE", trigger):
                responses = [self.request(
                    "POST", "/admin/api/v2/backups/create", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                ) for _ in range(2)]
            for response in responses:
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(response.json()["state"], "queued")
            self.assertEqual(responses[0].json()["job_id"], responses[1].json()["job_id"])
            job = get_job(TEST_URL, responses[0].json()["job_id"])
            self.assertEqual((job["action"], job["state"], job["payload"]),
                             ("backup_create", "queued", {}))
            self.assertEqual(self._job_count(), 1)
            self.assertEqual(json.loads(trigger.read_text()), {"action": "jobs"})

    def test_v2_backup_restore_inventory_reports_disposable_target_availability(self):
        with self.admin_config(), patch.object(admin_module, "BACKUP_DIR", Path("/tmp/no-backups")), \
             patch.object(admin_module, "BACKUP_RESTORE_DATABASE_URL", "postgresql://user:secret@localhost:5432/maintenance"), \
             patch.object(admin_module, "HUB_DB_URL", "postgresql://app:secret@localhost:5432/catalog"):
            result = self.request("GET", "/admin/api/v2/backups", base_url="http://localhost")
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()["restore_test_available"])
        self.assertNotIn("secret", result.text)
        self.assertNotIn("maintenance", result.text)

    def test_v2_restore_test_rejects_malformed_or_primary_database_target(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            backup = Path(directory) / "adulthub-test.dump.gz"
            backup.write_bytes(b"compressed backup placeholder")
            for target in ("not-a-postgres-uri", TEST_URL):
                with self.subTest(target="primary" if target == TEST_URL else "malformed"), \
                     patch.object(admin_module, "BACKUP_DIR", Path(directory)), \
                     patch.object(admin_module, "BACKUP_RESTORE_DATABASE_URL", target), \
                     patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                    inventory = self.request("GET", "/admin/api/v2/backups", base_url="http://localhost")
                    rejected = self.request(
                        "POST", "/admin/api/v2/backups/restore-test", base_url="http://localhost",
                        headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                        json={"filename": backup.name},
                    )
                self.assertEqual(inventory.status_code, 200)
                self.assertFalse(inventory.json()["restore_test_available"])
                self.assertEqual(rejected.status_code, 503, rejected.text)
                self.assertNotIn("postgres", rejected.text.lower())
                self.assertEqual(self._job_count(), 0)

    def test_v2_schedules_use_shared_write_policy_and_revisioned_enable(self):
        with self.admin_config(), patch.object(admin_module, "HUB_DB_URL", TEST_URL):
            denied = self.request(
                "POST", "/admin/api/v2/schedules", base_url="http://localhost",
                headers={"Origin": "http://localhost"},
                json={"name": "Nightly audit", "action": "consistency_audit", "schedule_type": "daily", "daily_at": "03:00"},
            )
            created = self.request(
                "POST", "/admin/api/v2/schedules", base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={"name": "Nightly audit", "action": "consistency_audit", "schedule_type": "daily", "daily_at": "03:00", "timezone": "Europe/Warsaw"},
            )
            schedule_id = created.json()["id"]
            listing = self.request("GET", "/admin/api/v2/schedules", base_url="http://localhost")
            disabled = self.request(
                "POST", f"/admin/api/v2/schedules/{schedule_id}/enabled", base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={"enabled": False, "revision": 1},
            )
            conflict = self.request(
                "POST", f"/admin/api/v2/schedules/{schedule_id}/enabled", base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={"enabled": True, "revision": 1},
            )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["items"][0]["timezone"], "Europe/Warsaw")
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_v2_playback_diagnostics_redacts_and_authorizes_controls(self):
        diagnostic = Mock()
        diagnostic.status.return_value = {"enabled": False, "saved_count": 1, "secret_url": "https://secret.example/token"}
        diagnostic.list_reports.return_value = [{"report_id": "r1", "provider": "telegram", "outcome": "error",
                                                 "started_epoch": 1700000000, "url": "https://secret.example/token"}]
        diagnostic.set_enabled.return_value = {"enabled": True, "enabled_until": "2026-10-05 19:00 UTC", "url": "secret"}
        diagnostic.clear_reports.return_value = 3
        with self.admin_config(), patch.object(admin_module, "_playback_diagnostics", return_value=diagnostic):
            status = self.request("GET", "/admin/api/v2/diagnostics/playback/status", base_url="http://localhost")
            reports = self.request("GET", "/admin/api/v2/diagnostics/playback/reports", base_url="http://localhost")
            denied = self.request("POST", "/admin/api/v2/diagnostics/playback/enable", base_url="http://localhost",
                                  headers={"Origin": "http://localhost"}, json={})
            allowed = self.request("POST", "/admin/api/v2/diagnostics/playback/enable", base_url="http://localhost",
                                   headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={})
            clear_denied = self.request("DELETE", "/admin/api/v2/diagnostics/playback/reports", base_url="http://localhost",
                                        headers={"Origin": "http://localhost"})
            clear_allowed = self.request("DELETE", "/admin/api/v2/diagnostics/playback/reports", base_url="http://localhost",
                                         headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"})
        self.assertEqual(status.status_code, 200)
        self.assertNotIn("secret_url", status.text)
        self.assertEqual(reports.status_code, 200)
        self.assertNotIn("secret.example", reports.text)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertNotIn("url", allowed.json())
        self.assertEqual(clear_denied.status_code, 403)
        self.assertEqual(clear_allowed.status_code, 200, clear_allowed.text)
        diagnostic.clear_reports.assert_called_once_with()
        self.assertEqual(clear_allowed.json()["cleared"], 3)


    def test_v2_cache_clear_requires_write_access_and_reports_count(self):
        clear = Mock(return_value=6)
        with self.admin_config(), patch.object(resolver_app.app.state, "admin_cache_clear", clear):
            denied = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="http://localhost",
                                  headers={"Origin": "http://localhost"}, json={})
            clear.assert_not_called()
            allowed = self.request("POST", "/admin/api/v2/maintenance/cache/clear", base_url="http://localhost",
                                   headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(allowed.json()["cleared"], 6)
        clear.assert_called_once_with()

    def test_v2_cache_clear_empties_real_persistent_resolver_cache(self):
        from stream_cache import PersistentLRUCache

        cache = PersistentLRUCache(db_url=TEST_URL, max_entries=4, ttl_seconds=60)
        cache.set("v2-clear-one", "https://cdn.example/one")
        cache.set("v2-clear-two", "https://cdn.example/two")
        headers = {
            "Origin": "http://localhost",
            "Authorization": "Bearer test-write-token",
        }
        try:
            with self.admin_config(), \
                 patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(resolver_app, "cache", cache), \
                 patch.object(resolver_app.app.state, "admin_cache_clear", resolver_app._admin_cache_clear):
                response = self.request(
                    "POST", "/admin/api/v2/maintenance/cache/clear",
                    base_url="http://localhost", headers=headers, json={},
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {"ok": True, "cleared": 2})
            self.assertEqual(len(cache), 0)
            self.assertIsNone(cache.get("v2-clear-one"))
            with db.connect(TEST_URL) as conn:
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM stream_cache WHERE cache_key LIKE 'v2-clear-%'"
                ).fetchone()[0], 0)
        finally:
            cache.close()

    def test_v2_sync_supports_global_scheduled_and_per_source_modes(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            config = Path(directory) / "config.yaml"
            config.write_text(
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Alpha'\n",
                encoding="utf-8",
            )
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "CONFIG_PATH", config), \
                 patch("video_duplicates.enqueue_job", side_effect=["job-global", "job-scheduled", "job-backfill"]) as enqueue, \
                 patch.object(admin_module, "_wake_worker") as wake:
                headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
                global_sync = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                           headers=headers, json={})
                scheduled = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                         headers=headers, json={"source": "-100111", "sync_mode": "scheduled"})
                backfill = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                        headers=headers, json={"source": "-100111", "sync_mode": "backfill"})
                invalid = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                       headers=headers, json={"source": "bad", "sync_mode": "scheduled"})
                empty_source = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                             headers=headers, json={"source": "", "sync_mode": "scheduled"})
                malformed = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                         headers=headers, content=b"{broken",)
        self.assertEqual([response.status_code for response in (global_sync, scheduled, backfill)], [202, 202, 202])
        self.assertEqual([call.args[1:] for call in enqueue.call_args_list], [
            ("sync", {"source": None, "sync_mode": "scheduled"}),
            ("sync", {"source": "-100111", "sync_mode": "scheduled"}),
            ("sync", {"source": "-100111", "sync_mode": "backfill"}),
        ])
        self.assertEqual([response.json()["job_id"] for response in (global_sync, scheduled, backfill)],
                         ["job-global", "job-scheduled", "job-backfill"])
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(empty_source.status_code, 400)
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(wake.call_count, 3)

    def test_v2_metadata_scan_requires_write_access_and_queues_bounded_worker_job(self):
        with self.admin_config():
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch("video_duplicates.enqueue_job", return_value="metadata-scan-job") as enqueue, \
                 patch("common.job_queue.read_job", side_effect=[
                     {"state": "queued"}, {"state": "processing"}, OSError("temporary status read failure")
                 ]) as read_job, \
                 patch.object(admin_module, "_wake_worker") as wake:
                denied = self.request(
                    "POST", "/admin/api/v2/metadata/enrichment/scan", base_url="http://localhost",
                    headers={"Origin": "http://localhost"}, json={},
                )
                accepted = self.request(
                    "POST", "/admin/api/v2/metadata/enrichment/scan", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={},
                )
                deduplicated = self.request(
                    "POST", "/admin/api/v2/metadata/enrichment/scan", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={},
                )
                accepted_with_status_read_failure = self.request(
                    "POST", "/admin/api/v2/metadata/enrichment/scan", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={},
                )
                invalid = self.request(
                    "POST", "/admin/api/v2/metadata/enrichment/scan", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                    json={"limit": 10000},
                )
                oversized = self.request(
                    "POST", "/admin/api/v2/metadata/enrichment/scan", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                    content=b" " * 4097,
                )

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(accepted.status_code, 202, accepted.text)
        self.assertEqual(accepted.json(), {"ok": True, "job_id": "metadata-scan-job", "state": "queued"})
        self.assertEqual(deduplicated.status_code, 202, deduplicated.text)
        self.assertEqual(deduplicated.json(), {"ok": True, "job_id": "metadata-scan-job", "state": "processing"})
        self.assertEqual(accepted_with_status_read_failure.status_code, 202)
        self.assertEqual(accepted_with_status_read_failure.json(), {
            "ok": True, "job_id": "metadata-scan-job", "state": "accepted"
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(enqueue.call_count, 3)
        enqueue.assert_called_with(TEST_URL, "metadata_backfill", {})
        self.assertEqual(read_job.call_count, 3)
        self.assertEqual(wake.call_count, 3)

    def test_v2_metadata_enrichment_status_exposes_configured_batch_limits(self):
        config = ROOT / "tests/fixtures/config_telegram_only.yaml"
        with self.admin_config(), \
             patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
             patch.object(admin_module, "CONFIG_PATH", config):
            response = self.request(
                "GET", "/admin/api/v2/metadata/enrichment", base_url="http://localhost"
            )
            public = self.request(
                "GET", "/admin/api/v2/metadata/enrichment", base_url="https://media.example.org"
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["batch_limits"], {
            "videos_per_run": 500,
            "pbc_actors_per_run": 50,
            "theporndb_actors_per_run": 50,
            "stashdb_actors_per_run": 50,
        })
        self.assertEqual(response.json()["sync_schedule"], {
            "mode": "interval", "daily_at": None, "interval_minutes": 60,
            "run_on_startup": True,
        })
        self.assertNotIn("entity_key", response.text)
        self.assertEqual(public.status_code, 403)

    def test_v2_metadata_scan_is_blocked_when_worker_lacks_that_action(self):
        from common.job_queue import ALL_ACTIONS

        old_worker_actions = [action for action in ALL_ACTIONS if action != "metadata_backfill"]
        with self.admin_config(worker_actions=old_worker_actions), \
             patch("video_duplicates.enqueue_job") as enqueue, \
             patch.object(admin_module, "_wake_worker") as wake:
            capabilities = self.request("GET", "/admin/api/v2/capabilities", base_url="http://localhost")
            response = self.request(
                "POST", "/admin/api/v2/metadata/enrichment/scan", base_url="http://localhost",
                headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"},
                json={},
            )

        self.assertEqual(capabilities.status_code, 200)
        self.assertFalse(capabilities.json()["actions_enabled"])
        self.assertEqual(capabilities.json()["read_only_reason"], "worker_incompatible")
        self.assertIn("metadata_backfill", capabilities.json()["worker"]["missing_actions"])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["code"], "worker_incompatible")
        enqueue.assert_not_called()
        wake.assert_not_called()
        self.assertEqual(self._job_count(), 0)

    def test_v2_manual_sync_persists_and_deduplicates_real_queue_job(self):
        with self.admin_config():
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "_wake_worker") as wake:
                headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
                first = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                     headers=headers, json={})
                second = self.request("POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                                      headers=headers, json={})

        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        first_job_id = first.json()["job_id"]
        self.assertEqual(second.json()["job_id"], first_job_id)
        job = get_job(TEST_URL, first_job_id)
        payload = job["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        self.assertEqual(job["action"], "sync")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(payload, {"source": None, "sync_mode": "scheduled"})
        self.assertEqual(self._job_count(), 1)
        self.assertEqual(wake.call_count, 2)
        self.assertEqual(wake.call_args.kwargs, {"source": None})

    def test_v2_per_source_sync_scopes_have_distinct_durable_jobs(self):
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            config = Path(directory) / "config.yaml"
            config.write_text(
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Alpha'\n",
                encoding="utf-8",
            )
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "CONFIG_PATH", config), \
                 patch.object(admin_module, "_wake_worker") as wake:
                headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
                scheduled = self.request(
                    "POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                    headers=headers, json={"source": "-100111", "sync_mode": "scheduled"},
                )
                backfill = self.request(
                    "POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                    headers=headers, json={"source": "-100111", "sync_mode": "backfill"},
                )
                scheduled_replay = self.request(
                    "POST", "/admin/api/v2/maintenance/sync", base_url="http://localhost",
                    headers=headers, json={"source": "-100111", "sync_mode": "scheduled"},
                )

        self.assertEqual([response.status_code for response in (scheduled, backfill, scheduled_replay)],
                         [202, 202, 202])
        scheduled_id = scheduled.json()["job_id"]
        backfill_id = backfill.json()["job_id"]
        self.assertNotEqual(scheduled_id, backfill_id)
        self.assertEqual(scheduled_replay.json()["job_id"], scheduled_id)
        scheduled_job = get_job(TEST_URL, scheduled_id)
        backfill_job = get_job(TEST_URL, backfill_id)
        self.assertEqual(scheduled_job["payload"], {"source": "-100111", "sync_mode": "scheduled"})
        self.assertEqual(backfill_job["payload"], {"source": "-100111", "sync_mode": "backfill"})
        self.assertEqual(self._job_count(), 2)
        self.assertEqual(wake.call_count, 3)

    def test_v2_job_retry_and_cancel_use_durable_queue_lifecycle(self):
        from common.job_queue import enqueue_job, finish_job, read_job
        sync = load_flat_sync_module()

        failed_id = enqueue_job(TEST_URL, "collections_refresh", {})
        finish_job(TEST_URL, failed_id, "failed", {"error_code": "simulated_upstream_failure"})
        worker_result = {"collections": [{"name": "Adult Hub · Retry", "items": 1}],
                         "matched_videos": 1}
        with tempfile.TemporaryDirectory(prefix="adult_hub_v2_retry_flow_") as directory, self.admin_config():
            root = Path(directory)
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "_wake_worker") as wake:
                headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
                retried = self.request("POST", f"/admin/api/v2/jobs/{failed_id}/retry",
                                       base_url="http://localhost", headers=headers)
                self.assertEqual(retried.status_code, 200, retried.text)
                retry_id = retried.json()["job_id"]
                queued = read_job(TEST_URL, retry_id)
                queued_payload = queued["payload"]
                if isinstance(queued_payload, str):
                    queued_payload = json.loads(queued_payload)
                self.assertEqual((queued["action"], queued["state"], queued_payload),
                                 ("collections_refresh", "queued", {}))
                with patch.object(sync, "DATA_DIR", root / "data"), \
                     patch.dict("os.environ", {"JELLYFIN_URL": "http://jellyfin.invalid",
                                                 "JELLYFIN_API_KEY": "test-key"}), \
                     patch("common.managed_collections.refresh_managed_collections",
                           new=AsyncMock(return_value=worker_result)) as refresh:
                    with db.connect(TEST_URL) as conn:
                        self.assertEqual(sync.process_video_duplicate_jobs(conn, root / "media"), 1)
                    refresh.assert_awaited_once()
                completed = read_job(TEST_URL, retry_id)
                completed_result = completed["result"]
                if isinstance(completed_result, str):
                    completed_result = json.loads(completed_result)
                self.assertEqual(completed["state"], "done")
                self.assertEqual(completed_result["matched_videos"], 1)

                cancel_id = enqueue_job(TEST_URL, "collections_refresh", {})
                completed_cancel = self.request("POST", f"/admin/api/v2/jobs/{retry_id}/cancel",
                                                base_url="http://localhost", headers=headers)
                cancelled = self.request("POST", f"/admin/api/v2/jobs/{cancel_id}/cancel",
                                         base_url="http://localhost", headers=headers)

        self.assertEqual(completed_cancel.status_code, 409)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["state"], "cancelled")
        self.assertEqual(read_job(TEST_URL, failed_id)["state"], "failed")
        self.assertEqual(read_job(TEST_URL, retry_id)["state"], "done")
        self.assertEqual(read_job(TEST_URL, cancel_id)["state"], "cancelled")
        self.assertEqual(wake.call_args_list[0].args, ("jobs",))

    def test_v2_collections_refresh_persists_real_queue_job(self):
        sync = load_flat_sync_module()

        result = {"collections": [{"name": "Adult Hub · Test", "items": 2}], "matched_videos": 2}
        with tempfile.TemporaryDirectory(prefix="adult_hub_collections_flow_") as directory, self.admin_config(), \
             patch.dict("os.environ", {
                 "JELLYFIN_URL": "http://jellyfin.invalid", "JELLYFIN_API_KEY": "test-key"
             }):
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            with patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.object(admin_module, "_wake_worker") as wake:
                response = self.request("POST", "/admin/api/v2/maintenance/collections/refresh",
                                        base_url="http://localhost",
                                        headers={"Origin": "http://localhost",
                                                 "Authorization": "Bearer test-write-token"}, json={})

            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["job_id"]
            queued = get_job(TEST_URL, job_id)
            self.assertEqual((queued["action"], queued["state"], queued["payload"]),
                             ("collections_refresh", "queued", {}))
            self.assertEqual(self._job_count(), 1)
            wake.assert_called_once_with("jobs")

            with patch.object(sync, "DATA_DIR", root / "data"), \
                 patch.dict("os.environ", {"JELLYFIN_URL": "http://jellyfin.invalid",
                                             "JELLYFIN_API_KEY": "test-key"}), \
                 patch("common.managed_collections.refresh_managed_collections",
                       new=AsyncMock(return_value=result)) as refresh:
                with db.connect(TEST_URL) as conn:
                    completed = sync.process_video_duplicate_jobs(conn, media)

            self.assertEqual(completed, 1)
            refresh.assert_awaited_once()
            self.assertEqual(refresh.await_args.args[1:],
                             ("http://jellyfin.invalid", "test-key", TEST_URL))
            finished = get_job(TEST_URL, job_id)
            self.assertEqual(finished["state"], "done")
            finished_result = finished["result"]
            if isinstance(finished_result, str):
                finished_result = json.loads(finished_result)
            self.assertEqual(finished_result["matched_videos"], 2)
            self.assertEqual(finished_result["collections"], result["collections"])

    def test_v2_jellyfin_refresh_is_authorized_and_does_not_expose_credentials(self):
        from unittest.mock import AsyncMock
        response_from_jellyfin = Mock(status_code=204)
        post = AsyncMock(return_value=response_from_jellyfin)
        with self.admin_config(), patch.dict("os.environ", {
            "JELLYFIN_URL": "http://jellyfin.local/", "JELLYFIN_API_KEY": "secret-jellyfin-key"
        }), patch("httpx.AsyncClient.post", new=post):
            denied = self.request("POST", "/admin/api/v2/maintenance/jellyfin/refresh", base_url="http://localhost",
                                  headers={"Origin": "http://localhost"}, json={})
            allowed = self.request("POST", "/admin/api/v2/maintenance/jellyfin/refresh", base_url="http://localhost",
                                   headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertTrue(allowed.json()["requested"])
        self.assertNotIn("secret-jellyfin-key", allowed.text)
        post.assert_awaited_once()
        self.assertEqual(post.await_args.args[0], "http://jellyfin.local/Library/Refresh")

    def test_v2_jellyfin_refresh_rejects_missing_configuration(self):
        with self.admin_config(), patch.dict("os.environ", {"JELLYFIN_URL": "", "JELLYFIN_API_KEY": ""}):
            response = self.request("POST", "/admin/api/v2/maintenance/jellyfin/refresh", base_url="http://localhost",
                                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("JELLYFIN_API_KEY", response.text)

    def test_v2_collections_refresh_is_durable_and_authorized(self):
        with self.admin_config(), patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
             patch("video_duplicates.enqueue_job", return_value="job-collections") as enqueue, \
             patch.object(admin_module, "_wake_worker") as wake:
            denied = self.request("POST", "/admin/api/v2/maintenance/collections/refresh", base_url="http://localhost",
                                  headers={"Origin": "http://localhost"}, json={})
            with patch.dict("os.environ", {"JELLYFIN_URL": "", "JELLYFIN_API_KEY": ""}):
                unavailable = self.request(
                    "POST", "/admin/api/v2/maintenance/collections/refresh", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={}
                )
            with patch.dict("os.environ", {
                "JELLYFIN_URL": "http://jellyfin.invalid", "JELLYFIN_API_KEY": "test-key"
            }):
                queued = self.request(
                    "POST", "/admin/api/v2/maintenance/collections/refresh", base_url="http://localhost",
                    headers={"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}, json={}
                )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(unavailable.status_code, 503, unavailable.text)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(queued.json()["job_id"], "job-collections")
        enqueue.assert_called_once_with(TEST_URL, "collections_refresh", {})
        wake.assert_called_once_with("jobs")

    def test_v2_playback_diagnostics_export_is_bounded_redacted_and_downloadable(self):
        diagnostic = Mock()
        diagnostic.list_reports.return_value = [
            {"report_id": "recent", "provider": "telegram", "outcome": "error",
             "started_epoch": time.time(), "elapsed_ms": 420, "error_type": "TimeoutError",
             "video_key": "telegram:-1001:secret-message", "url": "https://private.example/token",
             "exception": "private traceback"},
            {"report_id": "old", "provider": "telegram", "started_epoch": time.time() - 40 * 86400},
            {"report_id": "bad-time", "started_epoch": "invalid"},
        ]
        with self.admin_config(), patch.object(admin_module, "_playback_diagnostics", return_value=diagnostic):
            exported = self.request("GET", "/admin/api/v2/diagnostics/playback/export?days=7&limit=10",
                                    base_url="http://localhost")
            invalid = self.request("GET", "/admin/api/v2/diagnostics/playback/export?days=31",
                                   base_url="http://localhost")
            blocked = self.request("GET", "/admin/api/v2/diagnostics/playback/export",
                                   base_url="http://media.example.org")
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(exported.headers["cache-control"], "no-store")
        self.assertIn("attachment; filename=\"adult-hub-playback-diagnostics-", exported.headers["content-disposition"])
        self.assertEqual(diagnostic.list_reports.call_args.kwargs["limit"], 10)
        data = exported.json()
        self.assertEqual(data["scope"]["days"], 7)
        self.assertEqual([report["report_id"] for report in data["reports"]], ["recent"])
        self.assertEqual(data["reports"][0]["elapsed_ms"], 420)
        for secret in ("video_key", "private-message", "private.example", "private traceback"):
            self.assertNotIn(secret, exported.text)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "invalid_request")
        self.assertEqual(invalid.headers["X-Correlation-ID"], invalid.json()["error"]["correlation_id"])
        self.assertEqual(blocked.status_code, 403)

    def test_admin_page_shows_application_version(self):
        with self.admin_config():
            page = self.request("GET", "/admin/", base_url="http://localhost")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="main-content"', page.text)
        self.assertIn('/admin/assets-v2/admin-v2.js', page.text)
        self.assertNotIn("{{APP_VERSION}}", page.text)





    def test_source_access_check_uses_existing_provider_and_sanitizes_failures(self):
        async def oversized_stream():
            yield b'{"source_id":"-100111","padding":"'
            yield b"x" * 5000
            yield b'"}'

        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            config = Path(directory) / "config.yaml"
            config.write_text(
                "sync:\n  interval_minutes: 60\n"
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Alpha'\n        enabled: false\n",
                encoding="utf-8",
            )
            provider = Mock()
            provider.check_configured_channel_access = AsyncMock(return_value=None)
            headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
            with patch.object(admin_module, "CONFIG_PATH", config), \
                 patch("providers.get_provider", return_value=provider):
                checked = self.request("POST", "/admin/api/v2/sources/check",
                    base_url="http://localhost", headers=headers, json={"source_id": "-100111"})
                missing = self.request("POST", "/admin/api/v2/sources/check",
                    base_url="http://localhost", headers=headers, json={"source_id": "-100999"})
                oversized_headers = {**headers, "Content-Length": "5000"}
                oversized = self.request("POST", "/admin/api/v2/sources/check",
                    base_url="http://localhost", headers=oversized_headers, content="x" * 5000)
                streamed_oversized = self.request("POST", "/admin/api/v2/sources/check",
                    base_url="http://localhost", headers=headers, content=oversized_stream())
                provider.check_configured_channel_access.side_effect = RuntimeError("session secret leaked")
                unknown = self.request("POST", "/admin/api/v2/sources/check",
                    base_url="http://localhost", headers=headers, json={"source_id": "-100111"})
                private_error = type("ChannelPrivateError", (Exception,), {})("private channel detail")
                provider.check_configured_channel_access.side_effect = private_error
                denied_access = self.request("POST", "/admin/api/v2/sources/check",
                    base_url="http://localhost", headers=headers, json={"source_id": "-100111"})
            self.assertEqual(checked.status_code, 200, checked.text)
            self.assertEqual(checked.json()["status"], "accessible")
            self.assertEqual(checked.json()["message"], "Telegram read access confirmed.")
            self.assertEqual(unknown.status_code, 200, unknown.text)
            self.assertEqual(unknown.json()["status"], "unknown")
            self.assertNotIn("session secret", unknown.text)
            self.assertEqual(denied_access.json()["status"], "inaccessible")
            self.assertNotIn("private channel detail", denied_access.text)
            self.assertEqual(missing.status_code, 400)
            self.assertEqual(oversized.status_code, 413)
            self.assertEqual(streamed_oversized.status_code, 413)
            self.assertEqual(provider.check_configured_channel_access.await_count, 3)

    def test_v2_source_preview_and_save_are_revision_checked_and_secret_free(self):
        import os
        from common.config_loader import load_config
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                "sync:\n  interval_minutes: 60\n"
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Base'\n",
                encoding="utf-8",
            )
            overlay = root / "managed.json"
            body = {"expected_revision": 0,
                    "overrides": {"telegram": {"channels": {"-100111": {"studio": "Managed"}}}}}
            with patch.object(admin_module, "CONFIG_PATH", config), \
                 patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.dict(os.environ, {"ADMIN_SOURCE_OVERRIDES_PATH": str(overlay)}):
                preview = self.request("POST", "/admin/api/v2/sources/preview", base_url="http://localhost", json=body)
                self.assertFalse(overlay.exists(), "Preview must not write the settings file")
                denied = self.request("POST", "/admin/api/v2/sources/save", base_url="http://localhost",
                                      headers={"Origin": "http://localhost"}, json=body)
                headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
                saved = self.request("POST", "/admin/api/v2/sources/save", base_url="http://localhost",
                                     headers=headers, json=body)
                stale = self.request("POST", "/admin/api/v2/sources/save", base_url="http://localhost",
                                     headers=headers, json=body)
                settings = self.request("GET", "/admin/api/v2/sources/settings", base_url="http://localhost")
                effective = load_config(str(config), force_reload=True)
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertEqual(preview.json()["changes"][0]["after"], "Managed")
            self.assertNotIn("private.invalid", preview.text)
            self.assertNotIn("do-not-return", preview.text)
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertEqual(saved.json()["revision"], 1)
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(settings.status_code, 200, settings.text)
            self.assertEqual(settings.json()["revision"], 1)
            self.assertEqual(settings.json()["overrides"]["telegram"]["channels"]["-100111"]["studio"], "Managed")
            self.assertNotIn("private.invalid", settings.text)
            self.assertEqual(effective.managed_revision, 1)
            self.assertEqual(effective.sources["telegram"].channels[0].studio, "Managed")

    def test_v2_source_audit_outbox_recovers_after_database_outage(self):
        import os
        from common.managed_sources import read_overrides, read_pending_source_audit
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                "sync:\n  interval_minutes: 60\n"
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Base'\n",
                encoding="utf-8",
            )
            overlay = root / "managed.json"
            body = {"expected_revision": 0,
                    "overrides": {"telegram": {"channels": {"-100111": {"studio": "Managed"}}}}}
            headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
            with patch.object(admin_module, "CONFIG_PATH", config), \
                 patch.object(admin_module, "HUB_DB_URL", BAD_URL), \
                 patch.dict(os.environ, {"ADMIN_SOURCE_OVERRIDES_PATH": str(overlay)}):
                saved = self.request("POST", "/admin/api/v2/sources/save", base_url="http://localhost",
                                     headers=headers, json=body)
                pending = read_pending_source_audit(overlay)
                blocked = self.request("POST", "/admin/api/v2/sources/save", base_url="http://localhost",
                                       headers=headers, json={**body, "expected_revision": 1})
                self.assertEqual(read_overrides(overlay)["revision"], 1)
                with patch.object(admin_module, "HUB_DB_URL", TEST_URL):
                    from common import managed_sources
                    with patch.object(managed_sources, "clear_pending_source_audit", side_effect=OSError("simulated crash after audit commit")):
                        first_recovery = self.request("GET", "/admin/api/v2/sources/settings", base_url="http://localhost")
                    settings = self.request("GET", "/admin/api/v2/sources/settings", base_url="http://localhost")
                    events = db.connect(TEST_URL)
                    try:
                        audit_count = events.execute(
                            "SELECT count(*) FROM admin_audit_events WHERE event_key='source-settings:1'"
                        ).fetchone()[0]
                    finally:
                        events.close()
                cleared = read_overrides(overlay)
                outbox_cleared = read_pending_source_audit(overlay)
            self.assertEqual(saved.status_code, 200, saved.text)
            self.assertTrue(saved.json()["audit_pending"])
            self.assertEqual(pending["revision"], 1)
            self.assertEqual(blocked.status_code, 503, blocked.text)
            self.assertTrue(first_recovery.json()["audit_pending"])
            self.assertEqual(settings.status_code, 200, settings.text)
            self.assertFalse(settings.json()["audit_pending"])
            self.assertNotIn("pending_audit", cleared)
            self.assertIsNone(outbox_cleared)
            self.assertEqual(audit_count, 1)

    def test_v2_can_add_new_telegram_channel_with_revision_check(self):
        import os
        from common.config_loader import load_config
        with tempfile.TemporaryDirectory() as directory, self.admin_config():
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(
                "sync:\n  interval_minutes: 60\n"
                "sources:\n  - provider: telegram\n    channels:\n"
                "      - id: '-100111'\n        studio: 'Base'\n",
                encoding="utf-8",
            )
            overlay = root / "managed.json"
            headers = {"Origin": "http://localhost", "Authorization": "Bearer test-write-token"}
            body = {"expected_revision": 0, "channel": {"id": "@new_channel", "studio": "New Studio"}}
            with patch.object(admin_module, "CONFIG_PATH", config), \
                 patch.object(admin_module, "HUB_DB_URL", TEST_URL), \
                 patch.dict(os.environ, {"ADMIN_SOURCE_OVERRIDES_PATH": str(overlay)}):
                denied = self.request("POST", "/admin/api/v2/sources", base_url="http://localhost", json=body)
                created = self.request("POST", "/admin/api/v2/sources", base_url="http://localhost", headers=headers, json=body)
                duplicate = self.request("POST", "/admin/api/v2/sources", base_url="http://localhost", headers=headers, json=body)
                stale = self.request("POST", "/admin/api/v2/sources", base_url="http://localhost", headers=headers,
                                     json={"expected_revision": 0, "channel": {"id": "@second", "studio": "Second"}})
                effective = load_config(str(config), force_reload=True)
            self.assertEqual(denied.status_code, 403)
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json()["source_id"], "@new_channel")
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(stale.status_code, 409)
            self.assertEqual([channel.id for channel in effective.sources["telegram"].channels],
                             ["-100111", "@new_channel"])


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
            with db.connect(TEST_URL) as conn:
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
                with db.connect(TEST_URL) as conn:
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


    def test_open_proxy_routes_removed(self):
        res1 = self.request("GET", "/admin/px?u=https://example.com")
        self.assertEqual(res1.status_code, 404)
        res2 = self.request("GET", "/px?u=https://example.com")
        self.assertEqual(res2.status_code, 404)
        res3 = self.request("GET", "/admin/px/seg.ts?u=https://example.com/seg.ts")
        self.assertEqual(res3.status_code, 404)
        res4 = self.request("GET", "/px/seg.ts?u=https://example.com/seg.ts")
        self.assertEqual(res4.status_code, 404)

    def test_unauthorized_telegram_channel_blocked_at_api(self):
        class MockTelegramProvider:
            def is_channel_authorized(self, ch):
                return False

        with patch.object(resolver_app, "get_provider", return_value=MockTelegramProvider()):
            res_play = self.request("GET", "/admin/play/telegram:me:42:unique/stream")
            self.assertEqual(res_play.status_code, 403)
            res_thumb = self.request("GET", "/admin/api/thumb?provider=telegram&ref=me:42:unique")
            self.assertEqual(res_thumb.status_code, 403)
            res_hist = self.request("GET", "/admin/api/telegram/history/me")
            self.assertEqual(res_hist.status_code, 403)
            res_cat = self.request("GET", "/admin/api/category/me?provider=telegram")
            self.assertEqual(res_cat.status_code, 403)


if __name__ == "__main__":
    unittest.main()
