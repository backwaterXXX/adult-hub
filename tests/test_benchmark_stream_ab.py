"""Safety contracts for the mutating playback benchmark setup."""

import json
import os
import unittest
from unittest.mock import Mock, patch

from tools import benchmark_stream_ab as benchmark


class BenchmarkDiagnosticsTests(unittest.TestCase):
    def test_diagnostics_api_uses_supplied_admin_write_token(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"ok": True}).encode()
        with patch.dict(os.environ, {"ADMIN_WRITE_TOKEN": "local-test-token"}), \
                patch.object(benchmark.urllib.request, "urlopen", return_value=response) as urlopen:
            result = benchmark.diag_api("POST", "/admin/api/v2/diagnostics/playback/enable")

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url,
                         "http://127.0.0.1:8090/admin/api/v2/diagnostics/playback/enable")
        self.assertEqual(request.get_header("Authorization"), "Bearer local-test-token")
        self.assertEqual(result, {"ok": True})

    def test_mutating_setup_uses_v2_and_does_not_measure_on_status_failure(self):
        with patch.object(benchmark, "recreate_resolver", return_value=(True, {})), \
                patch.object(benchmark, "diag_api", return_value={"_error": "HTTPError"}) as api, \
                patch.object(benchmark, "cmd_measure") as measure:
            result = benchmark.main([
                "--allow-mutate", "--video-id", "telegram:test", "--total", "10",
            ])

        self.assertEqual(result, 1)
        api.assert_called_once_with("GET", "/admin/api/v2/diagnostics/playback/status")
        measure.assert_not_called()

    def test_mutating_setup_requires_zero_active_count_and_successful_enable(self):
        responses = [
            {"ok": True, "enabled": False, "active_count": 0},
            {"ok": True, "enabled": True, "enabled_until": "2026-10-12T10:00:00Z"},
        ]
        with patch.object(benchmark, "recreate_resolver", return_value=(True, {})), \
                patch.object(benchmark, "diag_api", side_effect=responses) as api, \
                patch.object(benchmark, "cmd_measure", return_value=0) as measure:
            result = benchmark.main([
                "--allow-mutate", "--video-id", "telegram:test", "--total", "10",
            ])

        self.assertEqual(result, 0)
        self.assertEqual([call.args for call in api.call_args_list], [
            ("GET", "/admin/api/v2/diagnostics/playback/status"),
            ("POST", "/admin/api/v2/diagnostics/playback/enable"),
        ])
        measure.assert_called_once()


if __name__ == "__main__":
    unittest.main()
