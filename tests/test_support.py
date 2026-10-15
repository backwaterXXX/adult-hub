"""Shared test profile controls, fixtures, and checkout paths."""

import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_PROFILE = os.getenv("ADULT_HUB_TEST_PROFILE", "full").strip().lower()


def load_json_fixture(*parts: str):
    """Read a checked-in JSON fixture relative to tests/fixtures."""
    fixture_path = REPO_ROOT / "tests" / "fixtures" / Path(*parts)
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def optional_import_failed(error: ImportError) -> None:
    """Only permit missing app modules in an explicitly partial image suite."""
    if TEST_PROFILE != "partial":
        raise error
