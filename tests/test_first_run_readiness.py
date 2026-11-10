"""Unit tests for Stage 4: first-run readiness and Admin integration.

Covers:
1. Zero channels and first-source readiness (config loading, overrides fallback, and Admin v2 API creation).
2. Missing Jellyfin API key safely skipped during sync/jobs, and useful errors when credentials required.
3. First-run cleanup and retention safety (empty library, missing or unmounted media directory).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch


# Isolate environment to prevent touching production config or /data
_ORIG_CONFIG_PATH = os.environ.get("CONFIG_PATH")
_ORIG_SYNC_ACTIVITY_LOG_PATH = os.environ.get("SYNC_ACTIVITY_LOG_PATH")
_ISOLATED_DIR = tempfile.TemporaryDirectory()
_ISOLATED_PATH = Path(_ISOLATED_DIR.name)
_ISOLATED_CONFIG = _ISOLATED_PATH / "config.yaml"
_ISOLATED_CONFIG.write_text("sync:\n  interval_minutes: 60\nsources: []\n", encoding="utf-8")
os.environ["CONFIG_PATH"] = str(_ISOLATED_CONFIG)
os.environ["SYNC_ACTIVITY_LOG_PATH"] = str(_ISOLATED_PATH / "sync_activity.log")

def tearDownModule():
    if _ORIG_CONFIG_PATH is not None:
        os.environ["CONFIG_PATH"] = _ORIG_CONFIG_PATH
    else:
        os.environ.pop("CONFIG_PATH", None)
    if _ORIG_SYNC_ACTIVITY_LOG_PATH is not None:
        os.environ["SYNC_ACTIVITY_LOG_PATH"] = _ORIG_SYNC_ACTIVITY_LOG_PATH
    else:
        os.environ.pop("SYNC_ACTIVITY_LOG_PATH", None)
    _ISOLATED_DIR.cleanup()

from common.config_loader import load_config, parse_and_validate_config
from common.managed_sources import preview_overrides, update_overrides
from sync.jellyfin_db import trigger_library_refresh
from sync.jellyfin_titles import sync_jellyfin_titles
from sync.jellyfin_people import sync_jellyfin_people
from sync.jellyfin_telegram_metadata import sync_jellyfin_telegram_metadata
from sync.library_cleanup import _media_root_is_safe_for_cleanup, cleanup_orphaned_records, cleanup_retention, LibraryCleanupServices


class TestFirstSourceReadiness(unittest.TestCase):
    """Test zero channels baseline and first-source addition through Admin v2."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config.yaml"
        self.overlay_path = self.root / "admin-source-overrides.json"
        self.prev_env = os.environ.get("ADMIN_SOURCE_OVERRIDES_PATH")
        os.environ["ADMIN_SOURCE_OVERRIDES_PATH"] = str(self.overlay_path)

    def tearDown(self):
        if self.prev_env is None:
            os.environ.pop("ADMIN_SOURCE_OVERRIDES_PATH", None)
        else:
            os.environ["ADMIN_SOURCE_OVERRIDES_PATH"] = self.prev_env
        self.temp_dir.cleanup()

    def test_zero_channels_in_config_loads_normally(self):
        """A fresh config with zero channels parses and validates normally."""
        raw_config = {
            "sync": {"interval_minutes": 60},
            "sources": [
                {
                    "provider": "telegram",
                    "enabled": True,
                    "defaults": {
                        "stream_mode": "proxy",
                        "max_pages": 1,
                        "min_duration_seconds": 420,
                        "history_batch_size": 100,
                        "tags_whitelist": [],
                        "tags_blacklist": [],
                    },
                    "channels": [],
                }
            ],
        }
        config = parse_and_validate_config(raw_config)
        self.assertIn("telegram", config.sources)
        self.assertEqual(len(config.sources["telegram"].channels), 0)

    def test_first_channel_addition_via_overrides(self):
        """Adding first channel to zero-channel config produces valid effective config."""
        self.config_path.write_text("""
sync:
  interval_minutes: 60
sources:
  - provider: telegram
    enabled: true
    defaults:
      stream_mode: proxy
      max_pages: 1
      min_duration_seconds: 420
      history_batch_size: 100
      tags_whitelist: []
      tags_blacklist: []
    channels: []
""", encoding="utf-8")

        candidate = {
            "telegram": {
                "channels": {
                    "-100123456789": {"studio": "First Channel Studio", "enabled": True}
                }
            }
        }
        preview = preview_overrides(self.config_path, candidate, expected_revision=0)
        self.assertEqual(preview["revision"], 0)
        self.assertEqual(preview["next_revision"], 1)
        self.assertTrue(len(preview["changes"]) > 0)

        saved = update_overrides(
            self.config_path, candidate, expected_revision=0,
            audit_event={"changes": preview["changes"], "truncated": False},
        )
        self.assertEqual(saved["revision"], 1)

        loaded = load_config(str(self.config_path), force_reload=True)
        self.assertEqual(loaded.managed_revision, 1)
        tg = loaded.get_source("telegram")
        self.assertIsNotNone(tg)
        self.assertEqual(len(tg.channels), 1)
        self.assertEqual(tg.channels[0].id, "-100123456789")
        self.assertEqual(tg.channels[0].studio, "First Channel Studio")
        self.assertTrue(tg.channels[0].enabled)

    def test_first_channel_addition_when_base_sources_empty(self):
        """Adding a channel when base configuration has empty sources list initializes telegram source."""
        self.config_path.write_text("""
sync:
  interval_minutes: 60
sources: []
""", encoding="utf-8")

        candidate = {
            "telegram": {
                "channels": {
                    "-100987654321": {"studio": "Fallback Studio", "enabled": True}
                }
            }
        }
        preview = preview_overrides(self.config_path, candidate, expected_revision=0)
        self.assertEqual(preview["revision"], 0)
        self.assertEqual(preview["next_revision"], 1)

        saved = update_overrides(
            self.config_path, candidate, expected_revision=0,
            audit_event={"changes": preview["changes"], "truncated": False},
        )
        self.assertEqual(saved["revision"], 1)

        loaded = load_config(str(self.config_path), force_reload=True)
        tg = loaded.get_source("telegram")
        self.assertIsNotNone(tg)
        self.assertEqual(len(tg.channels), 1)
        self.assertEqual(tg.channels[0].id, "-100987654321")


class TestJellyfinIntegrationOptionalAuth(unittest.TestCase):
    """Test optional Jellyfin credentials and graceful skipping of API operations."""

    def test_trigger_library_refresh_skips_when_api_key_empty(self):
        """trigger_library_refresh safely returns False without raising when API key is empty."""
        logger_mock = Mock()
        result = trigger_library_refresh("http://jellyfin:8096", "", logger=logger_mock)
        self.assertFalse(result)
        logger_mock.info.assert_called_once()
        self.assertIn("skipped", logger_mock.info.call_args[0][0])

    def test_trigger_library_refresh_skips_when_url_empty(self):
        """trigger_library_refresh safely returns False when URL is empty."""
        logger_mock = Mock()
        result = trigger_library_refresh("", "secret_key", logger=logger_mock)
        self.assertFalse(result)
        logger_mock.info.assert_called_once()

    def test_sync_jellyfin_titles_skips_when_credentials_absent(self):
        """sync_jellyfin_titles returns skipped status without making HTTP requests."""
        logger_mock = Mock()
        result = sync_jellyfin_titles(clean_title=lambda t: t, url="http://jellyfin:8096", api_key="", logger=logger_mock)
        self.assertEqual(result, {"checked": 0, "updated": 0, "failed": 0, "skipped": "not_configured"})
        logger_mock.info.assert_called_once()

    def test_sync_jellyfin_people_skips_when_credentials_absent(self):
        """sync_jellyfin_people returns (0, 0) when URL or API key is absent."""
        conn_mock = Mock()
        checked, updated = sync_jellyfin_people(conn_mock, "http://jellyfin:8096", "")
        self.assertEqual((checked, updated), (0, 0))
        conn_mock.execute.assert_not_called()

    def test_sync_jellyfin_telegram_metadata_skips_when_credentials_absent(self):
        """sync_jellyfin_telegram_metadata returns zeroes without raising when API key is empty."""
        result = sync_jellyfin_telegram_metadata("http://jellyfin:8096", "", Path("/tmp"), _core={})
        self.assertEqual(result, {"checked": 0, "needed": 0, "updated": 0, "failed": 0})

    def test_performer_refresh_skips_jellyfin_when_credentials_absent(self):
        """_performer_refresh safely records jellyfin_refresh as skipped instead of failing."""
        from sync.duplicate_jobs import _performer_refresh

        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (
            "telegram", "Title", "slug", 100, "thumb.jpg", '["Performer 1"]', "[]", "[]", "Studio", 1000, "Plot"
        )
        services = Mock()
        services.load_performer_thumbs.return_value = {}
        services.format_dateadded.return_value = "2026-11-06"
        services.trigger_jellyfin_refresh.return_value = False

        with patch.dict(os.environ, {"JELLYFIN_URL": "http://jellyfin:8096", "JELLYFIN_API_KEY": ""}), \
             tempfile.TemporaryDirectory() as tmp_media:
            result = _performer_refresh(
                conn, "job-perf-1", {"video_ids": ["telegram:1:100"]}, Path(tmp_media), services
            )

        self.assertEqual(result.get("videos_refreshed"), 1)
        self.assertEqual(result.get("jellyfin_refresh"), "skipped")
        services.trigger_jellyfin_refresh.assert_not_called()

    def test_collections_refresh_surfaces_useful_error_when_credentials_required(self):
        """_action_collections_refresh raises ValueError when credentials are required for actual API task."""
        from sync.duplicate_job_external_handlers import _action_collections_refresh

        conn = MagicMock()
        with patch.dict(os.environ, {"JELLYFIN_URL": "http://jellyfin:8096", "JELLYFIN_API_KEY": ""}):
            with self.assertRaises(ValueError) as ctx:
                _action_collections_refresh(conn, "job-col-1", {}, Path("/tmp"), Mock())
            self.assertIn("Jellyfin configuration is required for collection refresh", str(ctx.exception))


class TestFirstRunSafety(unittest.TestCase):
    """Test safety invariants on empty library and missing media directory."""

    def test_missing_media_directory_stops_cleanup(self):
        """_media_root_is_safe_for_cleanup fails closed when media directory does not exist."""
        logger_mock = Mock()
        conn_mock = Mock()
        non_existent = Path("/nonexistent/media/mount/path")

        self.assertFalse(_media_root_is_safe_for_cleanup(non_existent, conn_mock, logger_mock))
        logger_mock.warning.assert_called_once()
        self.assertIn("media directory is unavailable", logger_mock.warning.call_args[0][0])
        conn_mock.execute.assert_not_called()

    def test_missing_mount_with_registered_files_stops_cleanup(self):
        """_media_root_is_safe_for_cleanup fails closed when files are in DB but mount has no .strm files."""
        with tempfile.TemporaryDirectory() as tmp:
            empty_mount = Path(tmp) / "empty_mount"
            empty_mount.mkdir()

            conn_mock = Mock()
            conn_mock.execute.return_value.fetchone.return_value = (50,)  # 50 registered files
            logger_mock = Mock()

            self.assertFalse(_media_root_is_safe_for_cleanup(empty_mount, conn_mock, logger_mock))
            logger_mock.warning.assert_called_once()
            self.assertIn("holds no stream files while the catalog registers", logger_mock.warning.call_args[0][0])

    def test_empty_library_cleanup_is_non_destructive(self):
        """An empty library pass executes without deletions or errors."""
        with tempfile.TemporaryDirectory() as tmp:
            media_dir = Path(tmp) / "media"
            media_dir.mkdir()

            conn_mock = Mock()
            # synced_files count is 0
            conn_mock.execute.return_value.fetchone.return_value = (0,)
            conn_mock.execute.return_value.fetchall.return_value = []
            logger_mock = Mock()

            services = LibraryCleanupServices(
                is_cleanup_allowed=Mock(return_value=False),
                video_is_disabled=Mock(return_value=False),
                resolve_media_path=lambda p, m: m / p,
                logger=logger_mock,
            )

            cleanup_orphaned_records(media_dir, conn_mock, providers_succeeded=set(), services=services)
            cleanup_retention(conn_mock, media_dir, retention_days=30, providers_succeeded=set(), services=services)

            # Confirm no DELETE query was executed
            for call_item in conn_mock.execute.call_args_list:
                sql = call_item[0][0]
                self.assertNotIn("DELETE", sql)


if __name__ == "__main__":
    unittest.main()
