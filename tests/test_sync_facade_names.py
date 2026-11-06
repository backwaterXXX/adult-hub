"""Every ``_core.<name>`` used by sync_orchestration must exist on the sync facade.

sync_orchestration reads settings and helpers through ``_core``, which is the
``sync`` module. Those names are only resolved at call time, so a missing
re-export fails at runtime, not at import. This test catches that statically.
"""

import ast
import unittest
from pathlib import Path

from test_support import load_flat_sync_module

SYNC_DIR = Path(__file__).resolve().parents[1] / "sync"


class SyncFacadeNamesTest(unittest.TestCase):
    def test_orchestration_core_references_resolve_on_sync_facade(self):
        tree = ast.parse((SYNC_DIR / "sync_orchestration.py").read_text(encoding="utf-8"))
        names = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "_core"
        }
        self.assertTrue(names)

        facade = load_flat_sync_module("sync_facade_names_check")
        missing = sorted(name for name in names if not hasattr(facade, name))

        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
