"""The chained history cleanup is the only place retired auth rows are pruned."""

from __future__ import annotations

import unittest
from unittest import mock

from sync import duplicate_job_external_handlers as handlers


class HistoryCleanupActionTest(unittest.TestCase):
    def test_prunes_history_and_retired_auth_rows(self):
        history_counts = {"history_rows": 7}
        auth_counts = {"pruned_rate_limits": 3, "pruned_pairing_sessions": 2,
                       "pruned_sessions": 1}
        with mock.patch.object(handlers._core, "_record_job_progress") as progress, \
             mock.patch("common.history_retention.prune_history", return_value=history_counts) as prune_history, \
             mock.patch("common.db.table_names", return_value=["client_auth_rate_limits"]), \
             mock.patch("common.client_auth.service.prune_retired_auth_data",
                        return_value=auth_counts) as prune_auth:
            result, _wake = handlers._action_history_cleanup(
                mock.Mock(), "job-1", {}, None, None)

        progress.assert_called_once()
        prune_history.assert_called_once()
        prune_auth.assert_called_once()
        self.assertEqual(result["history_rows_removed"], history_counts)
        self.assertEqual(result["auth_rows_removed"], auth_counts)
        self.assertEqual(result["local_state"], "history_cleanup_completed")

    def test_a_schema_without_auth_tables_still_completes_the_cleanup(self):
        conn = mock.Mock()
        with mock.patch.object(handlers._core, "_record_job_progress"), \
             mock.patch("common.history_retention.prune_history", return_value={"history_rows": 3}), \
             mock.patch("common.db.table_names", return_value=["videos"]), \
             mock.patch("common.client_auth.service.prune_retired_auth_data") as prune_auth:
            result, _wake = handlers._action_history_cleanup(conn, "job-2", {}, None, None)

        prune_auth.assert_not_called()
        self.assertEqual(result["auth_rows_removed"], {})
        self.assertEqual(result["history_rows_removed"], {"history_rows": 3})


if __name__ == "__main__":
    unittest.main()
