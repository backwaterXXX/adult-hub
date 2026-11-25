"""Request-level checks for critical resolver routes and admin authorization."""

import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from pg_helper import TEST_URL, reset_db
import resolver.admin as admin_module
import resolver.app as resolver_app
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


class BaseHttpApiTestCase(unittest.TestCase):
    MUTATIONS = (
        ("POST", "/admin/api/v2/maintenance/cache/clear"),
        ("POST", "/admin/api/v2/maintenance/filesize-backfill"),
        ("POST", "/admin/api/v2/maintenance/dead-streams/quarantine"),
        ("POST", "/admin/api/v2/operations/preview"),
        ("POST", "/admin/api/v2/operations/execute"),
        ("POST", "/admin/api/v2/duplicates/actions"),
        ("POST", "/admin/api/v2/duplicates/visual/verify"),
        ("POST", "/admin/api/v2/jobs/{job_id}/retry"),
        ("POST", "/admin/api/v2/jobs/{job_id}/cancel"),
        ("POST", "/admin/api/v2/metadata/execute"),
        ("POST", "/admin/api/v2/sources"),
        ("POST", "/admin/api/v2/sources/save"),
        ("POST", "/admin/api/v2/sources/check"),
        ("POST", "/admin/api/v2/sources/remove"),
        ("POST", "/admin/api/v2/availability/execute"),
        ("POST", "/admin/api/v2/backups/create"),
        ("POST", "/admin/api/v2/backups/restore-test"),
        ("POST", "/admin/api/v2/schedules"),
        ("POST", "/admin/api/v2/schedules/{schedule_id}/enabled"),
        ("POST", "/admin/api/v2/schedules/{schedule_id}/cron"),
        ("POST", "/admin/api/v2/schedules/{schedule_id}/run"),
        ("POST", "/admin/api/v2/schedules/system/{schedule_id}/run"),
        ("POST", "/admin/api/v2/schedules/system/{schedule_id}"),
        ("POST", "/admin/api/v2/schedules/system/{schedule_id}/stages/{stage_id}/enabled"),
        ("POST", "/admin/api/v2/schedules/system/{schedule_id}/stages/{stage_id}/run"),
        ("POST", "/admin/api/v2/quality/proposals/{proposal_id}/reject"),
        ("POST", "/admin/api/v2/quality/proposals/{proposal_id}/refresh"),
        ("POST", "/admin/api/v2/maintenance/cache/clear"),
        ("POST", "/admin/api/v2/maintenance/sync"),
        ("POST", "/admin/api/v2/performers/proposals/{proposal_id}"),
        ("POST", "/admin/api/v2/performers/proposals/reconcile"),
        ("POST", "/admin/api/v2/performers/proposal-groups/{reason}/{key}"),
        ("POST", "/admin/api/v2/pairing/approve"),
        ("PUT", "/admin/api/v2/organize/people/{name}/profile"),
        ("POST", "/admin/api/v2/library/videos/{video_id}/generate-artwork"),
        ("POST", "/admin/api/v2/library/videos/{video_id}/download-artwork"),
        ("POST", "/admin/api/v2/library/videos/{video_id}/generate-phash"),
        ("POST", "/admin/api/v2/telegram/auth/start"),
        ("POST", "/admin/api/v2/telegram/auth/code"),
        ("POST", "/admin/api/v2/telegram/auth/password"),
        ("POST", "/admin/api/v2/telegram/auth/cancel"),
        ("POST", "/admin/api/v2/telegram/auth/resend"),
        ("POST", "/admin/api/v2/telegram/accounts"),
        ("PATCH", "/admin/api/v2/telegram/accounts/{account_id}"),
        ("DELETE", "/admin/api/v2/telegram/accounts/{account_id}"),
        ("POST", "/admin/api/v2/telegram/accounts/{account_id}/test"),
        ("POST", "/admin/api/v2/telegram/accounts/{account_id}/session/replace-unauthorized"),
    )
    CLIENT_INTEGRATION_MUTATIONS = {
        ("POST", "/admin/api/v2/video-phash/frames"),
        ("POST", "/admin/api/v2/video-phash/generate"),
        ("POST", "/admin/api/v2/phash-jobs/{job_id}/retry"),
        ("POST", "/admin/api/v2/phash-jobs/{job_id}/cancel"),
    }

    @classmethod
    def setUpClass(cls):
        from pg_helper import init_test_db
        initial_connection = init_test_db()
        initial_connection.close()


    def setUp(self):
        reset_db()
        conn = db.connect(TEST_URL)
        try:
            conn.execute("ALTER TABLE videos ALTER COLUMN duration SET DEFAULT 600")
            conn.commit()
        finally:
            conn.close()


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



class TestResolverHttpApi(BaseHttpApiTestCase):
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
        # The header links to Status; probe details and recent failures live there.
        self.assertIn('id="serviceStatus"', response.text)
        self.assertIn('id="writeAccessNotice"', response.text)
        self.assertIn('id="systemProbeRows"', response.text)
        self.assertIn('id="systemRecentFailures"', response.text)
        for removed_chip in ("connectionState", "adminAccessMode", "activeTaskCount"):
            self.assertNotIn(f'id="{removed_chip}"', response.text)
        self.assertIn('/admin/assets-v2/header-status.js', response.text)
        self.assertIn('id="operationDialog"', response.text)
        self.assertIn('id="bulkSelectionCount"', response.text)
        self.assertIn('id="checkAvailabilityBulk"', response.text)
        self.assertIn('id="quarantineExport"', response.text)
        self.assertNotIn('id="qualityIssuesPane"', response.text)
        self.assertIn('id="systemBackupView"', response.text)
        self.assertNotIn('id="systemDiagnosticsView"', response.text)
        self.assertIn('id="systemMaintenanceView"', response.text)
        self.assertNotIn('id="systemSyncActivityView"', response.text)
        self.assertIn('data-system-tab="maintenance"', response.text)
        self.assertIn('id="systemStatusView"', response.text)
        self.assertIn('data-system-tab="backups"', response.text)
        self.assertNotIn('id="systemActivityView"', response.text)
        self.assertNotIn('/admin/assets-v2/activity.js', response.text)
        self.assertIn('/admin/assets-v2/maintenance.js', response.text)
        self.assertNotIn('/admin/assets-v2/linkcheck.js', response.text)
        self.assertNotIn('/admin/assets-v2/enrichment.js', response.text)
        self.assertNotIn('/admin/assets-v2/audits.js', response.text)
        self.assertIn("Operation history", response.text)
        source_script = self.request("GET", "/admin/assets-v2/sources-core.js", base_url="http://127.0.0.1")
        self.assertEqual(source_script.status_code, 200, source_script.text)
        self.assertIn("const SOURCE_CHECK_URL = `${V2_API}/sources/check`;", source_script.text)
        maintenance_script = self.request("GET", "/admin/assets-v2/maintenance.js", base_url="http://127.0.0.1")
        self.assertEqual(maintenance_script.status_code, 200, maintenance_script.text)
        self.assertIn("`${api}/sync`", maintenance_script.text)
        quarantine_script = self.request("GET", "/admin/assets-v2/quarantine.js", base_url="http://127.0.0.1")
        self.assertEqual(quarantine_script.status_code, 200, quarantine_script.text)
        self.assertIn("organize/quarantine", quarantine_script.text)


    def test_admin_v2_status_remains_and_retired_reports_return_404(self):
        with patch.object(admin_module, "ADMIN_PUBLIC", False), \
             patch.object(admin_module, "ENABLE_ADMIN", True), \
             patch.object(admin_module, "_db_stats", return_value={"db_ok": True, "videos_total": 3}), \
             patch.object(admin_module, "_sync_status", return_value={"running": True}), \
             patch.object(admin_module, "_attention_summary", return_value={"pending_tasks": 1}):
            status = self.request("GET", "/admin/api/v2/status", base_url="http://127.0.0.1")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["db"]["videos_total"], 3)
        for method, path in (
            ("GET", "/admin/api/v2/audits"),
            ("GET", "/admin/api/v2/activity"),
            ("GET", "/admin/api/v2/activity/changes"),
            ("GET", "/admin/api/v2/metadata/enrichment"),
            ("POST", "/admin/api/v2/metadata/enrichment/scan"),
            ("GET", "/admin/api/v2/linkcheck"),
            ("GET", "/admin/api/v2/sync/activity"),
            ("GET", "/admin/api/v2/sources/search?q=test"),
            ("GET", "/admin/api/v2/quality/items"),
            ("GET", "/admin/api/v2/quality/summary"),
            ("GET", "/admin/api/v2/diagnostics/playback/status"),
        ):
            with self.subTest(method=method, path=path):
                response = self.request(method, path, base_url="http://127.0.0.1")
                self.assertEqual(response.status_code, 404, response.text)
        for asset in ("activity.js", "audits.js", "enrichment.js", "linkcheck.js", "sync-activity.js"):
            with self.subTest(asset=asset):
                self.assertEqual(self.request("GET", f"/admin/assets-v2/{asset}").status_code, 404)


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


    def test_every_admin_mutation_rejects_missing_authorization_before_parsing_body(self):
        with self.admin_config():
            for method, path in self.MUTATIONS:
                with self.subTest(path=path):
                    request_path = (
                        path.replace("{proposal_id}", "1")
                        .replace("{video_id}", "telegram:123")
                        .replace("{account_id}", "test_acc")
                    )
                    response = self.request(method, request_path, base_url="http://localhost",
                                            content=b"invalid json",
                                            headers={"Origin": "http://localhost"})
                    self.assertEqual(response.status_code, 403, response.text)


    def test_every_admin_mutation_requires_origin_without_token(self):
        with self.admin_config(token="", public=True):
            for method, path in self.MUTATIONS:
                with self.subTest(path=path):
                    request_path = (
                        path.replace("{proposal_id}", "1")
                        .replace("{video_id}", "telegram:123")
                        .replace("{account_id}", "test_acc")
                    )
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
                    "POST", "/admin/api/v2/availability/execute", base_url="http://localhost",
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
                    "POST", "/admin/api/v2/availability/execute", base_url="http://localhost",
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


    def test_admin_v2_unhandled_route_errors_use_redacted_envelope(self):
        async def unexpected_failure():
            raise RuntimeError("private source /srv/library token=secret")

        from resolver.admin_v2_errors import V2ErrorRoute
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


    def test_admin_page_shows_application_version(self):
        with self.admin_config():
            page = self.request("GET", "/admin/", base_url="http://localhost")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="main-content"', page.text)
        self.assertIn('/admin/assets-v2/admin-v2-core.js', page.text)
        self.assertNotIn("__APP_VERSION__", page.text)
        self.assertIn("Admin", page.text)
        self.assertIn(f"v{resolver_app.APP_VERSION}", page.text)



if __name__ == "__main__":
    unittest.main()
