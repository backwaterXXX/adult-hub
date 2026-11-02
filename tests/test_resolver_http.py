"""Request-level coverage for the resolver application, without a database.

Every test execs its own copy of ``resolver/app.py`` and replaces the
collaborators that would otherwise reach PostgreSQL, DNS or an upstream CDN
with in-process stand-ins, so the HTTP surface is exercised for real while
nothing leaves the test process. ``DATABASE_URL`` is blanked for the whole
test so that even a forgotten stub cannot open a real connection.
"""

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
import socket as socket_module
import sys
import types
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RESOLVER_DIR = ROOT / "resolver"
for _entry in (str(RESOLVER_DIR), str(ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from fastapi.testclient import TestClient
import httpx

from common.config_loader import ConfigError

MODULE_PATH = RESOLVER_DIR / "app.py"
CONFIG_FIXTURE = ROOT / "tests" / "fixtures" / "config_telegram_only.yaml"
ASSET = "admin-v2.css"
PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "10.0.0.8"
_UNSET = object()
QUIET = ("resolver", "httpx", "httpcore", "common.config_loader", "admin", "providers")


def source_config(**addresses):
    """A loaded-config stand-in: provider name -> address (or None)."""
    return types.SimpleNamespace(
        sources={name: types.SimpleNamespace(address=address) for name, address in addresses.items()}
    )


def validation_error():
    return RequestValidationError([{"type": "pattern", "loc": ("query", "mode"),
                                    "msg": "string does not match", "input": "tee",
                                    "ctx": {"pattern": "redirect|proxy"}}])


class LogCapture(logging.Handler):
    """Every message the copied application logs, kept per level."""

    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append((record.levelname, record.getMessage()))

    def text(self, level):
        return [message for name, message in self.messages if name == level]

    def blob(self, level):
        return "\n".join(self.text(level))

    @property
    def logger(self):
        logger = logging.Logger("resolver")
        logger.parent = None
        logger.propagate = False
        logger.setLevel(logging.DEBUG)
        logger.addHandler(self)
        return logger


class FakeSocket:
    """DNS answers on demand, so no test resolves a real name."""

    def __init__(self, addresses=(PUBLIC_IP,), error=None):
        self.addresses = list(addresses)
        self.error = error
        self.lookups = []

    def __getattr__(self, name):
        return getattr(socket_module, name)

    def getaddrinfo(self, host, port, **_kwargs):
        self.lookups.append((host, port))
        if self.error is not None:
            raise self.error
        return [(0, 0, 0, "", (address, port)) for address in self.addresses]


class Headers(dict):
    """The case-insensitive slice of header behaviour the resolver uses."""

    def __init__(self, values=None):
        super().__init__({name.lower(): value for name, value in (values or {}).items()})

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def get(self, key, default=None):
        return super().get(key.lower(), default)

    def __contains__(self, key):
        return super().__contains__(key.lower())


class FakeResponse:
    """One scripted streaming response from an upstream CDN."""

    def __init__(self, status_code=200, headers=None, chunks=(), error=None):
        self.status_code = status_code
        self.headers = Headers(headers)
        self.chunks = list(chunks)
        self.error = error

    async def aiter_bytes(self, chunk_size=None):
        if self.error is not None:
            raise self.error
        for chunk in self.chunks:
            yield chunk


class FakeStreamContext:
    """What ``client.stream(...)`` hands back: an async context over a response."""

    def __init__(self, answer, exit_error=None):
        self.answer = answer
        self.exit_error = exit_error
        self.exits = 0

    async def __aenter__(self):
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    async def __aexit__(self, *_exc):
        self.exits += 1
        if self.exit_error is not None:
            raise self.exit_error
        return None


class FakeUpstream:
    """The shared httpx client, answering from a queue of scripted responses."""

    def __init__(self, *answers, exit_error=None):
        self.answers = list(answers)
        self.exit_error = exit_error
        self.requests = []
        self.contexts = []

    def stream(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if len(self.answers) > 1:
            answer = self.answers.pop(0)
        elif self.answers:
            answer = self.answers[0]
        else:
            answer = FakeResponse()
        context = FakeStreamContext(answer, exit_error=self.exit_error)
        self.contexts.append(context)
        return context

    def urls(self):
        return [request[1] for request in self.requests]

    def kwargs(self, index=0):
        return self.requests[index][2]

    def method(self, index=0):
        return self.requests[index][0]

    def header(self, name, index=0):
        return self.requests[index][2].get("headers", {}).get(name)


class FakeCache:
    """The stream cache without its PostgreSQL half."""

    def __init__(self, entries=None, gets=None, length=0):
        self.entries = dict(entries or {})
        self.gets = list(gets) if gets is not None else None
        self.reads = []
        self.writes = []
        self.invalidated = []
        self.closes = 0
        self.length = length

    def get(self, key):
        self.reads.append(key)
        if self.gets is not None:
            if not self.gets:
                return None
            return self.gets.pop(0) if len(self.gets) > 1 else self.gets[0]
        entry = self.entries.get(key)
        return entry[0] if isinstance(entry, tuple) else entry

    def set(self, key, value, ttl_seconds=None):
        self.writes.append((key, value, ttl_seconds))
        self.entries[key] = value

    def invalidate(self, key):
        self.invalidated.append(key)
        self.entries.pop(key, None)

    def clear(self):
        self.closes += 1
        return len(self.entries)

    def close(self):
        self.closes += 1

    def __len__(self):
        return self.length


class BoundStreams:
    """A semaphore stand-in that is not tied to one event loop."""

    def __init__(self):
        self.holds = 0
        self.high_water = 0

    async def __aenter__(self):
        self.holds += 1
        self.high_water = max(self.high_water, self.holds)
        return self

    async def __aexit__(self, *_exc):
        self.holds -= 1
        return None


class Awaitable:
    """A close() result that is awaitable without owning a Telegram socket."""

    def __init__(self):
        self.awaited = 0

    def __await__(self):
        self.awaited += 1
        return asyncio.sleep(0).__await__()


class Provider:
    """A provider that resolves to a fixed stream URL, or refuses to."""

    def __init__(self, stream="https://cdn.example.test/video.mp4", authorized=True, error=None):
        self.stream = stream
        self.authorized = authorized
        self.error = error
        self.resolved = []
        self.authorisation_checks = []

    def is_channel_authorized(self, channel_ref):
        self.authorisation_checks.append(channel_ref)
        return self.authorized

    async def resolve_stream(self, provider_video_id):
        self.resolved.append(provider_video_id)
        if self.error is not None:
            raise self.error
        return self.stream


class ClosableProvider(Provider):
    """A provider exposing the optional ``close()`` used during shutdown."""

    def __init__(self, *args, close_result=None, error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.close_result = close_result
        self.close_error = error
        self.closed = 0

    def close(self):
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error
        return self.close_result


class RecordingLiveness:
    """Answers the cached-URL liveness probe from a scripted queue."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.checked = []

    async def __call__(self, url):
        self.checked.append(url)
        if not self.answers:
            return True
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConnection:
    """Answers the resolver's catalog queries and records every statement."""

    def __init__(self, answers, error=None):
        self.answers = list(answers)
        self.error = error
        self.statements = []
        self.closed = False
        self.commit_count = 0
        self.rollback_count = 0

    def execute(self, sql, params=None):
        statement = " ".join(sql.split())
        self.statements.append((statement, params))
        if self.error is not None:
            raise self.error
        for needle, rows in self.answers:
            if needle in statement:
                return Cursor(rows)
        return Cursor([])

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def close(self):
        self.closed = True


class FakeDb:
    """The ``common.db`` surface the resolver uses, with no server behind it."""

    def __init__(self, *, answers=(), tables=(), columns=None, error=None):
        self.answers = list(answers)
        self.tables = set(tables)
        self.columns = dict(columns or {})
        self.error = error
        self.urls = []
        self.connections = []

    def connect(self, url=None, **_kwargs):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        connection = FakeConnection(self.answers)
        self.connections.append(connection)
        return connection

    def table_names(self, _conn):
        return set(self.tables)

    def table_columns(self, _conn, table):
        return list(self.columns.get(table, []))


class FakeAdmin:
    """The handful of admin helpers the resolver's own endpoints call."""

    def __init__(self, *, blocked=False, sync_status=None, history=(), history_error=None,
                 db_url="postgresql://hub.invalid/adulthub"):
        self.blocked = blocked
        self.sync_status = {} if sync_status is None else sync_status
        self.history = list(history)
        self.history_error = history_error
        self.history_limits = []
        self.HUB_DB_URL = db_url

    def _admin_blocked(self, _request):
        return self.blocked

    def _sync_status(self):
        return self.sync_status

    def _sync_history(self, limit):
        self.history_limits.append(limit)
        if self.history_error is not None:
            raise self.history_error
        return {"runs": list(self.history)}


class FakeMetrics:
    """The Prometheus recorder, reduced to what the tests assert on."""

    def __init__(self, rendered="# HELP adult_hub_http requests\n"):
        self.rendered = rendered
        self.recorded = []

    def record_request(self, method, route, status, duration, *, stream=False):
        self.recorded.append((method, route, status, stream))

    def render(self):
        return self.rendered


@contextlib.contextmanager
def quiet_logging():
    """Keep the application's own logging out of the test output."""
    saved = {}
    for name in QUIET:
        logger = logging.getLogger(name)
        saved[name] = (logger.level, logger.propagate)
        logger.setLevel(logging.CRITICAL)
        logger.propagate = False
    try:
        yield
    finally:
        for name, (level, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.propagate = propagate


async def _drain(response):
    return b"".join([chunk async for chunk in response.body_iterator])


class ResolverHarness(unittest.TestCase):
    """One freshly executed copy of ``resolver/app.py`` per test."""

    maxDiff = None

    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(quiet_logging())
        self.stack.enter_context(patch.dict(os.environ, {"DATABASE_URL": ""}))
        self.logs = LogCapture()

    def load(self, *, env=None, config=_UNSET, config_error=None, get_provider=_UNSET,
             ensure_registered=_UNSET):
        environment = {"DATABASE_URL": "", "CONFIG_PATH": str(CONFIG_FIXTURE)}
        environment.update(env or {})
        patches = [patch.dict(os.environ, environment),
                   patch("common.logging_setup.configure_logging", lambda _name: self.logs.logger)]
        if config is not _UNSET or config_error is not None:
            patches.append(patch("common.config_loader.load_config",
                                 side_effect=[config_error] if config_error else [config]))
        if get_provider is not _UNSET:
            patches.append(patch("providers.get_provider", get_provider))
        if ensure_registered is not _UNSET:
            patches.append(patch("providers.ensure_config_providers_registered", ensure_registered))
        with contextlib.ExitStack() as import_stack:
            # The app still registers its shutdown hook through the deprecated
            # on_event decorator; that is a production concern, not a test one.
            import_stack.enter_context(warnings.catch_warnings())
            warnings.simplefilter("ignore", DeprecationWarning)
            for entry in patches:
                import_stack.enter_context(entry)
            spec = importlib.util.spec_from_file_location("resolver_app_under_test", MODULE_PATH)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        module.socket = FakeSocket()
        module._stream_semaphore = BoundStreams()
        return module

    def client(self, module, *, raise_server_exceptions=True):
        return TestClient(module.app, raise_server_exceptions=raise_server_exceptions)

    def get(self, module, path, **kwargs):
        return self.client(module).get(path, **kwargs)

    def head(self, module, path, **kwargs):
        return self.client(module).head(path, **kwargs)

    def complete(self, coroutine):
        return asyncio.run(coroutine)

    def use_upstream(self, module, *answers, exit_error=None):
        module.upstream = FakeUpstream(*answers, exit_error=exit_error)
        shared = module.upstream

        async def get_shared_client():
            return shared

        async def aclose_shared_client():
            return None

        module.get_shared_client = get_shared_client
        module.aclose_shared_client = aclose_shared_client
        return shared

    def use_database(self, module, **kwargs):
        module.db = FakeDb(**kwargs)
        return module.db

    def use_admin(self, module, **kwargs):
        module.admin_module = FakeAdmin(**kwargs)
        return module.admin_module

    def use_metrics(self, module, rendered="# HELP adult_hub_http requests\n"):
        module.resolver_metrics = FakeMetrics(rendered)
        return module.resolver_metrics

    def use_cache(self, module, **kwargs):
        module.cache = FakeCache(**kwargs)
        return module.cache

    def use_persistent_cache(self, module, *, entries=7):
        """Install the real PostgreSQL-backed cache, which probes the server.

        ``len(cache)`` opens a connection while the persistent store is live, so
        a health route that touches it cannot hide behind a stubbed cache.
        """
        stream_cache = sys.modules["stream_cache"]
        database = FakeDb(answers=[("count(*) FROM stream_cache", [(entries,)])])
        self.stack.enter_context(patch.object(stream_cache, "db", database))
        cache = stream_cache.PersistentLRUCache(db_url="postgresql://hub.invalid/adulthub")
        self.assertTrue(cache._pg_ok, "the test cache must stay database-backed")
        database.urls.clear()
        module.cache = cache
        return database

    def use_providers(self, module, **by_name):
        registry = dict(by_name)
        module.get_provider = lambda name: registry[name]
        module.list_providers = lambda: dict(registry)
        return registry

    def single_candidate(self, module):
        module._get_stream_candidate_ids = lambda video_id: [video_id]


class TestStartup(ResolverHarness):
    def test_a_configured_source_address_reaches_its_provider(self):
        self.addresses = []
        provider = types.SimpleNamespace(
            set_address=lambda value: self.addresses.append(value))
        config = source_config(telegram="https://tg.example", idle=None)
        registered = []

        module = self.load(config=config, get_provider=lambda name: provider,
                           ensure_registered=lambda loaded: registered.append(loaded))

        self.assertEqual(self.addresses, ["https://tg.example"])
        self.assertIs(module.app_config, config)
        self.assertEqual(registered, [config])
        self.assertIn("Initialized provider 'telegram' with address https://tg.example",
                      self.logs.text("INFO"))

    def test_an_address_for_an_unlisted_provider_is_left_alone(self):
        def raise_key_error(_name):
            raise KeyError("Provider 'telegram' not found. Available: []")

        registered = []
        module = self.load(config=source_config(telegram="https://tg.example"),
                           get_provider=raise_key_error,
                           ensure_registered=lambda loaded: registered.append(loaded))

        self.assertEqual(registered, [module.app_config])

    def test_a_broken_config_file_stops_the_process(self):
        with self.assertRaises(SystemExit):
            self.load(config_error=ConfigError("sources: required"))

        self.assertEqual(self.logs.text("CRITICAL"), ["FATAL CONFIG ERROR: sources: required"])

    def test_a_provider_that_refuses_to_start_is_re_raised(self):
        with self.assertRaisesRegex(RuntimeError, "no credentials"):
            self.load(config_error=RuntimeError("no credentials"))

        self.assertEqual(self.logs.text("CRITICAL"), ["FATAL PROVIDER ERROR: no credentials"])

    def test_an_exit_requested_by_the_loader_survives(self):
        with self.assertRaises(SystemExit) as raised:
            self.load(config_error=SystemExit(3))

        self.assertEqual(raised.exception.code, 3)

    def test_an_unexpected_loader_failure_becomes_exit_code_one(self):
        with self.assertRaises(SystemExit) as raised:
            self.load(config_error=KeyError("unexpected"))

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("configuration loader failed unexpectedly", self.logs.blob("CRITICAL"))

    def test_the_concurrency_limit_comes_from_the_environment(self):
        module = self.load(env={"RESOLVER_CONCURRENCY_LIMIT": "3"})

        self.assertEqual(module.RESOLVER_CONCURRENCY_LIMIT, 3)

    def test_the_default_concurrency_limit_is_ten(self):
        module = self.load()

        self.assertEqual(module.RESOLVER_CONCURRENCY_LIMIT, 10)


class TestShutdown(ResolverHarness):
    def test_shutdown_closes_the_client_the_provider_and_the_cache(self):
        module = self.load()
        awaitable = Awaitable()
        provider = ClosableProvider(close_result=awaitable)
        cache = self.use_cache(module)
        module.aclose_shared_client = lambda: asyncio.sleep(0)
        module.get_provider = lambda name: provider

        self.complete(module._on_shutdown())

        self.assertEqual(provider.closed, 1)
        self.assertEqual(awaitable.awaited, 1)
        self.assertEqual(cache.closes, 1)

    def test_a_provider_without_close_is_simply_skipped(self):
        module = self.load()
        cache = self.use_cache(module)
        module.aclose_shared_client = lambda: asyncio.sleep(0)
        module.get_provider = lambda name: Provider()

        self.complete(module._on_shutdown())

        self.assertEqual(cache.closes, 1)

    def test_a_plain_provider_close_is_not_awaited(self):
        module = self.load()
        provider = ClosableProvider(close_result=None)
        module.aclose_shared_client = lambda: asyncio.sleep(0)
        module.get_provider = lambda name: provider
        self.use_cache(module)

        self.complete(module._on_shutdown())

        self.assertEqual(provider.closed, 1)

    def test_a_refused_provider_close_is_ignored(self):
        module = self.load()
        provider = ClosableProvider(error=OSError("close refused"))
        module.aclose_shared_client = lambda: asyncio.sleep(0)
        module.get_provider = lambda name: provider
        self.use_cache(module)

        self.complete(module._on_shutdown())

    def test_a_shutdown_that_fails_everywhere_still_completes(self):
        module = self.load()

        async def explode():
            raise OSError("nothing shuts down")

        def missing(_name):
            raise KeyError("Provider 'telegram' not found")

        def refuse_close():
            raise OSError("the cache refuses to close")

        module.aclose_shared_client = explode
        module.get_provider = missing
        module.cache = self.use_cache(module)
        module.cache.close = refuse_close

        self.complete(module._on_shutdown())


class TestAdminSurface(ResolverHarness):
    def test_admin_assets_are_never_cached(self):
        module = self.load()

        response = self.get(module, f"/admin/assets-v2/{ASSET}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_a_v2_api_answer_is_marked_uncached(self):
        module = self.load()

        response = self.get(module, "/admin/api/v2/not-here")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_a_v1_answer_keeps_its_own_caching(self):
        module = self.load()
        self.use_cache(module)

        response = self.get(module, "/admin/health")

        self.assertNotIn("no-store", response.headers.get("cache-control", ""))

    def test_a_v2_parameter_error_uses_the_v2_envelope(self):
        module = self.load()
        request = types.SimpleNamespace(url=types.SimpleNamespace(path="/admin/api/v2/videos"))

        response = self.complete(module.handle_request_validation_error(request, validation_error()))

        self.assertEqual(response.status_code, 400)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["message"], "Invalid request parameters.")
        self.assertFalse(body["ok"])

    def test_a_v1_parameter_error_keeps_the_standard_answer(self):
        module = self.load()
        request = types.SimpleNamespace(url=types.SimpleNamespace(path="/admin/play/x/stream"))

        response = self.complete(module.handle_request_validation_error(request, validation_error()))

        self.assertEqual(response.status_code, 422)

    def test_an_unknown_playback_mode_is_rejected_over_http(self):
        module = self.load()

        response = self.get(module, "/admin/play/telegram:1/stream?mode=tee")

        self.assertEqual(response.status_code, 422)

    def test_every_request_is_recorded_for_prometheus(self):
        module = self.load()
        metrics = self.use_metrics(module)
        self.use_cache(module)

        self.get(module, "/admin/health")
        self.get(module, "/not-a-route")

        self.assertEqual(metrics.recorded, [("GET", "/admin/health", 200, False),
                                            ("GET", "unmatched", 404, False)])

    def test_a_playback_request_is_recorded_as_a_stream(self):
        module = self.load()
        metrics = self.use_metrics(module)
        self.use_cache(module)
        self.use_providers(module, telegram=Provider(stream="tg://-1001:42"))
        self.single_candidate(module)

        async def proxy(_request, url, cache_key=None):
            return Response(content="streamed", media_type="video/mp4")

        module.proxy_fetch_response = proxy
        self.get(module, "/admin/play/telegram:-1001:42/stream")

        self.assertEqual(metrics.recorded,
                         [("GET", "/admin/play/{video_id:path}/stream", 200, True)])

    def test_a_failing_route_is_recorded_as_a_server_error(self):
        module = self.load()
        metrics = self.use_metrics(module)

        def explode():
            raise ZeroDivisionError("no health for you")

        module._provider_health = explode
        response = self.client(module, raise_server_exceptions=False).get("/admin/health")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(metrics.recorded, [("GET", "/admin/health", 500, False)])

    def test_the_admin_hooks_report_resolver_state(self):
        module = self.load()
        cache = self.use_cache(module, entries={"telegram:1": "tg://x"}, length=4)

        self.assertEqual(module._admin_extra()["cache_entries"], 4)
        self.assertEqual(module._admin_cache_clear(), 1)
        self.assertEqual(cache.closes, 1)


class TestHealthEndpoints(ResolverHarness):
    def test_health_describes_the_running_resolver(self):
        module = self.load()
        self.use_cache(module, length=2)

        response = self.get(module, "/admin/health")

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["cache_entries"], 2)
        self.assertIn("telegram", body["providers"])

    def test_liveness_never_opens_the_database(self):
        module = self.load()
        cache_database = self.use_persistent_cache(module)
        self.use_database(module, error=RuntimeError("database is down"))

        response = self.get(module, "/admin/health/live")

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(cache_database.urls, [])
        self.assertNotIn("cache_entries", body)

    def test_the_persisted_cache_size_is_still_reported_elsewhere(self):
        module = self.load()
        cache_database = self.use_persistent_cache(module, entries=7)
        self.use_database(module, answers=[("SELECT 1", [(1,)])])
        self.use_admin(module)

        health = self.get(module, "/admin/health").json()
        ready = self.get(module, "/admin/health/ready").json()

        self.assertEqual(health["cache_entries"], 7)
        self.assertEqual(ready["cache_entries"], 7)
        self.assertEqual(ready["database"], "ok")
        self.assertEqual(len(cache_database.urls), 2)

    def test_readiness_checks_the_shared_catalog(self):
        module = self.load()
        self.use_cache(module)
        database = self.use_database(module, answers=[("SELECT 1", [(1,)])])
        admin = self.use_admin(module)

        response = self.get(module, "/admin/health/ready")

        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["database"], "ok")
        self.assertEqual(database.urls, [admin.HUB_DB_URL])
        self.assertEqual(database.connections[0].statements[0][0], "SELECT 1")
        self.assertTrue(database.connections[0].closed)

    def test_readiness_stops_when_the_catalog_is_unreachable(self):
        module = self.load()
        self.use_cache(module)
        self.use_database(module, error=RuntimeError("connection refused"))
        self.use_admin(module)

        response = self.get(module, "/admin/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "not_ready", "database": "unavailable"})


class TestMetricsEndpoint(ResolverHarness):
    def test_metrics_are_hidden_from_a_blocked_client(self):
        module = self.load()
        self.use_admin(module, blocked=True)

        response = self.get(module, "/metrics")

        self.assertEqual(response.status_code, 404)

    def test_metrics_carry_the_sync_and_job_gauges(self):
        module = self.load()
        self.use_metrics(module)
        self.use_admin(module, sync_status={"running": True, "last_success": False,
                                            "updated_at": 1700000000.5},
                       history=[{"duration_seconds": 12.5}])
        database = self.use_database(module, answers=[(
            "FROM video_duplicate_jobs",
            [("metadata_update", "done", 3), ("hide", "queued", "2"),
             ("not_an_action", "done", 5), ("sync", "exploded", 1)])])
        admin = module.admin_module
        response = self.client(module).get("/metrics")

        self.assertEqual(response.headers["content-type"], "text/plain; version=0.0.4; charset=utf-8")
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertIn("# HELP adult_hub_http requests", body)
        self.assertIn("adult_hub_sync_running 1", body)
        self.assertIn("adult_hub_sync_last_success 0", body)
        self.assertIn("adult_hub_sync_status_updated_timestamp_seconds 1700000000.500", body)
        self.assertIn("adult_hub_sync_last_duration_seconds 12.500", body)
        self.assertIn('adult_hub_jobs{action="metadata_update",state="done"} 3', body)
        self.assertIn('adult_hub_jobs{action="hide",state="queued"} 2', body)
        self.assertNotIn("not_an_action", body)
        self.assertNotIn('action="sync",state="exploded"', body)
        self.assertIn("adult_hub_jobs_database_available 1", body)
        self.assertEqual(admin.history_limits, [1])
        self.assertEqual(database.urls, [admin.HUB_DB_URL])

    def test_optional_sync_gauges_appear_only_when_meaningful(self):
        module = self.load()
        self.use_metrics(module)
        self.use_admin(module, sync_status={"running": False, "last_success": None,
                                            "updated_at": "never"},
                       history_error=RuntimeError("no history"))
        self.use_database(module, answers=[("FROM video_duplicate_jobs", [])])

        body = self.get(module, "/metrics").text

        self.assertIn("adult_hub_sync_running 0", body)
        self.assertNotIn("adult_hub_sync_last_success", body)
        self.assertNotIn("adult_hub_sync_status_updated_timestamp_seconds", body)
        self.assertNotIn("adult_hub_sync_last_duration_seconds", body)
        self.assertIn("adult_hub_jobs_database_available 1", body)

    def test_a_run_without_a_duration_has_no_duration_gauge(self):
        module = self.load()
        self.use_metrics(module)
        self.use_admin(module, sync_status={"running": True}, history=[{"state": "failed"}])
        self.use_database(module, answers=[("FROM video_duplicate_jobs", [])])

        body = self.get(module, "/metrics").text

        self.assertNotIn("adult_hub_sync_last_duration_seconds", body)
        self.assertNotIn("adult_hub_sync_last_success", body)
        self.assertNotIn("adult_hub_sync_status_updated_timestamp_seconds", body)

    def test_a_broken_job_table_is_reported_instead_of_raising(self):
        module = self.load()
        self.use_metrics(module)
        self.use_admin(module)
        self.use_database(module, error=RuntimeError("jobs table is gone"))

        body = self.get(module, "/metrics").text

        self.assertIn("adult_hub_jobs_database_available 0", body)
        self.assertNotIn("adult_hub_jobs{", body)


class TestPerformerBio(ResolverHarness):
    def performer(self, module, name="Bunny"):
        return self.get(module, f"/admin/api/performer-bio?name={name}")

    def test_a_blank_name_has_no_bio(self):
        module = self.load()

        response = self.performer(module, name="")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Missing performer name")

    def test_a_stored_performer_answers_with_every_source(self):
        module = self.load()
        database = self.use_database(
            module,
            tables={"performer_bios", "performer_aliases", "performer_photos"},
            columns={"performer_bios": ["name", "bio", "birth", "aliases", "updated_at",
                                        "nationality"]},
            answers=[
                ("SELECT canonical FROM performer_aliases", []),
                ("SELECT bio, birth, aliases",
                 [("A long bio", "1990-01-01", '["Old"]', 1700000000, "PL")]),
                ("SELECT alias FROM performer_aliases", [("Bunny Rae",)]),
                ("SELECT thumb FROM performer_photos", [("https://photos.example/bunny.jpg",)]),
            ])

        bio = self.performer(module).json()

        self.assertEqual(bio["name"], "Bunny")
        self.assertEqual(bio["bio"], "A long bio")
        self.assertEqual(bio["birth"], "1990-01-01")
        self.assertEqual(bio["nationality"], "PL")
        self.assertEqual(bio["aliases"], ["Bunny Rae", "Old"])
        self.assertEqual(bio["photo_url"], "https://photos.example/bunny.jpg")
        self.assertEqual(bio["updated_at"], 1700000000)
        self.assertTrue(all(connection.closed for connection in database.connections))

    def test_an_alias_resolves_to_the_canonical_record(self):
        module = self.load()
        self.use_database(
            module,
            tables={"performer_bios", "performer_aliases"},
            answers=[
                ("SELECT canonical FROM performer_aliases", [("Bunny Rae",)]),
                ("SELECT alias FROM performer_aliases", []),
                ("SELECT bio, birth, aliases", [("Canonised", "", None, None, "")]),
            ])

        bio = self.performer(module, name="bunny").json()

        self.assertEqual(bio["name"], "Bunny Rae")
        self.assertEqual(bio["nationality"], "")
        self.assertEqual(bio["aliases"], [])
        self.assertEqual(bio["photo_url"], "")
        self.assertEqual(bio["updated_at"], 0)

    def test_unreadable_stored_aliases_are_ignored(self):
        module = self.load()
        self.use_database(
            module,
            tables={"performer_bios"},
            answers=[("SELECT bio, birth, aliases", [("bio", "1990", "not json", 1, "PL")])])

        self.assertEqual(self.performer(module).json()["aliases"], [])

    def test_a_number_where_the_alias_list_should_be_is_ignored(self):
        module = self.load()
        self.use_database(
            module,
            tables={"performer_bios"},
            answers=[("SELECT bio, birth, aliases", [("bio", "", "42", 1, "")])])

        self.assertEqual(self.performer(module).json()["aliases"], [])

    def test_a_photo_without_a_bio_row_still_answers(self):
        module = self.load()
        self.use_database(
            module,
            tables={"performer_photos"},
            answers=[("SELECT thumb FROM performer_photos", [("https://photos.example/x.jpg",)])])

        bio = self.performer(module).json()

        self.assertEqual(bio["bio"], "")
        self.assertEqual(bio["photo_url"], "https://photos.example/x.jpg")

    def test_a_performer_without_any_record_is_not_found(self):
        module = self.load()
        self.use_database(module, tables={"performer_photos"},
                          answers=[("SELECT thumb FROM performer_photos", [("",)])])

        response = self.performer(module, name="Nobody")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "No info for performer 'Nobody'")

    def test_a_refused_lookup_stays_the_refusal_it_was(self):
        module = self.load()
        self.use_database(module, error=HTTPException(status_code=404, detail="catalog is gone"))

        response = self.performer(module)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "catalog is gone")

    def test_a_broken_catalog_is_a_bad_gateway(self):
        module = self.load()
        self.use_database(module, error=RuntimeError("password=supersecret host=db.internal"))

        response = self.performer(module)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "HUB DB lookup failed.")
        self.assertNotIn("supersecret", response.text)
        self.assertNotIn("db.internal", response.text)

    def test_admin_profile_data_is_hidden_from_a_blocked_client(self):
        module = self.load()
        self.use_admin(module, blocked=True)
        database = self.use_database(module)

        response = self.performer(module)

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Bunny", response.text)
        self.assertEqual(database.urls, [])


class TestStreamUrlValidation(ResolverHarness):
    def test_a_public_https_address_is_accepted(self):
        module = self.load()

        module.validate_proxy_url("https://CDN.Example.test:8443/video.mp4")

        self.assertEqual(module.socket.lookups[-1], ("cdn.example.test", 8443))

    def test_the_default_port_follows_the_scheme(self):
        module = self.load()

        module.validate_proxy_url("https://cdn.example.test/video.mp4")
        module.validate_proxy_url("http://cdn.example.test/video.mp4")

        self.assertEqual([port for _host, port in module.socket.lookups], [443, 80])

    def test_the_host_is_normalised_before_the_lookup(self):
        module = self.load()

        module.validate_proxy_url("https://cdn.example.test./video.mp4")

        self.assertEqual(module.socket.lookups[-1][0], "cdn.example.test")

    def test_a_local_name_is_refused_without_asking_dns(self):
        module = self.load()

        for refused in ("https://localhost/video.mp4", "https://api.localhost/x",
                        "https://localhost.localdomain/x"):
            with self.subTest(refused=refused):
                with self.assertRaises(HTTPException) as raised:
                    module.validate_proxy_url(refused)
                self.assertEqual(raised.exception.status_code, 400)
                self.assertEqual(raised.exception.detail, "Local stream URLs are not allowed")
            self.assertEqual(module.socket.lookups, [])

    def test_a_non_http_scheme_or_host_is_invalid(self):
        module = self.load()

        for refused in ("", "ftp://cdn.example.test/x", "https:///x",
                        "https://user:pass@cdn.example.test/x"):
            with self.subTest(refused=refused):
                with self.assertRaises(HTTPException) as raised:
                    module.validate_proxy_url(refused)
                self.assertEqual(raised.exception.detail, "Invalid stream URL")

    def test_a_private_or_reserved_address_is_refused(self):
        module = self.load()

        for addresses in ([PRIVATE_IP], ["127.0.0.1"], ["::1"], ["169.254.169.254"], []):
            with self.subTest(addresses=addresses):
                module.socket = FakeSocket(addresses=addresses)
                with self.assertRaises(HTTPException) as raised:
                    module.validate_proxy_url("https://cdn.example.test/x")
                self.assertEqual(raised.exception.detail,
                                 "Non-public stream addresses are not allowed")

    def test_a_link_local_scope_suffix_still_counts_as_private(self):
        module = self.load()
        module.socket = FakeSocket(addresses=["fe80::1%eth0"])

        with self.assertRaises(HTTPException) as raised:
            module.validate_proxy_url("https://cdn.example.test/x")

        self.assertEqual(raised.exception.detail, "Non-public stream addresses are not allowed")

    def test_an_unresolvable_host_is_reported_as_unvalidated(self):
        module = self.load()
        module.socket = FakeSocket(error=OSError("name or service not known"))

        with self.assertRaises(HTTPException) as raised:
            module.validate_proxy_url("https://gone.example/x")

        self.assertEqual(raised.exception.detail, "Could not validate stream host")

    def test_a_nonsense_address_is_reported_as_unvalidated(self):
        module = self.load()
        module.socket = FakeSocket(addresses=["not-an-ip"])

        with self.assertRaises(HTTPException) as raised:
            module.validate_proxy_url("https://cdn.example.test/x")

        self.assertEqual(raised.exception.detail, "Could not validate stream host")

    def test_the_check_runs_off_the_event_loop(self):
        module = self.load()
        module.socket = FakeSocket(addresses=[])

        with self.assertRaises(HTTPException):
            self.complete(module._validate_proxy_url_async("https://cdn.example.test/x"))

        self.assertEqual(module.socket.lookups, [("cdn.example.test", 443)])


class TestSafeProxyStream(ResolverHarness):
    def test_a_plain_answer_is_handed_back_unclosed(self):
        module = self.load()
        upstream = self.use_upstream(module, FakeResponse(200, {"content-type": "video/mp4"}))
        client = self.complete(module.get_shared_client())

        context, response = self.complete(module._safe_proxy_stream(
            client, "https://cdn.example.test/v.mp4", headers={}, timeout=None))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(context.exits, 0)
        self.assertEqual(upstream.kwargs()["follow_redirects"], False)
        self.assertEqual(upstream.method(), "GET")

    def test_a_redirect_target_is_validated_before_it_is_followed(self):
        module = self.load()
        upstream = self.use_upstream(
            module,
            FakeResponse(302, {"location": "/next.mp4"}),
            FakeResponse(200, {"content-type": "video/mp4"}))
        client = self.complete(module.get_shared_client())

        _context, response = self.complete(module._safe_proxy_stream(
            client, "https://cdn.example.test/v.mp4", headers={}, timeout=None))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(upstream.urls(), ["https://cdn.example.test/v.mp4",
                                           "https://cdn.example.test/next.mp4"])
        self.assertEqual(upstream.contexts[0].exits, 1)
        self.assertEqual(module.socket.lookups, [("cdn.example.test", 443)] * 2)

    def test_a_redirect_without_a_target_is_refused(self):
        module = self.load()
        upstream = self.use_upstream(module, FakeResponse(301, {}))
        client = self.complete(module.get_shared_client())

        with self.assertRaises(HTTPException) as raised:
            self.complete(module._safe_proxy_stream(client, "https://cdn.example.test/v.mp4",
                                               headers={}, timeout=None))

        self.assertEqual(raised.exception.detail, "Invalid upstream redirect")
        self.assertEqual(upstream.contexts[0].exits, 1)

    def test_a_redirect_loop_is_cut_after_six_hops(self):
        module = self.load()
        upstream = self.use_upstream(module,
                                     FakeResponse(307, {"location": "https://cdn.example.test/v.mp4"}))
        client = self.complete(module.get_shared_client())

        with self.assertRaises(HTTPException) as raised:
            self.complete(module._safe_proxy_stream(client, "https://cdn.example.test/v.mp4",
                                               headers={}, timeout=None))

        self.assertEqual(raised.exception.detail, "Too many upstream redirects")
        self.assertEqual(len(upstream.requests), 6)

    def test_a_redirect_into_a_private_host_is_refused_before_connecting(self):
        module = self.load()
        upstream = self.use_upstream(module, FakeResponse(302, {"location": "/second.mp4"}),
                                     FakeResponse(200))
        client = self.complete(module.get_shared_client())
        original = module.validate_proxy_url

        def refuse_second(url):
            if url.endswith("/second.mp4"):
                raise HTTPException(status_code=400,
                                    detail="Non-public stream addresses are not allowed")
            return original(url)

        module.validate_proxy_url = refuse_second

        with self.assertRaises(HTTPException) as raised:
            self.complete(module._safe_proxy_stream(client, "https://cdn.example.test/v.mp4",
                                               headers={}, timeout=None))

        self.assertEqual(len(upstream.requests), 1)
        self.assertEqual(raised.exception.status_code, 400)


class TestStreamCandidates(ResolverHarness):
    def test_a_lone_video_is_played_from_itself(self):
        module = self.load()
        self.use_database(module)

        self.assertEqual(module._get_stream_candidate_ids("telegram:-100:1"), ["telegram:-100:1"])

    def test_the_source_table_lists_the_active_sources_in_order(self):
        module = self.load()
        self.use_database(module, tables={"video_sources"}, answers=[
            ("WHERE video_id=%s AND is_active=1",
             [("telegram:-100:1",), ("youtube:abc",), ("telegram:-100:1",), ("",)]),
        ])

        candidates = module._get_stream_candidate_ids("telegram:-100:1")

        self.assertEqual(candidates, ["telegram:-100:1", "youtube:abc"])

    def test_a_source_id_redirects_to_the_canonical_video(self):
        module = self.load()
        database = self.use_database(module, tables={"video_sources", "video_duplicate_decisions"},
                                     answers=[
                ("SELECT video_id FROM video_sources WHERE source_id", [("telegram:-100:9",)]),
                ("WHERE video_id=%s AND is_active=1",
                 [("telegram:-100:9",), ("telegram:-100:8",)]),
            ])

        candidates = module._get_stream_candidate_ids("telegram:-100:1:42")

        self.assertEqual(candidates, ["telegram:-100:9", "telegram:-100:8", "telegram:-100:1:42"])
        self.assertEqual(database.connections[0].statements[1][1], ("telegram:-100:9",))

    def test_a_duplicate_decision_names_the_keeper(self):
        module = self.load()
        self.use_database(module, tables={"video_duplicate_decisions"}, answers=[
            ("SELECT keeper_id FROM video_duplicate_decisions", [("telegram:-100:9",)]),
        ])

        self.assertEqual(module._get_stream_candidate_ids("telegram:-100:1"),
                         ["telegram:-100:9", "telegram:-100:1"])

    def test_an_empty_keeper_row_leaves_the_requested_id(self):
        module = self.load()
        self.use_database(module, tables={"video_duplicate_decisions"}, answers=[
            ("SELECT keeper_id FROM video_duplicate_decisions", [("",)]),
        ])

        self.assertEqual(module._get_stream_candidate_ids("telegram:-100:1"), ["telegram:-100:1"])

    def test_a_source_alias_skips_the_duplicate_ledger(self):
        module = self.load()
        database = self.use_database(module, tables={"video_sources", "video_duplicate_decisions"},
                                     answers=[
                ("SELECT video_id FROM video_sources WHERE source_id", [("telegram:-100:9",)]),
            ])

        module._get_stream_candidate_ids("telegram:-100:1")

        statements = [statement for statement, _ in database.connections[0].statements]
        self.assertEqual(statements[0],
                         "SELECT video_id FROM video_sources "
                         "WHERE source_id=%s AND video_id!=%s AND is_active=1")
        self.assertNotIn("video_duplicate_decisions", "\n".join(statements))

    def test_a_broken_catalog_leaves_the_requested_id(self):
        module = self.load()
        self.use_database(module, error=RuntimeError("catalog is down"))

        self.assertEqual(module._get_stream_candidate_ids("telegram:-100:1"), ["telegram:-100:1"])
        self.assertIn("Source candidate lookup error", self.logs.blob("DEBUG"))


class TestPlayIdParsing(ResolverHarness):
    def test_a_bare_id_is_a_telegram_id(self):
        module = self.load()

        self.assertEqual(module.parse_composite_id(""), ("telegram", ""))
        self.assertEqual(module.parse_composite_id(" plain_id "), ("telegram", "plain_id"))
        self.assertEqual(module.parse_composite_id("telegram:ch:42:uniq"),
                         ("telegram", "ch:42:uniq"))

    def test_an_unknown_prefix_is_a_bad_request(self):
        module = self.load()
        module.list_providers = lambda: {"telegram": None}

        with self.assertRaises(HTTPException) as raised:
            module._parse_play_id_or_400("bogus:1")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("Unknown provider prefix 'bogus'", raised.exception.detail)


class TestPlayStream(ResolverHarness):
    def setUp(self):
        super().setUp()
        self.proxied = []

    def record_proxy(self, module, body="streamed"):
        async def proxy(request, url, cache_key=None):
            self.proxied.append((url, cache_key))
            return Response(content=body, media_type="video/mp4")

        module.proxy_fetch_response = proxy

    def test_a_telegram_reference_is_handed_to_the_range_proxy(self):
        module = self.load()
        provider = Provider(stream="tg://-1001:42")
        self.use_providers(module, telegram=provider)
        self.use_cache(module)
        self.single_candidate(module)
        self.record_proxy(module)

        response = self.get(module, "/admin/play/telegram:-1001:42/stream")

        self.assertEqual(response.content, b"streamed")
        self.assertEqual(self.proxied, [("tg://-1001:42", "telegram:-1001:42")])
        self.assertEqual(provider.authorisation_checks, ["-1001"])
        self.assertEqual(module._stream_semaphore.high_water, 1)

    def test_an_unauthorized_channel_is_forbidden(self):
        module = self.load()
        self.use_providers(module, telegram=Provider(stream="tg://x", authorized=False))
        self.use_cache(module)
        self.single_candidate(module)

        response = self.get(module, "/admin/play/telegram:-1001:42/stream")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "Channel not authorized")

    def test_a_missing_telegram_provider_leaves_no_working_source(self):
        module = self.load()

        def missing(name):
            raise KeyError(name)

        module.get_provider = missing
        module.list_providers = lambda: {}
        self.use_cache(module)
        self.single_candidate(module)

        response = self.get(module, "/admin/play/plain-id/stream")

        self.assertEqual(response.status_code, 502)
        self.assertIn("Failed to resolve stream", response.json()["detail"])

    def test_a_cached_direct_stream_redirects_without_resolving(self):
        module = self.load()
        provider = Provider(stream="https://never.example/x")
        self.use_providers(module, youtube=provider)
        cache = self.use_cache(module, entries={"youtube:abc": ("https://cached.example/x", 0, 0)})
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(True)

        response = self.get(module, "/admin/play/youtube:abc/stream", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://cached.example/x")
        self.assertEqual(module.upstream_url_alive.checked, ["https://cached.example/x"])
        self.assertEqual(cache.reads, ["youtube:abc"])
        self.assertEqual(provider.resolved, [])

    def test_a_cached_telegram_reference_skips_the_liveness_probe(self):
        module = self.load()
        provider = Provider(stream="tg://x")
        self.use_providers(module, telegram=provider)
        self.use_cache(module)
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(False)
        self.record_proxy(module)

        response = self.get(module, "/admin/play/telegram:ch:1/stream")

        self.assertEqual(response.content, b"streamed")
        self.assertEqual(provider.resolved, ["ch:1"])
        self.assertEqual(module.upstream_url_alive.checked, [])
        self.assertEqual(provider.authorisation_checks, ["ch"])
        self.assertIn("Resolved new telegram stream", self.logs.blob("INFO"))

    def test_a_cached_telegram_reference_is_played_without_a_probe(self):
        module = self.load()
        provider = Provider(stream="https://never.example/x")
        self.use_providers(module, youtube=provider)
        self.use_cache(module, entries={"youtube:abc": "tg://-1001:9"})
        self.single_candidate(module)
        liveness = RecordingLiveness(False)
        module.upstream_url_alive = liveness

        async def proxy(_request, url, cache_key=None):
            self.proxied.append((url, cache_key))
            return Response(content="streamed", media_type="video/mp4")

        module.proxy_fetch_response = proxy
        response = self.get(module, "/admin/play/youtube:abc/stream")

        self.assertEqual(response.content, b"streamed")
        self.assertEqual(self.proxied, [("tg://-1001:9", "youtube:abc")])
        self.assertEqual(liveness.checked, [])
        self.assertEqual(provider.resolved, [])

    def test_a_dead_cached_stream_is_dropped_and_re_resolved(self):
        module = self.load()
        provider = Provider(stream="https://fresh.example/x")
        self.use_providers(module, youtube=provider)
        cache = self.use_cache(module, entries={"youtube:abc": ("https://dead.example/x", 0, 0)})
        self.single_candidate(module)
        liveness = RecordingLiveness(False, True)
        module.upstream_url_alive = liveness

        response = self.get(module, "/admin/play/youtube:abc/stream", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://fresh.example/x")
        self.assertEqual(liveness.checked, ["https://dead.example/x", "https://fresh.example/x"])
        self.assertEqual(cache.invalidated, ["youtube:abc"])
        self.assertEqual(cache.writes, [("youtube:abc", "https://fresh.example/x",
                                         module.cache_ttl_for_provider("youtube"))])
        self.assertIn("Cached stream for youtube:abc is dead", self.logs.blob("INFO"))

    def test_an_unanswered_liveness_probe_keeps_the_cached_stream(self):
        module = self.load()
        provider = Provider(stream="https://never.example/x")
        self.use_providers(module, youtube=provider)
        self.use_cache(module, entries={"youtube:abc": ("https://maybe.example/x", 0, 0)})
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(None)

        response = self.get(module, "/admin/play/youtube:abc/stream", follow_redirects=False)

        self.assertEqual(response.headers["location"], "https://maybe.example/x")
        self.assertEqual(provider.resolved, [])

    def test_a_second_look_inside_the_lock_can_find_the_stream(self):
        module = self.load()
        provider = Provider(stream="https://never.example/x")
        self.use_providers(module, youtube=provider)
        cache = self.use_cache(module, gets=[None, "https://raced.example/x"])
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(True)

        response = self.get(module, "/admin/play/youtube:abc/stream", follow_redirects=False)

        self.assertEqual(response.headers["location"], "https://raced.example/x")
        self.assertEqual(cache.reads, ["youtube:abc", "youtube:abc"])
        self.assertEqual(provider.resolved, [])

    def test_a_refused_resolution_is_retried_on_the_next_source(self):
        module = self.load()
        broken = Provider(error=TimeoutError("telegram timed out"))
        working = Provider(stream="https://backup.example/x")
        self.use_providers(module, telegram=broken, youtube=working)
        self.use_cache(module)
        module._get_stream_candidate_ids = lambda video_id: ["telegram:1", "youtube:abc"]
        module.upstream_url_alive = RecordingLiveness(True)

        response = self.get(module, "/admin/play/telegram:1/stream", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "https://backup.example/x")
        self.assertEqual(broken.resolved, ["1"])
        self.assertIn("Multi-source failover active", self.logs.blob("INFO"))
        self.assertIn("Failed to resolve telegram:1", self.logs.blob("WARNING"))

    def test_an_empty_resolution_is_a_failure_not_a_redirect(self):
        module = self.load()
        self.use_providers(module, telegram=Provider(stream=None))
        self.use_cache(module)
        self.single_candidate(module)

        response = self.get(module, "/admin/play/telegram:1/stream")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Failed to resolve stream")
        self.assertIn("Empty stream URL resolved for telegram:1", self.logs.blob("WARNING"))

    def test_a_freshly_resolved_dead_url_is_refused(self):
        module = self.load()
        self.use_providers(module, telegram=Provider(stream="https://gone.example/x"))
        self.use_cache(module)
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(False)

        response = self.get(module, "/admin/play/telegram:1/stream")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Failed to resolve stream")
        self.assertIn("Freshly resolved URL is dead", self.logs.blob("WARNING"))

    def test_an_unparsable_candidate_id_is_skipped(self):
        module = self.load()
        provider = Provider(stream="https://second.example/x")
        self.use_providers(module, telegram=provider)
        self.use_cache(module)
        module._get_stream_candidate_ids = lambda video_id: ["bogus:1", "plain"]
        module.upstream_url_alive = RecordingLiveness(True)

        response = self.get(module, "/admin/play/plain/stream", follow_redirects=False)

        self.assertEqual(response.headers["location"], "https://second.example/x")

    def test_no_candidates_at_all_end_in_a_bad_gateway(self):
        module = self.load()
        self.use_providers(module)
        self.use_cache(module)
        module._get_stream_candidate_ids = lambda video_id: []

        response = self.get(module, "/admin/play/telegram:1/stream")

        self.assertEqual(response.status_code, 502)
        self.assertIn("Failed to resolve stream for telegram:1", self.logs.blob("ERROR"))

    def test_proxy_mode_fetches_the_resolved_url_through_the_app(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(200, {"Content-Type": "video/mp4"}, chunks=[b"video"]))
        provider = Provider(stream="https://cdn.example.test/x.mp4")
        self.use_providers(module, telegram=provider)
        self.use_cache(module)
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(True)

        response = self.get(module, "/admin/play/telegram:1/stream?mode=proxy")

        self.assertEqual(response.content, b"video")
        self.assertEqual(response.headers["content-type"], "video/mp4")
        self.assertEqual(module.upstream.urls(), ["https://cdn.example.test/x.mp4"])

    def test_proxy_mode_refuses_an_unvalidated_resolution(self):
        module = self.load()
        provider = Provider(stream="https://localhost/x.mp4")
        self.use_providers(module, telegram=provider)
        self.use_cache(module)
        self.single_candidate(module)
        module.upstream_url_alive = RecordingLiveness(True)

        response = self.get(module, "/admin/play/telegram:1/stream?mode=proxy")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Local stream URLs are not allowed")

    def test_a_stream_head_request_probes_the_resolution_path(self):
        module = self.load()
        provider = Provider(stream="tg://-1001:42")
        self.use_providers(module, telegram=provider)
        self.use_cache(module)
        self.single_candidate(module)
        self.record_proxy(module)

        response = self.head(module, "/admin/play/telegram:-1001:42/stream")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(provider.resolved, ["-1001:42"])


class TestUpstreamLiveness(ResolverHarness):
    def probe(self, module, status_code):
        self.use_upstream(module, FakeResponse(status_code))
        return self.complete(module.upstream_url_alive("https://cdn.example.test/x.mp4"))

    def test_a_serving_status_counts_as_alive(self):
        module = self.load()

        for status_code in (200, 206, 301, 302, 307, 308):
            with self.subTest(status_code=status_code):
                self.assertIs(self.probe(module, status_code), True)

    def test_a_gone_upstream_counts_as_dead(self):
        module = self.load()

        for status_code in (401, 403, 404, 410, 502, 504):
            with self.subTest(status_code=status_code):
                self.assertIs(self.probe(module, status_code), False)

    def test_an_unexpected_status_leaves_the_answer_unknown(self):
        module = self.load()

        self.assertIsNone(self.probe(module, 500))

    def test_the_probe_reads_one_byte_with_a_browser_agent(self):
        module = self.load()
        upstream = self.use_upstream(module, FakeResponse(206))

        self.assertIs(self.complete(module.upstream_url_alive("https://cdn.example.test/x.mp4")), True)

        kwargs = upstream.kwargs()
        self.assertEqual(kwargs["headers"]["Range"], "bytes=0-0")
        self.assertEqual(kwargs["headers"]["Referer"], "https://cdn.example.test/")
        self.assertTrue(kwargs["headers"]["User-Agent"].startswith("Mozilla/5.0"))
        self.assertEqual(kwargs["follow_redirects"], False)
        self.assertEqual(kwargs["timeout"], 5.0)

    def test_a_network_failure_leaves_the_answer_unknown(self):
        module = self.load()
        self.use_upstream(module, RuntimeError("connection reset"))

        self.assertIsNone(self.complete(module.upstream_url_alive("https://cdn.example.test/x.mp4")))
        self.assertIn("Stream liveness check failed", self.logs.blob("DEBUG"))

    def test_an_unvalidated_address_is_reported_as_unknown(self):
        module = self.load()
        module.socket = FakeSocket(addresses=[PRIVATE_IP])

        self.assertIsNone(self.complete(module.upstream_url_alive("https://cdn.example.test/x.mp4")))


class TestProxyFetch(ResolverHarness):
    def request(self, method="GET", headers=None):
        return types.SimpleNamespace(method=method, headers=Headers(headers))

    def fetch(self, module, url="https://cdn.example.test/x.mp4", cache_key=None, method="GET"):
        return self.complete(module.proxy_fetch_response(self.request(method), url, cache_key))

    def test_a_telegram_reference_uses_the_range_proxy(self):
        module = self.load()
        seen = []

        async def telegram(request, url, cache_key=None, services=None):
            seen.append((url, cache_key))
            return Response(content="telegram", media_type="video/mp4")

        module._proxy_telegram_response_impl = telegram

        response = self.fetch(module, "tg://-1001:42", "k")

        self.assertEqual(response.body, b"telegram")
        self.assertEqual(seen, [("tg://-1001:42", "k")])

    def test_a_playlist_is_refused_before_anything_is_fetched(self):
        module = self.load()
        upstream = self.use_upstream(module)
        module.looks_like_playlist = lambda url: True

        with self.assertRaises(HTTPException) as raised:
            self.fetch(module, "https://cdn.example.test/x.m3u8")

        self.assertEqual(raised.exception.detail, "HLS proxying is not supported")
        self.assertEqual(upstream.requests, [])

    def test_an_upstream_error_becomes_a_bad_gateway(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(404))
        cache = self.use_cache(module)

        with self.assertRaises(HTTPException) as raised:
            self.fetch(module, cache_key="youtube:abc")

        self.assertEqual(raised.exception.detail, "Upstream fetch failed (HTTP 404)")
        self.assertEqual(cache.invalidated, [])
        self.assertEqual(module.upstream.contexts[0].exits, 1)

    def test_a_gateway_error_at_the_edge_drops_the_cached_url(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(504))
        cache = self.use_cache(module)

        with self.assertRaises(HTTPException) as raised:
            self.fetch(module, cache_key="youtube:abc")

        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(cache.invalidated, ["youtube:abc"])

    def test_a_transport_failure_is_reported_as_bad_gateway(self):
        module = self.load()
        self.use_upstream(module, httpx.ConnectError("no route to host"))

        with self.assertRaises(HTTPException) as raised:
            self.fetch(module)

        self.assertIn("Upstream fetch failed", raised.exception.detail)

    def test_a_get_streams_the_body_and_keeps_the_upstream_framing(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(
            206, {"Content-Range": "bytes 0-1/2", "Accept-Ranges": "bytes",
                  "Content-Length": "2", "Content-Type": "video/mp4"},
            chunks=[b"ab", b"cd"]))

        response = self.fetch(module)
        body = self.complete(_drain(response))

        self.assertEqual(body, b"abcd")
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.headers["content-range"], "bytes 0-1/2")
        self.assertEqual(response.headers["accept-ranges"], "bytes")
        self.assertEqual(response.headers["content-length"], "2")
        self.assertEqual(response.media_type, "video/mp4")
        self.assertEqual(module.upstream.method(), "GET")
        self.assertEqual(module.upstream.contexts[0].exits, 1)

    def test_a_missing_media_type_falls_back_to_octet_stream(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(200))

        response = self.fetch(module)

        self.assertEqual(response.media_type, "application/octet-stream")

    def test_the_client_range_is_passed_to_the_upstream(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(206, {"Content-Range": "bytes 0-0/1"}))

        self.complete(module.proxy_fetch_response(self.request(headers={"range": "bytes=0-0"}),
                                             "https://cdn.example.test/x.mp4"))

        self.assertEqual(module.upstream.header("Range"), "bytes=0-0")

    def test_a_request_without_a_range_asks_for_the_whole_stream(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(200))

        self.fetch(module)

        self.assertIsNone(module.upstream.header("Range"))
        self.assertEqual(module.upstream.header("Referer"), "https://cdn.example.test/")

    def test_a_head_request_closes_the_upstream_and_answers_alone(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(
            200, {"Content-Length": "1234", "Content-Type": "video/mp4"}))

        response = self.fetch(module, method="HEAD")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"")
        self.assertEqual(response.headers["content-length"], "1234")
        self.assertEqual(module.upstream.method(), "HEAD")
        self.assertEqual(module.upstream.contexts[0].exits, 1)

    def test_an_upstream_that_refuses_to_close_is_still_a_bad_gateway(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(502), exit_error=OSError("socket is stuck"))
        cache = self.use_cache(module)

        with self.assertRaises(HTTPException) as raised:
            self.fetch(module, cache_key="youtube:abc")

        self.assertEqual(raised.exception.detail, "Upstream fetch failed (HTTP 502)")
        self.assertEqual(cache.invalidated, ["youtube:abc"])
        self.assertEqual(module.upstream.contexts[0].exits, 1)

    def test_a_head_answer_survives_a_stuck_upstream_close(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(200, {"Content-Length": "7"}),
                          exit_error=OSError("socket is stuck"))

        response = self.fetch(module, method="HEAD")

        self.assertEqual(response.headers["content-length"], "7")
        self.assertEqual(module.upstream.contexts[0].exits, 1)

    def test_a_cancelled_download_still_closes_the_upstream(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(200, chunks=[b"ab"],
                                               error=asyncio.CancelledError()))

        response = self.fetch(module)

        with self.assertRaises(asyncio.CancelledError):
            self.complete(_drain(response))
        self.assertEqual(module.upstream.contexts[0].exits, 1)

    def test_a_finished_stream_survives_a_stuck_upstream_close(self):
        module = self.load()
        self.use_upstream(module, FakeResponse(200, chunks=[b"ab", b"cd"]),
                          exit_error=OSError("socket is stuck"))

        response = self.fetch(module)

        self.assertEqual(self.complete(_drain(response)), b"abcd")
        self.assertEqual(module.upstream.contexts[0].exits, 1)


class TestLegacyRedirects(ResolverHarness):
    def test_the_root_moves_to_the_admin_panel(self):
        module = self.load()

        response = self.get(module, "/", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/admin")

    def test_the_old_health_path_moves_under_admin(self):
        module = self.load()

        response = self.get(module, "/health", follow_redirects=False)

        self.assertEqual(response.headers["location"], "/admin/health")

    def test_an_old_api_path_keeps_its_query(self):
        module = self.load()

        response = self.get(module, "/api/videos?page=2", follow_redirects=False)

        self.assertEqual(response.headers["location"], "/admin/api/videos?page=2")

    def test_an_old_play_path_keeps_its_query(self):
        module = self.load()

        response = self.head(module, "/play/telegram:1/stream?mode=proxy", follow_redirects=False)

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/admin/play/telegram:1/stream?mode=proxy")

    def test_a_path_without_a_query_is_redirected_clean(self):
        module = self.load()

        response = self.get(module, "/api/videos", follow_redirects=False)

        self.assertEqual(response.headers["location"], "/admin/api/videos")


if __name__ == "__main__":
    unittest.main()
