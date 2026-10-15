import unittest

from resolver.admin_v2_audits import _result, _safe_import_state


class TestAdminV2Audits(unittest.TestCase):
    def test_import_check_status_is_whitelisted_and_result_is_sanitized(self):
        for status in ("checked", "partial", "unknown"):
            with self.subTest(status=status):
                self.assertEqual(_safe_import_state({"jellyfin_import_state": status}), status)
        for value in ("secret", {"status": "checked"}, None, ["checked"]):
            with self.subTest(value=value):
                self.assertIsNone(_safe_import_state({"jellyfin_import_state": value}))
        self.assertEqual(_result('{"jellyfin_import_state":"checked","token":"secret"}')
                         ["jellyfin_import_state"], "checked")
        self.assertEqual(_result('{"jellyfin_import_state":"unknown"}')
                         ["jellyfin_import_state"], "unknown")
        self.assertEqual(_result('{invalid'), {})


if __name__ == "__main__":
    unittest.main()
