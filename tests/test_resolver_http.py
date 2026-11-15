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

from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

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
            patches.append(patch("resolver.providers.get_provider", get_provider))
        if ensure_registered is not _UNSET:
            patches.append(patch("resolver.providers.ensure_config_providers_registered", ensure_registered))
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
        stream_cache = sys.modules["resolver.stream_cache"]
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
