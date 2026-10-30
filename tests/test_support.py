"""Shared test profile controls, fixtures, and checkout paths."""

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_PROFILE = os.getenv("ADULT_HUB_TEST_PROFILE", "full").strip().lower()


def load_json_fixture(*parts: str):
    """Read a checked-in JSON fixture relative to tests/fixtures."""
    fixture_path = REPO_ROOT / "tests" / "fixtures" / Path(*parts)
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def load_flat_sync_module(alias: str = "sync_flat") -> ModuleType:
    """Load sync/sync.py the way the sync image runs it, without claiming `sync`.

    sync.py imports its siblings flat (category_mapping, http_client, …), so the
    sync directory must be importable. Putting it in front of the repository root
    and importing it as `sync` replaces the `sync` package in sys.modules with a
    plain module, and every later `from sync.video_visual import …` in the suite
    then fails with "'sync' is not a package". The directory is appended instead
    and the file is registered under an alias, so patch targets name the alias.
    """
    loaded = sys.modules.get(alias)
    if isinstance(loaded, ModuleType):
        return loaded
    sync_dir = REPO_ROOT / "sync"
    if str(sync_dir) not in sys.path:
        sys.path.append(str(sync_dir))
    spec = importlib.util.spec_from_file_location(alias, sync_dir / "sync.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"sync.py is not importable from {sync_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def optional_import_failed(error: ImportError) -> None:
    """Only permit missing app modules in an explicitly partial image suite."""
    if TEST_PROFILE != "partial":
        raise error


def flat_sync_binding():
    """Bind sync.py to the name `sync` for the duration of one test or block.

    Modules of the sync package reach their siblings with `import sync as S`
    (sync/duplicate_jobs, sync/merge_performer_dupes, common/fuzzy_merge), which the
    image resolves to sync.py because nothing is a package there. In the checkout the
    name belongs to the sync package, pinned by tests/pg_helper.py, so those imports
    would silently fail without this binding. It is scoped because a process-wide
    binding is the very conflict pg_helper avoids.
    """
    return patch.dict(sys.modules, {"sync": load_flat_sync_module()})
