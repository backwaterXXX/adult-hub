"""Database-free contract tests for Admin V2 overview and source reads."""

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.responses import JSONResponse
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resolver"))

import admin_v2_sources
import admin_v2_status
import admin_data


def request(path):
    raw = path.encode()
    return Request({
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": raw, "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 1), "server": ("test", 80), "root_path": "",
        "app": SimpleNamespace(state=SimpleNamespace(admin_extra=lambda: {
            "status": "ok", "version": "1.2.3", "providers": ["telegram", "not safe"],
            "cache_entries": 5, "api_key": "do-not-send",
        })),
    })


class InlineAsyncio:
    @staticmethod
    async def to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    @staticmethod
    async def gather(*awaitables):
        return tuple([await item for item in awaitables])


def admin(blocked=False):
    return SimpleNamespace(
        HUB_DB_URL=None,
        _admin_blocked=lambda _request: blocked,
        JSONResponse=JSONResponse,
        asyncio=InlineAsyncio,
        _db_stats=lambda: {"db_ok": True, "db_path": "/private/path", "database_backend": "PostgreSQL", "db_size_mb": 1.5,
                           "videos_total": 8, "telegram_channels": {"-100private": 8},
                           "error": "private details", "by_provider": {
                               "tg:[private studio name]": 8, "second-web": 3, "invalid provider": 99,
                           }},
        _sync_status=lambda: {"running": False, "status_text": "Ready", "secret": "private"},
        _attention_summary=lambda: {"pending_tasks": 2, "raw_error": "private"},
        _linkcheck_tail=lambda _limit=20: {
            "path": "/private/linkcheck.log", "exists": True,
            "mtime_human": "2026-10-06 12:00", "tail": [
                "2026-10-06 12:00:00 [telegram] OK  telegram:-100123:secret-id: proxied playlist OK (8192B)",
                "2026-10-06 12:00:30 [telegram] FAIL telegram:-100123:other-id: resolver error: https://secret.invalid/path?token=secret",
                "2026-10-06 12:01:00 [telegram] 4/5 ok -> BROKEN",
            ],
        },
        _sync_activity_tail=lambda _limit: {
            "path": "/private/sync_activity.log", "exists": True,
            "mtime_human": "2026-10-06 12:00", "tail": [
                "2026-10-06 12:00:00,001 [INFO] Starting Adult-Hub sync (deduplicated)...",
                "2026-10-06 12:00:01,001 [INFO] Media Dir: /srv/private/path",
                "2026-10-06 12:00:02,001 [ERROR] Telegram sync failed for telegram:-100:secret: token=hidden",
                "2026-10-06 12:00:03,001 [INFO] Sync completed successfully.",
            ],
        },
        _capabilities=lambda: {"actions_enabled": False},
        admin_data=SimpleNamespace(configured_sources=lambda _path: {
            "sources": [{"provider": "telegram", "channels": []}],
            "available": True, "managed_revision": 4,
        }),
        CONFIG_PATH="/private/config.yaml",
        _sync_history=lambda limit=30: {"runs": [{"managed_config_revision": 3}], "limit": limit},
    )


class TestAdminV2Status(unittest.IsolatedAsyncioTestCase):
    async def test_jellyfin_probe_is_bounded_public_and_returns_no_instance_details(self):
        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                return False
            async def get(self, url):
                self.requested_url = url
                return SimpleNamespace(status_code=200, text="private server details")

        client = Client(timeout=3.0, follow_redirects=False)
        client_type = Mock(return_value=client)
        admin_module = admin()
        endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin_module).routes
                        if route.path == "/api/v2/status/jellyfin")
        with patch.dict("os.environ", {"JELLYFIN_URL": "https://jellyfin.example/base"}), \
                patch.object(admin_v2_status.httpx, "AsyncClient", client_type):
            response = await endpoint(request("/admin/api/v2/status/jellyfin"))

        self.assertEqual(response["status"], "reachable")
        self.assertRegex(response["checked_at"], r"\+00:00$")
        self.assertEqual(set(response), {"ok", "status", "checked_at"})
        self.assertEqual(client.requested_url, "https://jellyfin.example/base/System/Info/Public")
        self.assertEqual(client_type.call_args.kwargs, {"timeout": 3.0, "follow_redirects": False})
        self.assertNotIn("private server details", str(response))

    async def test_jellyfin_probe_handles_missing_configuration_and_denies_remote_admin(self):
        class Client:
            def __init__(self, **_kwargs):
                raise AssertionError("A missing or blocked Jellyfin probe must not make a request")

        admin_module = admin()
        endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin_module).routes
                        if route.path == "/api/v2/status/jellyfin")
        with patch.dict("os.environ", {"JELLYFIN_URL": ""}), \
                patch.object(admin_v2_status.httpx, "AsyncClient", Client):
            missing = await endpoint(request("/admin/api/v2/status/jellyfin"))
        self.assertEqual(missing, {"ok": True, "status": "not_configured", "checked_at": None})

        admin_module._admin_blocked = lambda _request: True
        blocked = await endpoint(request("/admin/api/v2/status/jellyfin"))
        self.assertEqual(blocked.status_code, 403)

    async def test_jellyfin_probe_does_not_follow_redirects_or_send_api_key(self):
        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                return False
            async def get(self, url):
                return SimpleNamespace(status_code=503, text="secret token should not escape")

        admin_module = admin()
        endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin_module).routes
                        if route.path == "/api/v2/status/jellyfin")
        with patch.dict("os.environ", {"JELLYFIN_URL": "http://jellyfin.example"}), \
                patch.dict("os.environ", {"JELLYFIN_API_KEY": "must-not-be-used"}), \
                patch.object(admin_v2_status.httpx, "AsyncClient", Client):
            response = await endpoint(request("/admin/api/v2/status/jellyfin"))
        self.assertEqual(response["status"], "unavailable")
        self.assertNotIn("must-not-be-used", str(response))
        self.assertNotIn("secret token should not escape", str(response))

    async def test_jellyfin_probe_timeout_is_reported_without_exception_details(self):
        class Client:
            def __init__(self, **_kwargs):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                return False
            async def get(self, _url):
                raise admin_v2_status.httpx.TimeoutException("https://jellyfin.invalid/?api_key=secret")

        admin_module = admin()
        endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin_module).routes
                        if route.path == "/api/v2/status/jellyfin")
        with patch.dict("os.environ", {"JELLYFIN_URL": "https://jellyfin.example"}), \
                patch.object(admin_v2_status.httpx, "AsyncClient", Client):
            response = await endpoint(request("/admin/api/v2/status/jellyfin"))
        self.assertEqual(response["status"], "unavailable")
        self.assertNotIn("jellyfin.invalid", str(response))
        self.assertNotIn("api_key", str(response))

    async def test_database_size_accepts_postgres_decimal(self):
        admin_module = admin()
        stats = admin_module._db_stats()
        stats["db_size_mb"] = Decimal("144.65")
        admin_module._db_stats = lambda: stats
        endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin_module).routes
                        if route.path == "/api/v2/status")

        response = await endpoint(request("/admin/api/v2/status"))

        self.assertEqual(response["db"]["db_size_mb"], 144.65)

    async def test_database_size_omits_invalid_decimal_values(self):
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-0.01")):
            with self.subTest(value=str(value)):
                admin_module = admin()
                stats = admin_module._db_stats()
                stats["db_size_mb"] = value
                admin_module._db_stats = lambda stats=stats: stats
                endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin_module).routes
                                if route.path == "/api/v2/status")

                response = await endpoint(request("/admin/api/v2/status"))

                self.assertNotIn("db_size_mb", response["db"])


    async def test_status_only_exposes_allowlisted_database_backend(self):
        admin_module = admin()
        admin_module._db_stats = lambda: {
            "db_ok": True, "db_path": "/private/db", "db_size_mb": 12,
            "database_backend": "PostgreSQL; host=db.internal password=secret",
        }
        endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin_module).routes
                        if route.path == "/api/v2/status")
        response = await endpoint(request("/admin/api/v2/status"))
        self.assertNotIn("database_backend", response["db"])
        self.assertNotIn("private/db", str(response))
        self.assertNotIn("db.internal", str(response))
        self.assertNotIn("password=secret", str(response))


    async def test_sync_status_read_error_is_unavailable_not_waiting(self):
        admin_module = admin()
        admin_module._sync_status = lambda: {
            "running": False, "status_text": "Waiting for next run", "error": "/private/status.json denied",
        }
        admin_module._sync_history = lambda _limit=1: {"runs": []}
        endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin_module).routes
                        if route.path == "/api/v2/status")
        response = await endpoint(request("/admin/api/v2/status"))
        self.assertEqual(response["sync"], {
            "available": False, "running": False, "freshness": "unknown",
            "status_text": "Status unavailable", "history_available": True, "last_run": None,
        })
        self.assertNotIn("private", str(response))

    async def test_unreadable_sync_history_is_unavailable_rather_than_absent(self):
        admin_module = admin()
        admin_module._sync_status = lambda: {"available": True, "running": False, "last_success": True}
        endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin_module).routes
                        if route.path == "/api/v2/status")
        for history in ({"runs": [], "error": "/private/sync-history.log denied"},
                        {"runs": []}):
            with self.subTest(history=str(history)):
                admin_module._sync_history = lambda _limit=1, history=history: history
                response = await endpoint(request("/admin/api/v2/status"))
                self.assertIsNone(response["sync"]["last_run"])
                self.assertEqual(response["sync"]["history_available"], "error" not in history)
                self.assertNotIn("private", str(response["sync"]))

        def broken(_limit):
            raise OSError("/private/sync-history.log denied")

        admin_module._sync_history = broken
        response = await endpoint(request("/admin/api/v2/status"))
        self.assertFalse(response["sync"]["history_available"])
        self.assertIsNone(response["sync"]["last_run"])
        self.assertNotIn("private", str(response["sync"]))

    async def test_sync_status_freshness_uses_two_configured_intervals_and_missing_is_unknown(self):
        endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin()).routes
                        if route.path == "/api/v2/status")
        now = 1_800_000_000
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.yaml"
            config.write_text("sync:\n  interval_minutes: 60\nsources: []\n", encoding="utf-8")
            admin_module = admin()
            admin_module.CONFIG_PATH = config
            admin_module._sync_history = lambda _limit=1: {"runs": [
                {"started_at": now - 600, "ended_at": now - 60, "success": True, "private_path": "/private/history"}]}
            states = [
                ({"available": True, "exists": True, "running": False,
                  "last_success": True, "updated_at": now - 3600}, "fresh", "Last run succeeded"),
                ({"available": True, "exists": True, "running": False,
                  "last_success": True, "updated_at": now - 7200}, "fresh", "Last run succeeded"),
                ({"available": True, "exists": True, "running": False,
                  "last_success": False, "updated_at": now - 7201}, "stale", "Status stale"),
                ({"available": True, "exists": True, "running": False,
                  "last_success": True}, "unknown", "Last run succeeded"),
            ]
            endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin_module).routes
                            if route.path == "/api/v2/status")
            with patch.object(admin_v2_status.time, "time", return_value=now):
                for state, freshness, status_text in states:
                    admin_module._sync_status = lambda state=state: state
                    result = await endpoint(request("/admin/api/v2/status"))
                    self.assertEqual(result["sync"]["freshness"], freshness)
                    self.assertEqual(result["sync"]["status_text"], status_text)

                # The recorded run time, not the status-read time, answers "when did the last sync run?".
                self.assertEqual(result["sync"]["last_run"], {
                    "started_at": "2027-01-15T07:50:00Z",
                    "ended_at": "2027-01-15T07:59:00Z",
                    "success": True,
                })
                self.assertTrue(result["sync"]["history_available"])
                self.assertNotIn("private", str(result["sync"]))

            missing_status = Path(directory) / "missing-status.json"
            admin_module._sync_status = lambda: admin_data.sync_status(missing_status)
            admin_module._sync_history = lambda _limit=1: {"runs": []}
            missing = await endpoint(request("/admin/api/v2/status"))
            self.assertEqual(missing["sync"], {
                "available": False, "running": False, "freshness": "unknown",
                "status_text": "Status unavailable", "history_available": True, "last_run": None,
            })

            daily_config = Path(directory) / "daily.yaml"
            daily_config.write_text(
                "sync:\n  interval_minutes: 60\n  daily_at: '03:30'\nsources: []\n",
                encoding="utf-8",
            )
            self.assertEqual(admin_v2_status._sync_stale_after_seconds(daily_config), 48 * 60 * 60)

    async def test_overview_is_allowlisted_and_excludes_paths_and_errors(self):
        endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin()).routes
                        if route.path == "/api/v2/status")
        response = await endpoint(request("/admin/api/v2/status"))
        self.assertEqual(response["db"]["videos_total"], 8)
        self.assertEqual(response["runtime"], {
            "status": "ok", "version": "1.2.3", "cache_entries": 5, "providers": ["telegram"],
        })
        self.assertEqual(response["db"]["db_size_mb"], 1.5)
        self.assertEqual(response["db"]["telegram_channels"], {"0": True})
        self.assertEqual(response["db"]["by_provider"], {"telegram": 8, "second-web": 3})
        self.assertNotIn("db_path", response["db"])
        self.assertNotIn("error", response["db"])
        self.assertNotIn("secret", response["sync"])
        self.assertNotIn("raw_error", response["attention"])
        self.assertNotIn("private studio name", str(response))
        self.assertNotIn("-100private", str(response))

    async def test_overview_obeys_admin_read_block(self):
        endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin(blocked=True)).routes
                        if route.path == "/api/v2/status")
        response = await endpoint(request("/admin/api/v2/status"))
        self.assertEqual(response.status_code, 403)

    async def test_capabilities_are_exposed_under_the_v2_route(self):
        endpoint = next(route.endpoint for route in admin_v2_status.build_router(admin()).routes
                        if route.path == "/api/v2/capabilities")
        response = await endpoint(request("/admin/api/v2/capabilities"))
        self.assertFalse(response["actions_enabled"])

    async def test_v2_source_reads_preserve_revision_and_history_contract(self):
        routes = {route.path: route.endpoint for route in admin_v2_sources.build_router(admin()).routes}
        sources = await routes["/api/v2/sources/list"](request("/admin/api/v2/sources/list"))
        history = await routes["/api/v2/sources/sync-history"](request("/admin/api/v2/sources/sync-history"), 50)
        self.assertEqual(sources["desired_revision"], 4)
        self.assertEqual(sources["applied_revision"], 3)
        self.assertEqual(history["limit"], 50)

    async def test_v2_sync_history_projects_operator_metrics_and_hides_hosts_and_raw_errors(self):
        admin_module = admin()
        admin_module._sync_history = lambda limit=30: {
            "runs": [{
                "started_at": 1791367200, "ended_at": 1791367210, "duration_seconds": 10,
                "success": False, "sync_mode": "scheduled", "managed_config_revision": 4,
                "source_filter": None,
                "sources": [{"source": "telegram:-100123", "label": "tg:[Contract Studio]",
                             "success": False, "added": 2, "removed": 1, "videos": 12,
                             "duration_seconds": 4.25, "error": "Sync failed (TimeoutError).",
                             "detail": "history write incomplete; token=private https://private.invalid/a?key=secret /srv/media/private",
                             "exception": "secret https://private.invalid",
                             "warnings": ["history batch: TimeoutError: https://private.invalid secret"]}],
                "stages": {"telegram_history": {"calls": 2, "total_seconds": 4.25,
                                                  "host": "sync.internal"}},
                "http": {"failed_attempts": 1, "retries": 1, "rate_limited": 1,
                         "hosts": {"sync.internal": {"token": "secret"}}},
                "database_url": "postgresql://private",
            }],
            "total": 1, "truncated": False,
        }
        endpoint = next(route.endpoint for route in admin_v2_sources.build_router(admin_module).routes
                        if route.path == "/api/v2/sources/sync-history")
        response = await endpoint(request("/admin/api/v2/sources/sync-history"), 50)
        self.assertTrue(response["available"])
        run = response["runs"][0]
        self.assertEqual(run["started_at"], 1791367200)
        self.assertFalse(run["success"])
        self.assertEqual(run["sources"][0]["source"], "telegram:-100123")
        self.assertEqual(run["sources"][0]["videos"], 12)
        self.assertTrue(run["sources"][0]["history_deferred"])
        self.assertNotIn("warnings", run["sources"][0])
        self.assertEqual(run["duration_seconds"], 10)
        self.assertEqual(run["sources"][0]["error"], "Sync failed (TimeoutError).")
        self.assertEqual(run["sources"][0]["detail"],
                         "history write incomplete; token=[redacted] [link hidden] [path hidden]")
        self.assertEqual(run["stages"]["telegram_history"], {"calls": 2, "total_seconds": 4.25})
        self.assertEqual(run["http"], {"failed_attempts": 1, "retries": 1, "rate_limited": 1})
        self.assertNotIn("sync.internal", str(response))
        self.assertNotIn("private.invalid", str(response))
        self.assertNotIn("/srv/media", str(response))
        self.assertNotIn("key=secret", str(response))
        self.assertNotIn("database_url", str(response))

        admin_module._sync_history = lambda _limit=30: {"runs": [], "error": "/private/path denied"}
        unavailable = await endpoint(request("/admin/api/v2/sources/sync-history"), 50)
        self.assertFalse(unavailable["available"])
        self.assertNotIn("private", str(unavailable))



    async def test_v2_source_reads_obey_admin_read_block(self):
        routes = {route.path: route.endpoint for route in admin_v2_sources.build_router(admin(True)).routes}
        for path, args in (("/api/v2/sources/list", (request("/admin/api/v2/sources/list"),)),
                           ("/api/v2/sources/sync-history", (request("/admin/api/v2/sources/sync-history"), 30))):
            response = await routes[path](*args)
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
