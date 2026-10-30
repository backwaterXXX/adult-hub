import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent))

try:
    from test_support import load_flat_sync_module
    sync = load_flat_sync_module()
    sanitize_filename = sync.sanitize_filename
    format_category_name = sync.format_category_name
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    sanitize_filename = None

class TestParsers(unittest.TestCase):

    def setUp(self):
        if sanitize_filename is None:
            self.skipTest("sync module not available in this environment")

    def test_sanitize_filename(self):
        self.assertEqual(sanitize_filename("Valid Name"), "Valid Name")
        self.assertEqual(sanitize_filename("Illegal: / \\ * ? < > | Chars"), "Illegal Chars")
        self.assertEqual(sanitize_filename("   Multiple    Spaces   "), "Multiple Spaces")
        long_name = "A" * 200
        self.assertEqual(len(sanitize_filename(long_name)), 120)

    def test_format_category_name(self):
        self.assertEqual(format_category_name("anal-9ft75"), "Anal")
        self.assertEqual(format_category_name("big-dick-dljhx"), "Big Dick")
        self.assertEqual(format_category_name("flip-fuck"), "Flip Fuck")
        self.assertEqual(format_category_name("solo"), "Solo")

    # System is Telegram-only.

if __name__ == "__main__":
    unittest.main()
