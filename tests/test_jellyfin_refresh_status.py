"""A disabled Jellyfin integration must read as "skipped", never as "failed".

Jellyfin is detached from this deployment: JELLYFIN_URL / JELLYFIN_API_KEY are empty, so
every refresh request is a no-op. Job results used to say ``jellyfin_refresh: failed``
anyway, which made 948 finished jobs look broken.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

from sync import duplicate_jobs

CONFIGURED = {"JELLYFIN_URL": "http://jellyfin:8096", "JELLYFIN_API_KEY": "secret"}
DISABLED = {"JELLYFIN_URL": "", "JELLYFIN_API_KEY": ""}


class JellyfinRefreshStatusTests(unittest.TestCase):
    def test_a_successful_refresh_is_updated(self):
        with patch.dict(os.environ, DISABLED):
            self.assertEqual(duplicate_jobs.jellyfin_refresh_status(True), "updated")

    def test_a_refused_refresh_on_a_configured_integration_is_failed(self):
        with patch.dict(os.environ, CONFIGURED):
            self.assertEqual(duplicate_jobs.jellyfin_refresh_status(False), "failed")

    def test_a_refresh_without_an_integration_is_skipped(self):
        with patch.dict(os.environ, DISABLED):
            self.assertEqual(duplicate_jobs.jellyfin_refresh_status(False), "skipped")

    def test_a_half_configured_integration_counts_as_disabled(self):
        with patch.dict(os.environ, {"JELLYFIN_URL": "http://jellyfin:8096", "JELLYFIN_API_KEY": ""}):
            self.assertEqual(duplicate_jobs.jellyfin_refresh_status(False), "skipped")


class RefreshProgressTests(unittest.TestCase):
    def test_progress_records_skipped_and_never_calls_the_trigger(self):
        recorded = []
        services = MagicMock()

        with patch.dict(os.environ, DISABLED), patch.object(
            duplicate_jobs, "_record_job_progress",
            lambda conn, job_id, stage, **fields: recorded.append((job_id, stage, fields)),
        ):
            refreshed = duplicate_jobs._refresh_jellyfin_with_progress(
                object(), "job-1", "hidden", services,
            )

        self.assertFalse(refreshed)
        self.assertEqual(recorded[0][1], "refreshing_jellyfin")
        self.assertEqual(recorded[0][2]["jellyfin_refresh"], "skipped")
        services.trigger_jellyfin_refresh.assert_not_called()

    def test_progress_requests_the_refresh_when_configured(self):
        recorded = []
        services = MagicMock()
        services.trigger_jellyfin_refresh.return_value = True

        with patch.dict(os.environ, CONFIGURED), patch.object(
            duplicate_jobs, "_record_job_progress",
            lambda conn, job_id, stage, **fields: recorded.append((job_id, stage, fields)),
        ):
            refreshed = duplicate_jobs._refresh_jellyfin_with_progress(
                object(), "job-2", "hidden", services,
            )

        self.assertTrue(refreshed)
        self.assertEqual(recorded[0][2]["jellyfin_refresh"], "pending")
        services.trigger_jellyfin_refresh.assert_called_once()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
