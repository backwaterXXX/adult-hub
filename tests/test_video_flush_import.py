"""video_flush and video_persistence import each other, so both import orders must work.

The check runs each import order in a fresh interpreter from the repository root.
"""

import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class VideoFlushImportOrderTest(unittest.TestCase):
    def _import_in_fresh_process(self, statement: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", statement],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_flush_module_imports_first(self):
        result = self._import_in_fresh_process("import sync.video_flush; import sync.video_persistence")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_persistence_module_imports_first(self):
        result = self._import_in_fresh_process("import sync.video_persistence; import sync.video_flush")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
