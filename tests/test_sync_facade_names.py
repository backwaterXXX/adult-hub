"""Every ``_core.<name>`` used by a sync module must exist on the module it binds.

Several sync modules bind ``_core`` to a facade: ``sync_orchestration`` to ``sync``,
the ``duplicate_job_*`` modules to ``duplicate_jobs``, the ``scene_scraper_*`` modules
to ``scene_scrapers`` and ``pbc_backfill`` to ``pbc``. Those names are only resolved
at call time, so a missing re-export fails at runtime, not at import. This test
catches that statically for every file.
"""

import ast
import importlib
import unittest
from pathlib import Path

SYNC_DIR = Path(__file__).resolve().parents[1] / "sync"


def _core_binding(tree: ast.Module) -> str | None:
    """Return the module name that ``_core`` is imported from, if any."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.asname == "_core":
                    return alias.name
    return None


def _core_references(tree: ast.Module) -> set[str]:
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "_core"
    }


def _load_target(name: str):
    return importlib.import_module(f"sync.{name}")


class SyncFacadeNamesTest(unittest.TestCase):
    def test_core_references_resolve_on_bound_module(self):
        checked_files = []
        missing = {}
        for path in sorted(SYNC_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            target_name = _core_binding(tree)
            if target_name is None:
                continue
            names = _core_references(tree)
            if not names:
                continue
            target = _load_target(target_name)
            absent = sorted(name for name in names if not hasattr(target, name))
            if absent:
                missing[path.name] = absent
            checked_files.append(path.name)

        self.assertIn("sync_orchestration.py", checked_files)
        self.assertIn("duplicate_job_worker.py", checked_files)
        self.assertEqual(missing, {})


if __name__ == "__main__":
    unittest.main()
