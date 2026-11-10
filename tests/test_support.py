"""Shared test profile controls, fixtures, and checkout paths."""

import json
import os
from importlib import import_module
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_PROFILE = os.getenv("ADULT_HUB_TEST_PROFILE", "full").strip().lower()


def load_json_fixture(*parts: str):
    """Read a checked-in JSON fixture relative to tests/fixtures."""
    fixture_path = REPO_ROOT / "tests" / "fixtures" / Path(*parts)
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def load_sync_module() -> ModuleType:
    """Return sync/sync.py, the Telegram sync entry module, as ``sync.sync``.

    The module is imported from the repository root like every other package
    module, so patch targets name ``sync.sync``.
    """
    return import_module("sync.sync")


def optional_import_failed(error: ImportError) -> None:
    """Only permit missing app modules in an explicitly partial image suite."""
    if TEST_PROFILE != "partial":
        raise error
