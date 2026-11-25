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

    def test_auth_pruning_failure_does_not_hide_the_history_result(self):
        backing = mock.Mock(rowcount=0)
        conn = mock.Mock()
        conn.execute.return_value = backing
        with mock.patch.object(handlers._core, "_record_job_progress"), \
             mock.patch("common.history_retention.prune_history", return_value={"history_rows": 0}), \
             mock.patch("common.client_auth.service.prune_retired_auth_data",
                        side_effect=RuntimeError("auth tables missing")):
            with self.assertRaises(RuntimeError):
                handlers._action_history_cleanup(conn, "job-2", {}, None, None)


if __name__ == "__main__":
    unittest.main()
