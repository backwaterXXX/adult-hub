"""Unit tests for tools.backfill_telegram_file_sizes."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import httpx
import tools.backfill_telegram_file_sizes as bfs


class BackfillTelegramFileSizesTest(unittest.TestCase):
    def test_default_resolver_url(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(bfs._default_resolver_url(), "http://127.0.0.1:8090")
        with patch.dict("os.environ", {"RESOLVER_URL": "http://example.com:8090/"}):
            self.assertEqual(bfs._default_resolver_url(), "http://example.com:8090")
        with patch.dict("os.environ", {"RESOLVER_INTERNAL_URL": "http://internal:8090"}):
            self.assertEqual(bfs._default_resolver_url(), "http://internal:8090")

    def test_fetch_missing_size_video_ids(self):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("telegram:1",), ("telegram:2",)]

        ids = bfs._fetch_missing_size_video_ids(mock_conn, limit=10)
        self.assertEqual(ids, ["telegram:1", "telegram:2"])
        mock_conn.execute.assert_called_once()
        self.assertIn("LIMIT 10", mock_conn.execute.call_args[0][0])

    def test_backfill_empty(self):
        mock_conn = MagicMock()
        with patch.object(bfs, "_fetch_missing_size_video_ids", return_value=[]):
            checked, updated, failed = bfs.backfill_telegram_file_sizes(mock_conn)
        self.assertEqual((checked, updated, failed), (0, 0, 0))

    def test_backfill_success(self):
        mock_conn = MagicMock()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "variants": [{"is_original": True, "file_size": 12345678}],
        }
        mock_client.get.return_value = mock_resp

        with patch.object(bfs, "_fetch_missing_size_video_ids", return_value=["telegram:chan:1:100"]), \
             patch("tools.backfill_telegram_file_sizes.save_db_file_size", return_value=True) as mock_save:
            checked, updated, failed = bfs.backfill_telegram_file_sizes(
                mock_conn,
                limit=1,
                delay_sec=0,
                client=mock_client,
            )

        self.assertEqual((checked, updated, failed), (1, 1, 0))
        mock_save.assert_called_once_with("chan:1:100", 12345678)

    def test_backfill_unavailable_variant_is_not_a_failure(self):
        mock_conn = MagicMock()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_client.get.return_value = mock_resp

        with patch.object(bfs, "_fetch_missing_size_video_ids", return_value=["telegram:chan:1:100"]), \
             patch("tools.backfill_telegram_file_sizes.save_db_file_size") as mock_save:
            checked, updated, failed = bfs.backfill_telegram_file_sizes(
                mock_conn,
                limit=1,
                delay_sec=0,
                client=mock_client,
            )

        self.assertEqual((checked, updated, failed), (1, 0, 0))
        mock_save.assert_not_called()

    def test_backfill_server_error_counts_as_failure(self):
        mock_conn = MagicMock()
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client.get.return_value = mock_resp

        with patch.object(bfs, "_fetch_missing_size_video_ids", return_value=["telegram:chan:1:100"]), \
             patch("tools.backfill_telegram_file_sizes.save_db_file_size") as mock_save:
            checked, updated, failed = bfs.backfill_telegram_file_sizes(
                mock_conn,
                limit=1,
                delay_sec=0,
                client=mock_client,
            )

        self.assertEqual((checked, updated, failed), (1, 0, 1))
        mock_save.assert_not_called()

    def test_backfill_exception(self):
        mock_conn = MagicMock()
        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")

        with patch.object(bfs, "_fetch_missing_size_video_ids", return_value=["telegram:chan:1:100"]):
            checked, updated, failed = bfs.backfill_telegram_file_sizes(
                mock_conn,
                limit=1,
                delay_sec=0,
                client=mock_client,
            )

        self.assertEqual((checked, updated, failed), (1, 0, 1))


if __name__ == "__main__":
    unittest.main()
