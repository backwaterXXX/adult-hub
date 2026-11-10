"""DB-free tests for per-account Telegram authorization, the router filter and the sync gate.

No real Telegram connection is made: is_session_authorized is replaced by a fake that
answers from a set of session paths, and accounts live in the in-memory store.
"""

import asyncio
import atexit
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

import resolver.admin_v2_telegram_auth as admin_v2_telegram_auth
from resolver.providers import telegram_authorization
from resolver.providers.telegram_account_store import (
    _MEMORY_ACCOUNTS,
    _MEMORY_CHANNEL_ACCESS,
    set_memory_only_mode,
    upsert_account,
)
from resolver.providers.telegram_router import TelegramAccountRouter

API_ENV = {
    "TELEGRAM_API_ID": "123456",
    "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
}

_SYNC_TEMP_DIR = None


def _load_sync_module():
    """Import sync.sync with its activity log and data/cache paths in a temp directory.

    Importing sync.sync opens a rotating log file under the data directory, so the
    import must never point at the production data volume.
    """
    global _SYNC_TEMP_DIR
    if "sync.sync" in sys.modules:
        return sys.modules["sync.sync"]
    if _SYNC_TEMP_DIR is None:
        _SYNC_TEMP_DIR = tempfile.mkdtemp(prefix="sync-gate-")
        atexit.register(shutil.rmtree, _SYNC_TEMP_DIR, True)
    with patch.dict(os.environ, {
        "SYNC_ACTIVITY_LOG_PATH": os.path.join(_SYNC_TEMP_DIR, "sync_activity.log"),
        "DATA_DIR": _SYNC_TEMP_DIR,
        "CACHE_DIR": _SYNC_TEMP_DIR,
    }):
        import sync.sync as sync_module
    return sync_module


class FakeSessionCheck:
    """Stands in for is_session_authorized: authorized only for paths in the given set."""

    def __init__(self, authorized_paths):
        self.authorized_paths = set(authorized_paths)
        self.calls = []

    async def __call__(self, session_path, api_id, api_hash, *, raise_on_error=False):
        self.calls.append(session_path)
        return session_path in self.authorized_paths


class AuthorizationFixtureMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.default_session = os.path.join(self.tmp.name, "telegram.session")
        self.konrad_session = os.path.join(self.tmp.name, "telegram.Konrad.session")
        for path in (self.default_session, self.konrad_session):
            with open(path, "wb") as handle:
                handle.write(b"session-bytes")

        env_patch = patch.dict(os.environ, API_ENV)
        env_patch.start()
        self.addCleanup(env_patch.stop)

        _MEMORY_ACCOUNTS.clear()
        _MEMORY_CHANNEL_ACCESS.clear()
        set_memory_only_mode(True)
        self.addCleanup(set_memory_only_mode, False)

        telegram_authorization._cache.clear()
        telegram_authorization._generations.clear()
        telegram_authorization._inflight.clear()

        # Virtual clock so cache expiry can be tested without sleeping.
        self.now = 1000.0
        clock_patch = patch.object(telegram_authorization, "_clock", lambda: self.now)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

        # Default is unauthorized; Konrad is the logged-in account.
        self.session_check = FakeSessionCheck({self.konrad_session})
        check_patch = patch.object(telegram_authorization, "is_session_authorized", self.session_check)
        check_patch.start()
        self.addCleanup(check_patch.stop)

    def add_default_and_konrad(self):
        upsert_account("default", label="Default", session_file=self.default_session, enabled=True)
        upsert_account("Konrad", label="Konrad", session_file=self.konrad_session, enabled=True)


class AccountAuthorizationTests(AuthorizationFixtureMixin, unittest.TestCase):
    def test_authorized_account_ids_keeps_only_enabled_authorized_in_input_order(self):
        upsert_account("gamma", session_file=self.konrad_session, enabled=True)
        upsert_account("beta", session_file=self.konrad_session, enabled=False)
        upsert_account("alpha", session_file=self.konrad_session, enabled=True)
        upsert_account("delta", session_file=self.default_session, enabled=True)

        result = asyncio.run(
            telegram_authorization.authorized_account_ids(["gamma", "beta", "delta", "alpha"])
        )

        self.assertEqual(result, ["gamma", "alpha"])

    def test_cache_hit_does_not_check_again(self):
        self.add_default_and_konrad()

        first = asyncio.run(telegram_authorization.is_account_authorized("Konrad"))
        second = asyncio.run(telegram_authorization.is_account_authorized("Konrad"))

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(self.session_check.calls, [self.konrad_session])

    def test_negative_result_expires_after_negative_ttl(self):
        self.add_default_and_konrad()

        self.assertFalse(asyncio.run(telegram_authorization.is_account_authorized("default")))
        self.assertEqual(len(self.session_check.calls), 1)

        self.now += telegram_authorization.NEGATIVE_CACHE_SECONDS - 1
        self.assertFalse(asyncio.run(telegram_authorization.is_account_authorized("default")))
        self.assertEqual(len(self.session_check.calls), 1)

        self.now += 2
        self.assertFalse(asyncio.run(telegram_authorization.is_account_authorized("default")))
        self.assertEqual(len(self.session_check.calls), 2)

    def test_positive_result_expires_after_positive_ttl(self):
        self.add_default_and_konrad()

        self.assertTrue(asyncio.run(telegram_authorization.is_account_authorized("Konrad")))
        self.now += telegram_authorization.POSITIVE_CACHE_SECONDS + 1
        self.assertTrue(asyncio.run(telegram_authorization.is_account_authorized("Konrad")))

        self.assertEqual(len(self.session_check.calls), 2)

    def test_invalidate_forces_a_fresh_check(self):
        self.add_default_and_konrad()
        self.assertTrue(asyncio.run(telegram_authorization.is_account_authorized("Konrad")))

        # Session is revoked on Telegram's side. The cached positive still answers until invalidated.
        self.session_check.authorized_paths.discard(self.konrad_session)
        self.assertTrue(asyncio.run(telegram_authorization.is_account_authorized("Konrad")))
        self.assertEqual(len(self.session_check.calls), 1)

        telegram_authorization.invalidate("Konrad")
        self.assertFalse(asyncio.run(telegram_authorization.is_account_authorized("Konrad")))
        self.assertEqual(len(self.session_check.calls), 2)

    def test_probe_error_is_not_cached_as_not_authorized(self):
        self.add_default_and_konrad()
        failing = MagicMock(side_effect=RuntimeError("database is locked"))
        with patch.object(telegram_authorization, "is_session_authorized", failing):
            self.assertFalse(asyncio.run(telegram_authorization.is_account_authorized("Konrad")))

        # The error must not hide a working account for the whole negative TTL.
        working = AsyncMock(return_value=True)
        with patch.object(telegram_authorization, "is_session_authorized", working):
            self.assertTrue(asyncio.run(telegram_authorization.is_account_authorized("Konrad")))
        working.assert_awaited_once()


class RouterAuthorizationTests(AuthorizationFixtureMixin, unittest.TestCase):
    def test_select_chooses_authorized_konrad_when_default_is_unauthorized(self):
        self.add_default_and_konrad()
        router = TelegramAccountRouter()

        self.assertEqual(asyncio.run(router.select_eligible_account("-100123")), "Konrad")
        self.assertEqual(asyncio.run(router.select_eligible_account_general()), "Konrad")

    def test_select_raises_when_no_account_is_authorized(self):
        self.add_default_and_konrad()
        self.session_check.authorized_paths.clear()
        router = TelegramAccountRouter()

        with self.assertRaises(RuntimeError) as channel_ctx:
            asyncio.run(router.select_eligible_account("-100123"))
        self.assertIn("no authorized Telegram account is available", str(channel_ctx.exception))

        with self.assertRaises(RuntimeError) as general_ctx:
            asyncio.run(router.select_eligible_account_general())
        self.assertIn("no authorized Telegram account is available", str(general_ctx.exception))

    def test_router_does_not_recheck_on_every_lease_while_cache_is_fresh(self):
        self.add_default_and_konrad()
        router = TelegramAccountRouter()

        for _ in range(5):
            asyncio.run(router.select_eligible_account_general())

        # One probe per account on the first selection, none on the later four.
        self.assertEqual(sorted(self.session_check.calls), sorted([self.default_session, self.konrad_session]))


class InternalAnyEndpointTests(AuthorizationFixtureMixin, unittest.TestCase):
    def build_client(self, admin_blocked=False):
        mock_admin = SimpleNamespace(
            _admin_blocked=lambda req: admin_blocked,
            _write_token=lambda: "",
            ADMIN_PUBLIC=False,
            JSONResponse=JSONResponse,
        )
        app = FastAPI()
        app.include_router(admin_v2_telegram_auth.build_router(mock_admin))
        return TestClient(app, base_url="http://testserver")

    def test_any_endpoint_reports_authorized_konrad_when_default_is_unauthorized(self):
        self.add_default_and_konrad()

        response = self.build_client().get("/internal/telegram/auth/any")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "authorized": True, "accounts": ["Konrad"]})

    def test_any_endpoint_is_served_when_admin_is_blocked(self):
        self.add_default_and_konrad()

        response = self.build_client(admin_blocked=True).get("/internal/telegram/auth/any")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["authorized"])

    def test_any_endpoint_reports_false_when_no_account_is_authorized(self):
        self.add_default_and_konrad()
        self.session_check.authorized_paths.clear()

        response = self.build_client().get("/internal/telegram/auth/any")

        self.assertEqual(response.json(), {"ok": True, "authorized": False, "accounts": []})


class SyncGateTests(AuthorizationFixtureMixin, unittest.TestCase):
    RESOLVER_URL = "http://resolver:8090"

    def _response(self, payload: bytes):
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = payload
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def test_sync_gate_uses_any_endpoint_answer(self):
        sync_module = _load_sync_module()

        requested = []

        def fake_urlopen(req, timeout=None):
            requested.append(req.full_url)
            return self._response(b'{"ok": true, "authorized": true, "accounts": ["Konrad"]}')

        with patch.dict(os.environ, {"RESOLVER_API_URL": self.RESOLVER_URL}), \
                patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertTrue(sync_module.is_telegram_authorized())

        self.assertEqual(requested, [f"{self.RESOLVER_URL}/admin/internal/telegram/auth/any"])

    def test_sync_gate_false_answer_from_any_endpoint_is_used_without_fallback(self):
        sync_module = _load_sync_module()

        requested = []

        def fake_urlopen(req, timeout=None):
            requested.append(req.full_url)
            return self._response(b'{"ok": true, "authorized": false, "accounts": []}')

        with patch.dict(os.environ, {"RESOLVER_API_URL": self.RESOLVER_URL}), \
                patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertFalse(sync_module.is_telegram_authorized())

        self.assertEqual(requested, [f"{self.RESOLVER_URL}/admin/internal/telegram/auth/any"])

    def test_sync_gate_falls_back_to_status_when_any_endpoint_fails(self):
        sync_module = _load_sync_module()

        def fake_urlopen(req, timeout=None):
            if req.full_url.endswith("/auth/any"):
                raise urllib.error.URLError("refused")
            return self._response(b'{"ok": true, "authorized": true}')

        with patch.dict(os.environ, {"RESOLVER_API_URL": self.RESOLVER_URL}), \
                patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertEqual(sync_module._check_resolver_auth(self.RESOLVER_URL), True)

    def test_sync_gate_fails_closed_when_every_endpoint_fails(self):
        sync_module = _load_sync_module()

        with patch.dict(os.environ, {"RESOLVER_API_URL": self.RESOLVER_URL, "TELEGRAM_SESSION_PATH": os.path.join(self.tmp.name, "missing.session")}), \
                patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            self.assertIsNone(sync_module._check_resolver_auth(self.RESOLVER_URL))
            self.assertFalse(sync_module.is_telegram_authorized())


if __name__ == "__main__":
    unittest.main()
