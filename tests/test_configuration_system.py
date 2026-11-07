"""Focused tests for centralized configuration defaults, validation, and migration.

Verifies:
1. Canonical defaults in common.config.
2. Validation helpers with actionable error messages.
3. Legacy alias resolution and fallback chains.
4. Safe secret redaction.
5. Optional config.yaml behavior and Admin overlay precedence.
6. Safe, idempotent migration dry-run and live import.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common.config import (
    ConfigurationError,
    DEFAULT_BACKUP_DAILY_AT,
    DEFAULT_BACKUP_KEEP_DAYS,
    DEFAULT_LOG_LEVEL,
    DEFAULT_RESOLVER_PUBLIC_URL,
    DEFAULT_RESOLVER_WATCHDOG_FAILURES,
    get_admin_public,
    get_admin_write_token,
    get_backup_daily_at,
    get_backup_keep_days,
    get_database_url,
    get_resolver_public_url,
    get_stashdb_api_key,
    get_stream_public_url_telegram,
    get_theporndb_api_token,
    parse_bool,
    parse_daily_at,
    parse_float,
    parse_int,
    parse_url,
)
from common.config_loader import (
    ConfigError,
    find_config_file,
    load_config,
)
from common.config_migration import (
    audit_configuration,
    import_yaml_sources_to_overlay,
    redact_sensitive_text,
    redact_value,
)
from common.managed_sources import read_overrides


class TestConfigDefaultsAndValidation(unittest.TestCase):
    """Test common.config validation, parsing, and actionable error messages."""

    def test_canonical_defaults_are_sound(self):
        self.assertEqual(DEFAULT_LOG_LEVEL, "INFO")
        self.assertEqual(DEFAULT_BACKUP_KEEP_DAYS, 7)
        self.assertEqual(DEFAULT_BACKUP_DAILY_AT, "04:30")
        self.assertEqual(DEFAULT_RESOLVER_PUBLIC_URL, "http://127.0.0.1:8090")

    def test_parse_bool_handles_truthy_falsy_and_defaults(self):
        for truthy in ("1", "true", "True", "YES", "on", True):
            self.assertTrue(parse_bool(truthy, False))
        for falsy in ("0", "false", "False", "NO", "off", False):
            self.assertFalse(parse_bool(falsy, True))
        self.assertTrue(parse_bool(None, True))
        self.assertFalse(parse_bool(None, False))

    def test_parse_int_validates_bounds_and_actionable_errors(self):
        self.assertEqual(parse_int("42", 10), 42)
        self.assertEqual(parse_int(None, 10), 10)
        self.assertEqual(parse_int("", 10), 10)

        with self.assertRaises(ConfigurationError) as ctx:
            parse_int("not_a_number", 10, name="MY_INT")
        self.assertIn("MY_INT", str(ctx.exception))
        self.assertIn("must be an integer", str(ctx.exception))

        with self.assertRaises(ConfigurationError) as ctx:
            parse_int("-5", 10, minimum=0, name="POSITIVE_INT")
        self.assertIn("must be >= 0", str(ctx.exception))

        with self.assertRaises(ConfigurationError) as ctx:
            parse_int(True, 10, name="BOOL_TRAP")
        self.assertIn("must be an integer, got boolean", str(ctx.exception))

    def test_parse_float_validates_actionable_errors(self):
        self.assertEqual(parse_float("3.14", 1.0), 3.14)
        self.assertEqual(parse_float(None, 2.5), 2.5)

        with self.assertRaises(ConfigurationError) as ctx:
            parse_float("bad", 1.0, name="FLOAT_VAL")
        self.assertIn("FLOAT_VAL", str(ctx.exception))

        with self.assertRaises(ConfigurationError) as ctx:
            parse_float("-1.0", 1.0, minimum=0.0, name="POS_FLOAT")
        self.assertIn("must be >= 0.0", str(ctx.exception))

        for non_finite in ("nan", "NaN", "inf", "-inf", "Infinity"):
            with self.subTest(non_finite=non_finite):
                with self.assertRaises(ConfigurationError) as ctx:
                    parse_float(non_finite, 1.0, name="FLOAT_VAL")
                self.assertIn("must be a finite number", str(ctx.exception))

    def test_parse_daily_at_validates_24h_time(self):
        self.assertEqual(parse_daily_at("04:30"), "04:30")
        self.assertEqual(parse_daily_at("4:05"), "04:05")
        self.assertEqual(parse_daily_at("00:00"), "00:00")
        self.assertEqual(parse_daily_at("23:59"), "23:59")
        self.assertEqual(parse_daily_at(None, "04:30"), "04:30")

        for bad in ("24:00", "25:00", "04:60", "invalid", "12", "12:00:00"):
            with self.subTest(bad=bad), self.assertRaises(ConfigurationError) as ctx:
                parse_daily_at(bad, name="BACKUP_DAILY_AT")
            self.assertIn("HH:MM format", str(ctx.exception))

    def test_parse_url_validates_scheme_and_host(self):
        self.assertEqual(parse_url("https://example.com/play"), "https://example.com/play")
        self.assertEqual(parse_url("http://127.0.0.1:8090"), "http://127.0.0.1:8090")
        self.assertEqual(parse_url(None, "http://default"), "http://default")

        with self.assertRaises(ConfigurationError) as ctx:
            parse_url("ftp://example.com", name="TEST_URL")
        self.assertIn("must be a valid HTTP or HTTPS URL", str(ctx.exception))

        with self.assertRaises(ConfigurationError) as ctx:
            parse_url("http://", name="TEST_URL")
        self.assertIn("missing a valid host", str(ctx.exception))

    def test_database_url_test_safety_without_fallback(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:pass@prod:5432/live", "TEST_DATABASE_URL": ""}):
            with self.assertRaises(RuntimeError) as ctx:
                get_database_url(test=True)
            self.assertIn("TEST_DATABASE_URL is not configured", str(ctx.exception))
            self.assertNotIn("live", str(ctx.exception))

            self.assertEqual(
                get_database_url(test=True, allow_test_fallback=True),
                "postgresql://user:pass@prod:5432/live",
            )

    def test_admin_write_token_fails_closed_on_whitespace(self):
        with patch.dict(os.environ, {"ADMIN_WRITE_TOKEN": "   "}):
            with self.assertRaises(ConfigurationError) as ctx:
                get_admin_write_token()
            self.assertIn("cannot be whitespace-only", str(ctx.exception))

        with patch.dict(os.environ, {"ADMIN_WRITE_TOKEN": "", "ADMIN_DUPLICATE_TOKEN": "   "}):
            with self.assertRaises(ConfigurationError) as ctx:
                get_admin_write_token()
            self.assertIn("cannot be whitespace-only", str(ctx.exception))

        with patch.dict(os.environ, {"ADMIN_WRITE_TOKEN": "valid", "ADMIN_DUPLICATE_TOKEN": "   "}):
            with self.assertRaises(ConfigurationError) as ctx:
                get_admin_write_token()
            self.assertIn("cannot be whitespace-only", str(ctx.exception))

    def test_admin_security_write_authorized_fails_closed_when_token_is_none(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "resolver"))
        import admin_security

        class DummyRequest:
            headers = {
                "origin": "http://localhost:8090",
                "host": "localhost:8090",
                "authorization": "Bearer __FAIL_CLOSED_INVALID_TOKEN__",
            }
            url = "http://localhost:8090/admin/sources"

        req = DummyRequest()
        # When token is None (indicating invalid / fail-closed token configuration),
        # write_authorized MUST return False unconditionally even if Bearer token matches.
        self.assertFalse(admin_security.write_authorized(req, blocked=False, public=False, token=None))
        # Sentinel strings cannot authenticate against valid tokens
        self.assertFalse(admin_security.write_authorized(req, blocked=False, public=False, token="secret_token"))

    def test_watchdog_default_failures_is_three(self):
        self.assertEqual(DEFAULT_RESOLVER_WATCHDOG_FAILURES, 3)

    def test_admin_public_precedence_and_fallback(self):
        with patch.dict(os.environ, {"ADMIN_PUBLIC": "", "STANDALONE_ADMIN_PUBLIC": "true"}):
            self.assertTrue(get_admin_public())
        with patch.dict(os.environ, {"ADMIN_PUBLIC": "false", "STANDALONE_ADMIN_PUBLIC": "true"}):
            self.assertFalse(get_admin_public())
        with patch.dict(os.environ, {"ADMIN_PUBLIC": "", "STANDALONE_ADMIN_PUBLIC": ""}):
            self.assertFalse(get_admin_public())


class TestLegacyAliasesAndGetters(unittest.TestCase):
    """Test legacy alias resolution and fallback chains."""

    def test_admin_write_token_fallback_chain(self):
        with patch.dict(os.environ, {"ADMIN_WRITE_TOKEN": "primary_token", "ADMIN_DUPLICATE_TOKEN": "legacy_token"}):
            self.assertEqual(get_admin_write_token(), "primary_token")

        with patch.dict(os.environ, {"ADMIN_WRITE_TOKEN": "", "ADMIN_DUPLICATE_TOKEN": "legacy_token"}):
            self.assertEqual(get_admin_write_token(), "legacy_token")

        with patch.dict(os.environ, {"ADMIN_WRITE_TOKEN": "", "ADMIN_DUPLICATE_TOKEN": ""}):
            self.assertEqual(get_admin_write_token(), "")

    def test_stashdb_api_key_fallback_chain(self):
        with patch.dict(os.environ, {"STASHDB_API_KEY": "primary_key", "STASH_API_KEY": "legacy_key"}):
            self.assertEqual(get_stashdb_api_key(), "primary_key")

        with patch.dict(os.environ, {"STASHDB_API_KEY": "", "STASH_API_KEY": "legacy_key"}):
            self.assertEqual(get_stashdb_api_key(), "legacy_key")

    def test_theporndb_api_token_fallback_chain(self):
        with patch.dict(os.environ, {"THEPORNDB_API_TOKEN": "primary_tok", "THEPORNDB_API_KEY": "alt_key"}):
            self.assertEqual(get_theporndb_api_token(), "primary_tok")

        with patch.dict(os.environ, {"THEPORNDB_API_TOKEN": "", "THEPORNDB_API_KEY": "alt_key"}):
            self.assertEqual(get_theporndb_api_token(), "alt_key")

    def test_database_url_fallback_to_standalone_database_url(self):
        with patch.dict(os.environ, {
            "DATABASE_URL": "",
            "STANDALONE_DATABASE_URL": "postgresql://user:secretpass123@db:5432/adult",
        }):
            self.assertEqual(get_database_url(), "postgresql://user:secretpass123@db:5432/adult")

        with patch.dict(os.environ, {"DATABASE_URL": "", "STANDALONE_DATABASE_URL": ""}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                get_database_url()
            self.assertIn("is not configured", str(ctx.exception))
            self.assertNotIn("secretpass123", str(ctx.exception))

    def test_stream_public_url_telegram_fallback(self):
        with patch.dict(os.environ, {
            "STREAM_PUBLIC_URL_TELEGRAM": "https://tg.example.org",
            "RESOLVER_PUBLIC_URL": "https://main.example.org",
        }):
            self.assertEqual(get_stream_public_url_telegram(), "https://tg.example.org")

        with patch.dict(os.environ, {
            "STREAM_PUBLIC_URL_TELEGRAM": "",
            "RESOLVER_PUBLIC_URL": "https://main.example.org",
        }):
            self.assertEqual(get_stream_public_url_telegram(), "https://main.example.org")

    def test_backup_helpers(self):
        with patch.dict(os.environ, {"BACKUP_KEEP_DAYS": "14", "BACKUP_DAILY_AT": "02:15"}):
            self.assertEqual(get_backup_keep_days(), 14)
            self.assertEqual(get_backup_daily_at(), "02:15")

    def test_resolver_public_url(self):
        with patch.dict(os.environ, {"RESOLVER_PUBLIC_URL": "https://pub.example.org"}):
            self.assertEqual(get_resolver_public_url(), "https://pub.example.org")
        with patch.dict(os.environ, {"RESOLVER_PUBLIC_URL": ""}):
            self.assertEqual(get_resolver_public_url(), DEFAULT_RESOLVER_PUBLIC_URL)


class TestOptionalConfigYamlAndOverlayPrecedence(unittest.TestCase):
    """Verify that config.yaml is optional and Admin UI overlays take precedence."""

    def test_load_config_without_file_returns_defaults(self):
        with patch.dict(os.environ, {"CONFIG_PATH": "/config/config.yaml"}):
            self.assertIsNone(find_config_file(optional=True))
            cfg = load_config(force_reload=True, allow_missing=True)
            self.assertEqual(cfg.sync.retention_days, 30)
            self.assertEqual(cfg.sync.interval_minutes, 60)
            self.assertEqual(cfg.sources, {})

    def test_admin_overlay_populates_sources_without_config_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay_file = Path(tmp) / "admin-source-overrides.json"
            overlay_file.write_text("""{
                "revision": 3,
                "overrides": {
                    "telegram": {
                        "enabled": true,
                        "defaults": {
                            "stream_mode": "proxy",
                            "max_pages": 1,
                            "min_duration_seconds": 420,
                            "history_batch_size": 100,
                            "tags_whitelist": [],
                            "tags_blacklist": []
                        },
                        "channels": {
                            "@dynamic_chan": {
                                "enabled": true,
                                "studio": "Overlay Studio"
                            }
                        }
                    }
                }
            }""", encoding="utf-8")

            with patch.dict(os.environ, {
                "CONFIG_PATH": "/config/config.yaml",
                "ADMIN_SOURCE_OVERRIDES_PATH": str(overlay_file),
            }):
                cfg = load_config(force_reload=True, allow_missing=True)
                tg = cfg.get_source("telegram")
                self.assertIsNotNone(tg)
                self.assertTrue(tg.enabled)
                self.assertEqual(len(tg.channels), 1)
                ch = tg.get_channel("@dynamic_chan")
                self.assertIsNotNone(ch)
                self.assertEqual(ch.studio, "Overlay Studio")
                self.assertEqual(cfg.managed_revision, 3)

    def test_admin_overlay_takes_precedence_over_yaml_channels(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_file = Path(tmp) / "config.yaml"
            yaml_file.write_text("""
sync:
  interval_minutes: 45
sources:
  - provider: telegram
    enabled: true
    channels:
      - id: "@test_chan"
        studio: "YAML Studio"
        enabled: true
""", encoding="utf-8")

            overlay_file = Path(tmp) / "admin-source-overrides.json"
            overlay_file.write_text("""{
                "revision": 1,
                "overrides": {
                    "telegram": {
                        "channels": {
                            "@test_chan": {
                                "studio": "Admin UI Updated Studio",
                                "enabled": false
                            }
                        }
                    }
                }
            }""", encoding="utf-8")

            with patch.dict(os.environ, {
                "ADMIN_SOURCE_OVERRIDES_PATH": str(overlay_file),
            }):
                cfg = load_config(str(yaml_file), force_reload=True)
                self.assertEqual(cfg.sync.interval_minutes, 45)
                tg = cfg.get_source("telegram")
                ch = tg.get_channel("@test_chan")
                # Admin UI override must win over static YAML
                self.assertEqual(ch.studio, "Admin UI Updated Studio")
                self.assertFalse(ch.enabled)

    def test_explicit_missing_custom_config_path_raises_config_error(self):
        with patch.dict(os.environ, {"CONFIG_PATH": "/custom/path/to/missing.yaml"}):
            with self.assertRaises(ConfigError) as ctx:
                find_config_file(optional=True)
            self.assertIn("does not exist", str(ctx.exception))

        with self.assertRaises(ConfigError) as ctx:
            find_config_file("/another/explicit/missing.yaml", optional=True)
        self.assertIn("does not exist", str(ctx.exception))

    def test_invalid_yaml_top_level_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_yaml = Path(tmp) / "bad.yaml"
            bad_yaml.write_text("- item1\n- item2\n", encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_config(str(bad_yaml), force_reload=True)
            self.assertIn("must be a YAML dictionary", str(ctx.exception))


class TestConfigMigrationAndRedaction(unittest.TestCase):
    """Test audit_configuration and idempotent YAML channel import."""

    def test_redact_value_protects_secrets(self):
        self.assertEqual(redact_value("DATABASE_URL", "postgresql://user:secret@db:5432/db"), "[REDACTED]")
        self.assertEqual(redact_value("ADMIN_WRITE_TOKEN", "supersecrettoken"), "[REDACTED]")
        self.assertEqual(redact_value("TELEGRAM_API_HASH", "abcdef0123456789"), "[REDACTED]")
        self.assertEqual(redact_value("RESOLVER_PUBLIC_URL", "https://example.com"), "https://example.com")
        self.assertEqual(redact_value("LOG_LEVEL", "INFO"), "INFO")

    def test_audit_configuration_reports_aliases_and_validates(self):
        test_env = {
            "DATABASE_URL": "postgresql://user:secret@localhost:5432/adult",
            "ADMIN_DUPLICATE_TOKEN": "legacy_token",
            "RESOLVER_PUBLIC_URL": "https://media.example.org",
            "BACKUP_KEEP_DAYS": "7",
            "BACKUP_DAILY_AT": "04:30",
        }
        report = audit_configuration(test_env)
        self.assertTrue(report["valid"])
        self.assertEqual(len(report["aliases_in_use"]), 1)
        self.assertEqual(report["aliases_in_use"][0]["legacy"], "ADMIN_DUPLICATE_TOKEN")
        self.assertEqual(report["aliases_in_use"][0]["canonical"], "ADMIN_WRITE_TOKEN")
        self.assertEqual(report["effective_summary"]["DATABASE_URL"], "[CONFIGURED]")
        self.assertEqual(report["effective_summary"]["ADMIN_WRITE_TOKEN"], "[CONFIGURED]")

    def test_audit_configuration_flags_invalid_settings(self):
        test_env = {
            "DATABASE_URL": "",
            "RESOLVER_PUBLIC_URL": "not_a_valid_url",
            "BACKUP_DAILY_AT": "26:99",
            "TELEGRAM_API_ID": "12345",
            # Missing TELEGRAM_API_HASH
        }
        report = audit_configuration(test_env)
        self.assertFalse(report["valid"])
        self.assertTrue(any("DATABASE_URL is not set" in w for w in report["warnings"]))
        self.assertTrue(any("RESOLVER_PUBLIC_URL" in w for w in report["warnings"]))
        self.assertTrue(any("BACKUP_DAILY_AT" in w for w in report["warnings"]))
        self.assertTrue(any("TELEGRAM_API_HASH" in w for w in report["warnings"]))

    def test_idempotent_yaml_import_dry_run_and_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_file = Path(tmp) / "config.yaml"
            yaml_file.write_text("""
sources:
  - provider: telegram
    enabled: true
    channels:
      - id: "@yaml_chan1"
        studio: "Studio One"
      - id: "@yaml_chan2"
        studio: "Studio Two"
""", encoding="utf-8")

            overlay_file = Path(tmp) / "admin-source-overrides.json"
            overlay_file.write_text("""{
                "revision": 1,
                "overrides": {
                    "telegram": {
                        "channels": {
                            "@yaml_chan1": {
                                "studio": "Operator Modified Studio"
                            }
                        }
                    }
                }
            }""", encoding="utf-8")

            # 1. Dry run
            dry_res = import_yaml_sources_to_overlay(yaml_file, overlay_file, dry_run=True)
            self.assertTrue(dry_res["success"])
            self.assertTrue(dry_res["dry_run"])
            self.assertEqual(dry_res["imported_channels"], ["@yaml_chan2"])
            self.assertEqual(dry_res["skipped_existing"], ["@yaml_chan1"])
            self.assertEqual(dry_res["changes_count"], 1)

            # Verify file was not modified in dry run
            self.assertEqual(read_overrides(overlay_file)["revision"], 1)

            # 2. Live execution
            live_res = import_yaml_sources_to_overlay(yaml_file, overlay_file, dry_run=False)
            self.assertTrue(live_res["success"])
            self.assertFalse(live_res["dry_run"])
            self.assertEqual(live_res["imported_channels"], ["@yaml_chan2"])

            # Verify live execution incremented revision and added channel
            updated_text = overlay_file.read_text(encoding="utf-8")
            self.assertEqual(read_overrides(overlay_file)["revision"], 2)
            self.assertIn("@yaml_chan2", updated_text)
            # Operator existing studio modification was preserved!
            self.assertIn("Operator Modified Studio", updated_text)

            # 3. Subsequent run is completely idempotent (0 changes)
            repeat_res = import_yaml_sources_to_overlay(yaml_file, overlay_file, dry_run=False)
            self.assertTrue(repeat_res["success"])
            self.assertEqual(repeat_res["changes_count"], 0)
            self.assertEqual(len(repeat_res["skipped_existing"]), 2)

    def test_import_yaml_creates_target_overlay_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_file = Path(tmp) / "config.yaml"
            yaml_file.write_text("""
sources:
  - provider: telegram
    channels:
      - id: "@fresh_chan"
        studio: "Fresh Studio"
""", encoding="utf-8")
            overlay_file = Path(tmp) / "new_dir" / "overlay.json"
            res = import_yaml_sources_to_overlay(yaml_file, overlay_file, dry_run=False)
            self.assertTrue(res["success"])
            self.assertTrue(overlay_file.is_file())
            self.assertEqual(read_overrides(overlay_file)["revision"], 1)

    def test_import_yaml_captures_lock_timeout_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_file = Path(tmp) / "config.yaml"
            yaml_file.write_text("sources: []\n", encoding="utf-8")
            overlay_file = Path(tmp) / "overlay.json"
            from common.managed_sources import ManagedConfigLockTimeout
            with patch("common.config_migration.config_lock", side_effect=ManagedConfigLockTimeout("locked")):
                res = import_yaml_sources_to_overlay(yaml_file, overlay_file, dry_run=False)
                self.assertFalse(res["success"])
                self.assertIn("locked", res["error"])

    def test_redact_sensitive_text_edge_cases(self):
        # Username and password
        res1 = redact_sensitive_text("Connecting to postgresql://adulthub:supersecret@localhost:5432/db")
        self.assertNotIn("supersecret", res1)
        self.assertIn("***:***@localhost:5432/db", res1)

        # Empty username with password
        res2 = redact_sensitive_text("Error with postgresql://:secret_pass@127.0.0.1:5432/db")
        self.assertNotIn("secret_pass", res2)
        self.assertIn("***:***@127.0.0.1:5432/db", res2)

        # Token parameter in query string
        res3 = redact_sensitive_text("Failed fetching http://api.internal/v1?token=abcdef12345&foo=bar")
        self.assertNotIn("abcdef12345", res3)
        self.assertIn("token=[REDACTED]", res3)

    def test_import_yaml_reports_skipped_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            yaml_file = Path(tmp) / "config.yaml"
            yaml_file.write_text("""
sources:
  - provider: telegram
    channels:
      - "not_a_dict"
      - id: ""
        studio: "Empty ID"
      - id: "@valid_chan"
        studio: "Valid Studio"
""", encoding="utf-8")
            overlay_file = Path(tmp) / "overlay.json"
            res = import_yaml_sources_to_overlay(yaml_file, overlay_file, dry_run=True)
            self.assertTrue(res["success"])
            self.assertEqual(res["imported_channels"], ["@valid_chan"])
            self.assertEqual(len(res["skipped_malformed"]), 2)
            self.assertIn("not_a_dict", res["skipped_malformed"])


if __name__ == "__main__":
    unittest.main()
