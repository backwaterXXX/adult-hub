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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import db  # noqa: E402


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
        initial_connection = init_test_db(cls.db_url)
        initial_connection.close()

    def setUp(self):
        reset_db(self.db_url)
        self.conn = db.connect(self.db_url)
        self.addCleanup(self.conn.close)
