"""Unit tests for Stage 4: first-run readiness and Admin integration.

Covers:
1. Zero channels and first-source readiness (config loading, overrides fallback, and Admin v2 API creation).
2. First-run cleanup and retention safety (empty library, missing or unmounted media directory).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


from common.config_loader import load_config, parse_and_validate_config
from common.managed_sources import preview_overrides, update_overrides
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
