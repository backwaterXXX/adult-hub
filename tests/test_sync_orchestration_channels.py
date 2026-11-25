"""Tests for configured and environment Telegram channels, availability reconciliation, and sync gating."""

import os
import sys
import types
import unittest
from unittest.mock import patch

from common.config_loader import SourceConfig
from tests.test_sync_orchestration import (
    CONN,
    DATA_DIR,
    MEDIA_DIR,
    SyncPassHarness,
    sync_orchestration,
)


class TestConfiguredTelegramChannels(SyncPassHarness):
    def test_only_enabled_channels_are_scanned_and_reported(self):
        self.sync()

        self.assertEqual(self.scanned_channels(), ["chan-one", "chan-two"])
        self.assertEqual(sorted(self.rows()), ["telegram:chan-one", "telegram:chan-two"])

    def test_each_channel_is_scanned_in_the_requested_mode(self):
        self.sync(sync_mode="new")

        self.assertEqual(self.sink("sync_telegram_channel_pipeline").every_kwargs(),
                         [{"sync_mode": "new"}] * 2)
        self.assertEqual(self.sink("sync_telegram_channel_pipeline").args(1)[0], "chan-two")
        self.assertIs(self.sink("sync_telegram_channel_pipeline").args(1)[1], self.channels[1])
        self.assertEqual(self.sink("sync_telegram_channel_pipeline").args()[2:4], (CONN, MEDIA_DIR))
        self.assertIs(self.sink("sync_telegram_channel_pipeline").args()[4],
                      self.sink("flush_collected").args()[0])

    def test_the_scanned_channels_share_one_success_gate_with_the_destructive_passes(self):
        self.sync()

        gate = self.sink("rewrite_all_stream_files").args()[2]
        self.assertEqual(gate, {"telegram:chan-one", "telegram:chan-two"})
        self.assertEqual(self.sink("rewrite_all_stream_files").args()[:2], (CONN, MEDIA_DIR))
        self.assertIs(self.sink("cleanup_orphaned_records").args()[2], gate)
        self.assertEqual(self.sink("cleanup_orphaned_records").args()[:2], (MEDIA_DIR, CONN))
        self.assertIs(self.sink("cleanup_retention").args()[3], gate)
        self.assertEqual(self.sink("cleanup_retention").args()[:3], (CONN, MEDIA_DIR, 30))

    def test_a_failing_channel_keeps_its_historical_records_out_of_the_gate(self):
        self.sink("sync_telegram_channel_pipeline").raises = [None, TimeoutError("page fetch timed out")]

        self.assertFalse(self.sync())

        self.assertEqual(self.sink("cleanup_retention").args()[3], {"telegram:chan-one"})
        row = self.rows()["telegram:chan-two"]
        self.assertEqual(row["success"], False)
        self.assertEqual(row["error"], "Sync failed (TimeoutError).")
        self.assertEqual(row["detail"], "safe detail for page fetch timed out")
        self.assertTrue(self.core.logger.has(
            "Telegram sync failed for chan-two: safe detail for page fetch timed out", "error"))
        self.assertTrue(self.core.logger.has(
            "Sync completed with source failures: telegram:chan-two", "error"))

    def test_each_channel_reports_its_own_duration(self):
        self.core.time.step = 0.1234

        self.sync()

        self.assertEqual(self.rows()["telegram:chan-one"]["duration_seconds"], 0.123)
        self.assertEqual(self.rows()["telegram:chan-two"]["duration_seconds"], 0.123)

    def test_a_channel_filter_reaches_only_that_channel(self):
        self.sync(source_filter="telegram:chan-two")

        self.assertEqual(self.scanned_channels(), ["chan-two"])
        self.assertEqual(sorted(self.rows()), ["telegram:chan-two"])

    def test_a_bare_channel_id_is_understood_as_a_channel_filter(self):
        self.sync(source_filter="chan-two")

        self.assertEqual(self.scanned_channels(), ["chan-two"])
        self.assertEqual(sorted(self.rows()), ["telegram:chan-two"])

    def test_the_provider_filter_keeps_every_channel_and_only_telegram_rows(self):
        self.config.sources["pctv"] = SourceConfig(provider="pctv")

        self.assertTrue(self.sync(source_filter="telegram"))

        self.assertEqual(self.scanned_channels(), ["chan-one", "chan-two"])
        self.assertEqual(sorted(self.rows()), ["telegram:chan-one", "telegram:chan-two"])

    def test_an_unrelated_filter_skips_telegram_without_touching_the_gate(self):
        self.assertFalse(self.sync(source_filter="chan-missing"))

        self.assertEqual(self.scanned_channels(), [])
        self.assertEqual(self.sink("cleanup_retention").args()[3], set())
        self.assertEqual(self.history()["sources"], [
            {"source": "chan-missing", "label": "chan-missing", "success": False, "error": None,
             "added": 0, "detail": None, "removed": 0, "videos": 0, "duration_seconds": None}])

    def test_the_channel_scans_can_be_skipped_for_a_maintenance_only_pass(self):
        self.assertTrue(self.sync(skip_telegram_channels=True))

        self.assertEqual(self.scanned_channels(), [])
        self.assertTrue(self.core.logger.has(
            "Skipping Telegram channel scans by request; continuing full maintenance pass.", "info"))
        self.assertEqual(self.history()["sources"], [])

    def test_a_disabled_telegram_source_scans_and_reports_nothing(self):
        self.config.sources["telegram"].enabled = False
        self.use_core(config=self.config, sync_telegram=False)

        self.assertTrue(self.sync())

        self.assertEqual(self.scanned_channels(), [])
        self.assertEqual(self.history()["sources"], [])

    def test_the_global_switch_stops_the_scans_but_the_channels_stay_expected(self):
        self.use_core(config=self.config, sync_telegram=False)

        self.assertFalse(self.sync())

        self.assertEqual(self.scanned_channels(), [])
        self.assertEqual([row["source"] for row in self.history()["sources"]],
                         ["telegram:chan-one", "telegram:chan-two"])

    def test_a_source_without_channels_expects_no_telegram_row(self):
        self.config.sources["telegram"].channels = []

        self.assertTrue(self.sync())

        self.assertEqual(self.history()["sources"], [])



class TestTelegramAvailabilityReconciliation(SyncPassHarness):
    def test_the_reconciler_runs_only_for_a_scheduled_sync(self):
        self.sync(sync_mode="backfill")

        self.assertFalse(self.sink("reconcile_telegram_channel").called)

    def test_the_reconciler_is_handed_only_its_own_channels_videos(self):
        self.sync()

        reconciler = self.sink("reconcile_telegram_channel")
        self.assertEqual(reconciler.call_count, 2)
        first = reconciler.args()
        self.assertEqual(first[:5], (CONN, "chan-one", MEDIA_DIR,
                                     DATA_DIR / "telegram-unavailable-quarantine",
                                     self.core.RESOLVER_API_URL))
        self.assertIs(first[5], self.sink("fetch_json_with_retry"))
        self.assertIs(first[6], self.core.logger)
        self.assertEqual(reconciler.kwargs(0)["current_video_ids"], {"telegram:chan-one:1"})
        self.assertEqual(reconciler.kwargs(1)["current_video_ids"], {"telegram:chan-two:1"})
        self.assertEqual(reconciler.kwargs(0)["now"], 1_060)

    def test_the_availability_limit_comes_from_the_environment(self):
        with patch.dict(os.environ, {"TELEGRAM_AVAILABILITY_CHECKS_PER_RUN": "7"}):
            self.sync()

        self.assertEqual(self.sink("reconcile_telegram_channel").kwargs()["limit"], 7)

    def test_the_availability_limit_defaults_to_twenty_checks_per_run(self):
        with patch.dict(os.environ, {}):
            os.environ.pop("TELEGRAM_AVAILABILITY_CHECKS_PER_RUN", None)
            self.sync()

        self.assertEqual(self.sink("reconcile_telegram_channel").kwargs()["limit"], 20)

    def test_a_failing_reconciler_does_not_sink_the_channel(self):
        self.explode("reconcile_telegram_channel", OSError("session revoked"))

        self.assertTrue(self.sync())

        self.assertEqual(self.sink("cleanup_retention").args()[3],
                         {"telegram:chan-one", "telegram:chan-two"})
        for channel in ("chan-one", "chan-two"):
            self.assertTrue(self.core.logger.has(
                f"Telegram availability reconciliation failed for {channel}: session revoked", "warning"))



class TestEnvironmentTelegramChannels(SyncPassHarness):
    """Without a managed config the pass walks TELEGRAM_CHANNELS with a minimal channel shape."""

    def setUp(self):
        super().setUp()
        self.use_core(config=None, telegram_channels=["chan-one", " ", "chan-two"],
                      telegram_max_pages=2)

    def test_the_environment_channels_are_scanned_with_a_minimal_shape(self):
        self.sync()

        scanned = self.sink("sync_telegram_channel_pipeline")
        self.assertEqual(self.scanned_channels(), ["chan-one", "chan-two"])
        channel = scanned.args(1)[1]
        self.assertEqual((channel.studio, channel.min_duration_seconds), ("chan-two", 420))
        self.assertEqual(channel.max_pages, self.core.TELEGRAM_MAX_PAGES)
        self.assertEqual(channel.history_batch_size, 100)
        self.assertTrue(self.core.logger.has(
            f"Syncing Telegram ({len(self.core.TELEGRAM_CHANNELS)} channel(s), "
            f"{self.core.TELEGRAM_MAX_PAGES} page(s))...", "info"))

    def test_the_environment_channels_report_their_own_outcome(self):
        self.sink("sync_telegram_channel_pipeline").raises = [ConnectionError("no session"), None]

        # Nothing is "expected" without a managed config, so the pass reports
        # success and the failed channel only shows up in the log: it never
        # reaches the destructive passes, but it is not counted as a failure.
        self.assertTrue(self.sync())

        self.assertEqual([row["source"] for row in self.history()["sources"]], ["telegram:chan-two"])
        self.assertEqual(self.sink("cleanup_orphaned_records").args()[2], {"telegram:chan-two"})
        self.assertTrue(self.core.logger.has(
            "Telegram sync failed for chan-one: safe detail for no session", "error"))

    def test_a_channel_filter_needs_a_managed_config_to_be_understood(self):
        # Without config.yaml a channel id cannot be resolved, so the filter
        # silently stops the Telegram scans - yet "telegram:<id>" still counts
        # as an expected source, and the pass reports it as a failure.
        self.assertFalse(self.sync(source_filter="telegram:chan-two"))

        self.assertEqual(self.scanned_channels(), [])
        self.assertEqual(self.rows()["telegram:chan-two"]["success"], False)

    def test_the_provider_filter_scans_every_environment_channel(self):
        self.assertTrue(self.sync(source_filter="telegram"))

        self.assertEqual(self.scanned_channels(), ["chan-one", "chan-two"])
        self.assertEqual([row["source"] for row in self.history()["sources"]],
                         ["telegram:chan-one", "telegram:chan-two"])
        self.assertEqual([row["success"] for row in self.history()["sources"]], [True, True])

    def test_the_availability_reconciler_is_not_used_without_a_managed_config(self):
        self.sync()

        self.assertFalse(self.sink("reconcile_telegram_channel").called)



class TestTelegramAuthorizationSyncGating(SyncPassHarness):
    """Verify unauthorized Telegram channel scans are gated on all sync pass types."""

    def test_unauthorized_sync_skips_scans_without_filter(self):
        self.core.is_telegram_authorized = lambda: False
        self.assertTrue(self.sync())
        self.assertEqual(self.scanned_channels(), [])
        self.assertTrue(self.core.logger.has("Telegram is not authorized yet", "info"))
        # Verify non-Telegram maintenance continues: catalog sync ran
        self.assertEqual(self.sink("sync_catalog_if_due").call_count, 1)

    def test_unauthorized_sync_skips_scans_with_telegram_filter(self):
        self.core.is_telegram_authorized = lambda: False
        self.assertTrue(self.sync(source_filter="telegram"))
        self.assertEqual(self.scanned_channels(), [])
        self.assertTrue(self.core.logger.has("Telegram is not authorized yet", "info"))
        self.assertEqual(self.sink("sync_catalog_if_due").call_count, 1)

    def test_unauthorized_sync_skips_scans_in_backfill_mode(self):
        self.core.is_telegram_authorized = lambda: False
        self.assertTrue(self.sync(sync_mode="backfill"))
        self.assertEqual(self.scanned_channels(), [])
        self.assertTrue(self.core.logger.has("Telegram is not authorized yet", "info"))

    def test_unauthorized_sync_skips_scans_when_auth_function_missing(self):
        if hasattr(self.core, "is_telegram_authorized"):
            delattr(self.core, "is_telegram_authorized")
        self.assertTrue(self.sync())
        self.assertEqual(self.scanned_channels(), [])
        self.assertTrue(self.core.logger.has("Telegram is not authorized yet", "info"))
        self.assertEqual(self.sink("sync_catalog_if_due").call_count, 1)

    def test_unauthorized_sync_skips_scans_when_auth_function_not_callable(self):
        self.core.is_telegram_authorized = "not_callable"
        self.assertTrue(self.sync())
        self.assertEqual(self.scanned_channels(), [])
        self.assertTrue(self.core.logger.has("Telegram is not authorized yet", "info"))
        self.assertEqual(self.sink("sync_catalog_if_due").call_count, 1)

    def test_authorized_sync_runs_scans(self):
        self.core.is_telegram_authorized = lambda: True
        self.assertTrue(self.sync())
        self.assertEqual(self.scanned_channels(), ["chan-one", "chan-two"])



class TestIsTelegramAuthorizedFunction(unittest.TestCase):
    """Verify sync.sync.is_telegram_authorized fails closed and checks resolver or fallback."""

    def setUp(self):
        # Access the sync facade without polluting sys.modules['sync.sync']
        self.sync_mod = sync_orchestration._core

    def test_resolver_returns_authorized_true(self):
        class FakeResp:
            status = 200
            def read(self):
                return b'{"authorized": true}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with patch.dict(os.environ, {"RESOLVER_API_URL": "http://resolver:8090"}), \
             patch("urllib.request.urlopen", return_value=FakeResp()):
            self.assertTrue(self.sync_mod.is_telegram_authorized())

    def test_resolver_returns_authorized_false(self):
        class FakeResp:
            status = 200
            def read(self):
                return b'{"authorized": false}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        with patch.dict(os.environ, {"RESOLVER_API_URL": "http://resolver:8090"}), \
             patch("urllib.request.urlopen", return_value=FakeResp()):
            self.assertFalse(self.sync_mod.is_telegram_authorized())

    def test_resolver_error_with_unauthorized_session_file_present_fails_closed(self):
        """When resolver fails, presence of a non-empty session file MUST NOT grant authorization."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".session") as f:
            f.write(b"unauthorized_file_content")
            f.flush()
            with patch.dict(os.environ, {
                "RESOLVER_API_URL": "http://resolver:8090",
                "TELEGRAM_SESSION_PATH": f.name,
            }), patch("urllib.request.urlopen", side_effect=OSError("Resolver down")):
                # Must fail closed: file existence is not authorization
                self.assertFalse(self.sync_mod.is_telegram_authorized())

    def test_absent_config_fails_closed(self):
        """When neither resolver URL nor credentials are configured, fail closed."""
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(self.sync_mod.is_telegram_authorized())

    def test_defensible_fallback_when_resolver_absent_positive_auth(self):
        """When resolver is absent but valid credentials and session exist, fallback verifies auth."""
        import tempfile
        from unittest.mock import AsyncMock
        with tempfile.NamedTemporaryFile(suffix=".session") as f:
            f.write(b"session_bytes")
            f.flush()
            fake_pkg = types.ModuleType("resolver.providers")
            fake_session_mod = types.ModuleType("resolver.providers.telegram_session")
            fake_session_mod.is_session_authorized = AsyncMock(return_value=True)
            fake_pkg.telegram_session = fake_session_mod
            with patch.dict(os.environ, {
                "RESOLVER_API_URL": "",
                "TELEGRAM_API_ID": "123456",
                "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
                "TELEGRAM_SESSION_PATH": f.name,
            }, clear=True), patch.dict(sys.modules, {"resolver.providers": fake_pkg, "resolver.providers.telegram_session": fake_session_mod}):
                self.assertTrue(self.sync_mod.is_telegram_authorized())

    def test_defensible_fallback_when_resolver_absent_unauthorized(self):
        """When resolver is absent and fallback verification returns False, fail closed."""
        import tempfile
        from unittest.mock import AsyncMock
        with tempfile.NamedTemporaryFile(suffix=".session") as f:
            f.write(b"session_bytes")
            f.flush()
            fake_pkg = types.ModuleType("resolver.providers")
            fake_session_mod = types.ModuleType("resolver.providers.telegram_session")
            fake_session_mod.is_session_authorized = AsyncMock(return_value=False)
            fake_pkg.telegram_session = fake_session_mod
            with patch.dict(os.environ, {
                "RESOLVER_API_URL": "",
                "TELEGRAM_API_ID": "123456",
                "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
                "TELEGRAM_SESSION_PATH": f.name,
            }, clear=True), patch.dict(sys.modules, {"resolver.providers": fake_pkg, "resolver.providers.telegram_session": fake_session_mod}):
                self.assertFalse(self.sync_mod.is_telegram_authorized())



if __name__ == "__main__":
    unittest.main()
