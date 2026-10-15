"""The root-level duplicate tools remain usable compatibility entry points."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class TestRootCliWrappers(unittest.TestCase):
    def test_duplicate_tool_wrappers_show_help_from_another_working_directory(self):
        with tempfile.TemporaryDirectory(prefix="adult_hub_cli_") as cwd:
            for script in ("find_dupes.py", "merge_dupes.py", "merge_performer_dupes.py", "backup_db.py", "restore_db.py", "migrate_db.py"):
                with self.subTest(script=script):
                    result = subprocess.run(
                        [sys.executable, str(ROOT / script), "--help"],
                        cwd=cwd,
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("usage:", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
