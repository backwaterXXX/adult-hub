"""PostgreSQL test helper (hard switch: no SQLite in tests).

Reads ``TEST_DATABASE_URL`` from the environment (or from ``../.env`` for
local runs), initializes the full hub schema, and truncates all tables
between tests. Every DB-touching test module should use this.
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))

# sync/ holds the package (`sync.video_visual`, …) and sync.py, the flat entry
# point the sync image runs under the name `sync`. With that directory on sys.path
# the file wins the name for any fresh import, and whichever import happens first
# owns it for the whole process: later `from sync.video_visual import …` then fails
# with "'sync' is not a package". Pin the package here, with the directory hidden
# from the search, so the two identities never collide. Flat consumers get sync.py
# under an alias from test_support.load_flat_sync_module.
_SYNC_PACKAGE = str(ROOT / "sync")
if getattr(sys.modules.get("sync"), "__file__", None) == str(Path(_SYNC_PACKAGE) / "sync.py"):
    del sys.modules["sync"]
_search_path = sys.path[:]
sys.path[:] = [entry for entry in _search_path if entry != _SYNC_PACKAGE]
try:
    import sync  # noqa: F401
finally:
    sys.path[:] = _search_path

from common import db


def _load_dotenv() -> None:
    if os.getenv("TEST_DATABASE_URL"):
        return
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in ("TEST_DATABASE_URL", "DATABASE_URL"):
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

TEST_URL = os.getenv("TEST_DATABASE_URL", "")
if (os.getenv("ADULT_HUB_REQUIRE_TEST_DATABASE", "").strip().lower() in {"1", "true", "yes"}
        and not TEST_URL):
    raise RuntimeError(
        "ADULT_HUB_REQUIRE_TEST_DATABASE is enabled, but TEST_DATABASE_URL is not configured; "
        "set it to a disposable PostgreSQL database (do not use DATABASE_URL)."
    )


def require_url() -> str:
    if not TEST_URL:
        raise unittest.SkipTest("TEST_DATABASE_URL is not configured")
    return TEST_URL


# A distinct session-level advisory lock (the migration lock is 0xAD01780B).
TEST_RUN_LOCK_ID = 0xAD017BE0
# Test modules import this file both as `pg_helper` and `tests.pg_helper`, so
# the holder registry lives on builtins; a module-level global would let the
# two copies fight over the same advisory lock inside one process.
_REGISTRY_KEY = "_adulthub_test_run_locks"


def _registry() -> dict:
    import builtins
    holder = getattr(builtins, _REGISTRY_KEY, None)
    if holder is None:
        holder = {}
        setattr(builtins, _REGISTRY_KEY, holder)
    return holder


def _acquire_run_guard(url: str) -> None:
    """Refuse a second test process sharing one TEST_DATABASE_URL.

    Parallel suites truncate and deadlock each other's tables; a clear
    message here beats hours of debugging cross-run failures.
    """
    registry = _registry()
    if url in registry:
        return
    conn = db.connect(url)
    conn.autocommit = True
    row = conn.execute("SELECT pg_try_advisory_lock(%s)", (TEST_RUN_LOCK_ID,)).fetchone()
    if not row or not bool(row[0]):
        conn.close()
        raise RuntimeError(
            "Another test process already holds this TEST_DATABASE_URL. "
            "Run one test suite at a time per database."
        )
    registry[url] = conn
    import atexit
    atexit.register(_release_run_guard)


def _release_run_guard() -> None:
    for url, conn in list(_registry().items()):
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (TEST_RUN_LOCK_ID,))
            conn.close()
        except Exception:
            pass
        del _registry()[url]


from common.migrations import APPLICATION_TABLES, apply_migrations

_TABLES = list(APPLICATION_TABLES)


def reset_db(url: str | None = None) -> None:
    """TRUNCATE all hub tables (fast, keeps schema)."""
    conn = db.connect(url or require_url())
    try:
        with conn:
            conn.execute(
                "TRUNCATE " + ", ".join(_TABLES) + " RESTART IDENTITY CASCADE")
    finally:
        conn.close()


def init_test_db(url: str | None = None):
    """Create/upgrade schema in the test database and return a connection."""
    target_url = url or require_url()
    apply_migrations(target_url)
    reset_db(target_url)
    return db.connect(target_url)


class PgTestCase(unittest.TestCase):
    """Base class: fresh schema handle per test, truncated tables."""

    @classmethod
    def setUpClass(cls):
        cls.db_url = require_url()
        _acquire_run_guard(cls.db_url)
        initial_connection = init_test_db(cls.db_url)
        initial_connection.close()

    def setUp(self):
        reset_db(self.db_url)
        self.conn = db.connect(self.db_url)
        # Most DB tests focus on unrelated catalog behavior. Give those
        # fixtures a valid, known duration when they omit the field; tests
        # covering missing/short durations should pass those values explicitly.
        with self.conn:
            self.conn.execute("ALTER TABLE videos ALTER COLUMN duration SET DEFAULT 600")
        self.addCleanup(self.conn.close)
