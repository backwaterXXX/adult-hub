import json
import gzip
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

from scripts.admin_classic_usage_report import summarize


class TestAdminClassicUsageReport(unittest.TestCase):
    def test_shared_resolver_and_client_routes_are_not_counted_as_classic_ui_usage(self):
        report = summarize([
            '2026-10-10 10:00:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "GET /admin/api/category/private-channel?page=2 HTTP/1.1" 200 OK',
            '2026-10-10 10:00:01,000 [INFO] uvicorn.access: 10.0.0.4:22 - "GET /admin/api/telegram/history/-100123456?offset_id=secret HTTP/1.1" 200 OK',
            '2026-10-10 10:00:02,000 [INFO] uvicorn.access: 10.0.0.4:22 - "GET /admin/api/v1/video-phash/status?token=private HTTP/1.1" 200 OK',
            '2026-10-10 10:00:03,000 [INFO] uvicorn.access: 10.0.0.4:22 - "GET /admin/api/jobs/job-private-3 HTTP/1.1" 200 OK',
        ])

        self.assertEqual(report["observed_requests"], 1)
        self.assertEqual(report["unmapped_classic_api_requests"], 0)
        self.assertEqual(report["counts"][0]["surface"], "GET /api/jobs/{job_id}")
        self.assertEqual(report["non_classic_api_requests"], 3)
        non_classic = {row["surface"]: row["count"]
                       for row in report["non_classic_api_counts"]}
        self.assertEqual(non_classic, {
            "shared_resolver GET /api/category/{category_id}": 1,
            "shared_resolver GET /api/telegram/history/{channel_id}": 1,
            "client_integration GET /api/v1/video-phash/status": 1,
        })
        serialized = json.dumps(report)
        for private_value in ("10.0.0.4", "private-channel", "-100123456",
                              "offset_id=secret", "token=private", "job-private-3"):
            self.assertNotIn(private_value, serialized)

    def test_matches_legacy_route_templates_and_omits_request_identifiers(self):
        lines = [
            '2026-10-07 12:00:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "GET /admin/classic/?next=private HTTP/1.1" 200 OK',
            '2026-10-07 12:01:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "POST /admin/api/jobs/job-secret-123/retry?title=private HTTP/1.1" 202 Accepted',
            '2026-10-07 12:02:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "GET /admin/api/jobs/job-secret-123 HTTP/1.1" 500 Internal Server Error',
            '2026-10-07 12:03:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "GET /admin/api/jobs/nested/private HTTP/1.1" 404 Not Found',
            '2026-10-07 12:04:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "GET /admin/assets/admin.js HTTP/1.1" 200 OK',
            '2026-10-07 12:05:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "GET /admin/api/v2/jobs HTTP/1.1" 200 OK',
            '2026-10-07 12:06:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "GET /admin/v2/ HTTP/1.1" 200 OK',
        ]

        report = summarize(lines)
        self.assertEqual(report["observed_requests"], 5)
        self.assertEqual(report["unmapped_classic_api_requests"], 1)
        by_surface = {(row["surface"], row["status_class"]): row["count"]
                      for row in report["counts"]}
        self.assertEqual(by_surface[("classic_page", "2xx")], 1)
        self.assertEqual(by_surface[("classic_asset", "2xx")], 1)
        self.assertEqual(by_surface[("POST /api/jobs/{job_id}/retry", "2xx")], 1)
        self.assertEqual(by_surface[("GET /api/jobs/{job_id}", "5xx")], 1)
        self.assertEqual(by_surface[("unmapped_classic_api /api/jobs/{segment}/{segment}", "4xx")], 1)
        serialized = json.dumps(report)
        for private_value in ("10.0.0.4", "job-secret-123", "private", "nested"):
            self.assertNotIn(private_value, serialized)

    def test_unmapped_api_shapes_distinguish_known_routes_and_hide_unknown_segments(self):
        report = summarize([
            '2026-10-07 12:00:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "POST /admin/api/performers/fuzzy/secret-actor?token=hidden HTTP/1.1" 202 Accepted',
            '2026-10-07 12:01:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "POST /admin/api/videos/private-title/refresh HTTP/1.1" 200 OK',
        ])
        surfaces = {row["surface"] for row in report["counts"]}
        self.assertEqual(surfaces, {
            "unmapped_classic_api /api/performers/fuzzy/{segment}",
            "unmapped_classic_api /api/videos/{segment}/{segment}",
        })
        self.assertEqual(report["unmapped_classic_api_requests"], 2)
        serialized = json.dumps(report)
        for private_value in ("secret-actor", "private-title", "token=hidden", "10.0.0.4"):
            self.assertNotIn(private_value, serialized)

    def test_unmapped_route_keeps_only_static_words_valid_at_that_path_position(self):
        report = summarize([
            '2026-10-07 12:00:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "POST /admin/api/videos/private-title/refresh HTTP/1.1" 200 OK',
            '2026-10-07 12:01:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "POST /admin/api/jellyfin/refresh/private-id HTTP/1.1" 200 OK',
        ])
        surfaces = {row["surface"] for row in report["counts"]}
        self.assertEqual(surfaces, {
            "unmapped_classic_api /api/videos/{segment}/{segment}",
            "unmapped_classic_api /api/jellyfin/refresh/{segment}",
        })
        serialized = json.dumps(report)
        for private_value in ("private-title", "private-id", "10.0.0.4"):
            self.assertNotIn(private_value, serialized)

    def test_unknown_timestamp_is_aggregated_without_path_or_query(self):
        report = summarize([
            'INFO: 127.0.0.1:4 - "GET /admin/api/status?token=secret HTTP/1.1" 200 OK',
        ])
        self.assertEqual(report["counts"], [{
            "day": "unknown", "surface": "GET /api/status", "method": "GET",
            "status_class": "2xx", "count": 1,
        }])
        self.assertNotIn("secret", json.dumps(report))

    def test_reports_observed_log_days_without_claiming_complete_coverage(self):
        report = summarize([
            '2026-10-01 12:00:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "GET /admin/classic/ HTTP/1.1" 200 OK',
            '2026-10-03 12:00:00,000 [INFO] uvicorn.access: 10.0.0.4:22 - "GET /admin/api/v2/jobs?token=private HTTP/1.1" 200 OK',
            'INFO: 10.0.0.4:22 - "GET /admin/v2/?session=private HTTP/1.1" 200 OK',
        ])
        self.assertEqual(report["access_log_observation"]["parsed_requests"], 3)
        self.assertEqual(report["access_log_observation"]["days_with_records"], [
            {"day": "2026-10-01", "request_count": 1,
             "first_request_at": "2026-10-01 12:00:00", "last_request_at": "2026-10-01 12:00:00"},
            {"day": "2026-10-03", "request_count": 1,
             "first_request_at": "2026-10-03 12:00:00", "last_request_at": "2026-10-03 12:00:00"},
            {"day": "unknown", "request_count": 1,
             "first_request_at": None, "last_request_at": None},
        ])
        self.assertEqual(report["access_log_observation"]["unknown_timestamp_requests"], 1)
        self.assertIn("not proof of complete retention or uptime",
                      report["access_log_observation"]["note"])
        serialized = json.dumps(report)
        for private_value in ("10.0.0.4", "token=private", "session=private"):
            self.assertNotIn(private_value, serialized)

    def test_parses_rfc3339_timestamp_prefix_from_docker_logs(self):
        report = summarize([
            '2026-10-07T23:02:46.622000000Z INFO: 10.0.0.4:22 - "POST /admin/api/jobs/job-private/retry?token=private HTTP/1.1" 202 Accepted',
        ])
        self.assertEqual(report["counts"], [{
            "day": "2026-10-07", "surface": "POST /api/jobs/{job_id}/retry",
            "method": "POST", "status_class": "2xx", "count": 1,
        }])
        self.assertEqual(report["access_log_observation"]["days_with_records"], [
            {"day": "2026-10-07", "request_count": 1,
             "first_request_at": "2026-10-07T23:02:46Z",
             "last_request_at": "2026-10-07T23:02:46Z"},
        ])
        serialized = json.dumps(report)
        for private_value in ("10.0.0.4", "job-private", "token=private"):
            self.assertNotIn(private_value, serialized)

    def test_reports_first_and_last_observed_access_timestamp_per_day(self):
        report = summarize([
            '2026-10-10T11:45:12Z INFO: 127.0.0.1:4 - "GET /admin/classic/ HTTP/1.1" 200 OK',
            '2026-10-10T08:00:00.123456789Z INFO: 127.0.0.1:4 - "GET /admin/v2/ HTTP/1.1" 200 OK',
        ])
        self.assertEqual(report["access_log_observation"]["days_with_records"], [{
            "day": "2026-10-10", "request_count": 2,
            "first_request_at": "2026-10-10T08:00:00Z",
            "last_request_at": "2026-10-10T11:45:12Z",
        }])

    def test_cli_emits_only_aggregated_json(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/admin_classic_usage_report.py"), "-"],
            input='2026-10-07 12:00:00,000 [INFO] uvicorn.access: 192.0.2.1:4 - "POST /admin/api/sync/trigger?source=hidden HTTP/1.1" 503 Service Unavailable\n',
            text=True, capture_output=True, check=True,
        )
        report = json.loads(result.stdout)
        self.assertEqual(report["observed_requests"], 1)
        self.assertEqual(report["counts"][0]["surface"], "POST /api/sync/trigger")
        for private_value in ("192.0.2.1", "hidden", "source="):
            self.assertNotIn(private_value, result.stdout)

    def test_cli_reads_rotated_gzip_access_logs_without_emitting_private_values(self):
        line = ('2026-10-07T23:02:46.622000000Z INFO: 10.0.0.4:22 - '
                '"GET /admin/api/jobs/job-private-777?token=secret HTTP/1.1" 200 OK\n')
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "resolver-access.log.gz"
            with gzip.open(log_path, "wt", encoding="utf-8") as log_file:
                log_file.write(line)
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/admin_classic_usage_report.py"), str(log_path)],
                text=True, capture_output=True, check=True,
            )
        report = json.loads(result.stdout)
        self.assertEqual(report["counts"], [{
            "day": "2026-10-07", "surface": "GET /api/jobs/{job_id}",
            "method": "GET", "status_class": "2xx", "count": 1,
        }])
        for private_value in ("10.0.0.4", "job-private-777", "token=secret"):
            self.assertNotIn(private_value, result.stdout)


if __name__ == "__main__":
    unittest.main()
