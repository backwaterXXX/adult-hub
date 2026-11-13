"""The header's active-task count must reach its own handler, not the job detail route."""
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import resolver.admin as admin


class JobCountsRouteTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(admin.router)
        self.client = TestClient(app, client=("127.0.0.1", 5000))

    def test_status_job_counts_is_served_by_counts_handler(self):
        counts = {"active_total": 3, "available": True}
        with patch("resolver.admin_services.job_counts", return_value=counts) as job_counts:
            response = self.client.get("/admin/api/v2/status/job-counts")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), counts)
        job_counts.assert_called_once()


if __name__ == "__main__":
    unittest.main()
