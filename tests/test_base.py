import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).parent.parent / "resolver"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from providers.base import parse_duration, _to_base, unpack_packer
except ImportError as exc:
    from test_support import optional_import_failed
    optional_import_failed(exc)
    parse_duration = None
    _to_base = None
    unpack_packer = None

class TestBaseProviderUtils(unittest.TestCase):

    def setUp(self):
        if parse_duration is None:
            self.skipTest("providers module not available in this environment")

    def test_parse_duration_colon_formats(self):
        self.assertEqual(parse_duration("01:23:45"), 5025)
        self.assertEqual(parse_duration("12:34"), 754)
        self.assertEqual(parse_duration("45"), 45)
        self.assertEqual(parse_duration("00:00:00"), 0)
        self.assertEqual(parse_duration(""), 0)
        self.assertEqual(parse_duration(None), 0)

    def test_parse_duration_text_formats(self):
        self.assertEqual(parse_duration("15 min"), 900)
        self.assertEqual(parse_duration("30 sec"), 30)
        self.assertEqual(parse_duration("10 min 25 sec"), 625)

    def test_to_base(self):
        self.assertEqual(_to_base(0, 10), "0")
        self.assertEqual(_to_base(15, 16), "f")
        self.assertEqual(_to_base(61, 62), "Z")
        self.assertEqual(_to_base(62, 62), "10")

    def test_unpack_packer(self):
        packed = "eval(function(p,a,c,k,e,d){return p}('0 1',2,2,'hello|world'.split('|')))"
        unpacked = unpack_packer(packed)
        self.assertIsNotNone(unpacked)
        self.assertIn("hello world", unpacked)

    # NOTE (ETAP D): the Exampleweb category test moved to the private suite
    # (private-web-checkout/tests/test_base_web.py) with its provider.


if __name__ == "__main__":
    unittest.main()
