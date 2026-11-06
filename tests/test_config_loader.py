import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common.config_loader import ConfigError, parse_and_validate_config


class TestConfigLoader(unittest.TestCase):

    def test_positive_integer_rejects_boolean(self):
        from common.config_loader import _validate_positive_int
        for value in (True, False):
            with self.subTest(value=value), self.assertRaises(ConfigError):
                _validate_positive_int(value, "max_pages", "test", minimum=0)

    def test_valid_minimal_config(self):
        data = {
            "sync": {
                "interval_minutes": 30,
                "retention_days": 15,
            },
            "sources": [
                {
                    "provider": "telegram",
                    "enabled": True,
                    "defaults": {
                        "stream_mode": "proxy",
                        "max_pages": 1,
                        "min_duration_seconds": 420,
                    },
                    "channels": [
                        {"id": "-100123456789", "studio": "Chan 1", "min_duration_seconds": 500},
                        {"id": "@chan2", "enabled": False},
                    ],
                },
            ],
        }
        cfg = parse_and_validate_config(data)
        self.assertEqual(cfg.sync.interval_minutes, 30)
        self.assertEqual(cfg.sync.retention_days, 15)
        self.assertIsNone(cfg.sync.daily_at)
        self.assertTrue(cfg.sync.run_on_startup)

        tg = cfg.get_source("telegram")
        self.assertTrue(tg.enabled)
        self.assertEqual(len(tg.channels), 2)
        ch1 = tg.get_channel("-100123456789")
        self.assertIsNotNone(ch1)
        self.assertEqual(ch1.studio, "Chan 1")
        self.assertEqual(ch1.min_duration_seconds, 500)
        self.assertEqual(ch1.max_pages, 1)  # inherited from defaults

        ch2 = tg.get_channel("@chan2")
        self.assertIsNotNone(ch2)
        self.assertFalse(ch2.enabled)
        self.assertEqual(ch2.min_duration_seconds, 420)  # inherited from defaults

        self.assertTrue(cfg.is_telegram_channel_enabled("-100123456789"))
        self.assertFalse(cfg.is_telegram_channel_enabled("@chan2"))

    def test_sync_daily_at_and_run_on_startup(self):
        # Valid daily_at
        cfg = parse_and_validate_config({"sync": {"daily_at": "03:30", "run_on_startup": False}})
        self.assertEqual(cfg.sync.daily_at, "03:30")
        self.assertFalse(cfg.sync.run_on_startup)

        # Single-digit hour normalized
        cfg2 = parse_and_validate_config({"sync": {"daily_at": "4:05"}})
        self.assertEqual(cfg2.sync.daily_at, "04:05")
        self.assertTrue(cfg2.sync.run_on_startup)

        # Invalid daily_at values rejected
        for bad in ("25:00", "03:60", "night", "12", "12:00:00", True, 123):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                parse_and_validate_config({"sync": {"daily_at": bad}})

        # Non-boolean run_on_startup rejected
        with self.assertRaises(ConfigError):
            parse_and_validate_config({"sync": {"run_on_startup": "yes"}})

    def test_duplicate_provider_rejected(self):
        data = {
            "sources": [
                {"provider": "telegram", "channels": [{"id": "@one"}]},
                {"provider": "telegram", "channels": [{"id": "@two"}]},
            ]
        }
        with self.assertRaises(ConfigError) as ctx:
            parse_and_validate_config(data)
        self.assertIn("Duplicate provider", str(ctx.exception))

    def test_duplicate_telegram_channel_rejected(self):
        data = {
            "sources": [
                {
                    "provider": "telegram",
                    "channels": [
                        {"id": "-100123456789"},
                        {"id": "-100123456789"},
                    ],
                }
            ],
        }
        with self.assertRaises(ConfigError) as ctx:
            parse_and_validate_config(data)
        self.assertIn("Duplicate telegram channel id", str(ctx.exception))

    def test_unknown_top_level_key_rejected(self):
        data = {"sources": [], "invalid_key": 123}
        with self.assertRaises(ConfigError) as ctx:
            parse_and_validate_config(data)
        self.assertIn("Unknown top-level configuration key", str(ctx.exception))

    def test_unknown_sync_key_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_and_validate_config({"sync": {"not_a_real_setting": 1}})
        self.assertIn("Unknown key(s) in 'sync' section", str(ctx.exception))

    def test_unknown_telegram_field_rejected(self):
        data = {
            "sources": [
                {
                    "provider": "telegram",
                    "channels": [{"id": "@chan", "non_existent_field": True}],
                }
            ]
        }
        with self.assertRaises(ConfigError) as ctx:
            parse_and_validate_config(data)
        self.assertIn("Unknown field(s) in telegram channel", str(ctx.exception))

    def test_telegram_with_address_rejected(self):
        data = {
            "sources": [
                {
                    "provider": "telegram",
                    "address": "https://t.me",
                }
            ]
        }
        with self.assertRaises(ConfigError) as ctx:
            parse_and_validate_config(data)
        self.assertIn("Telegram source does not take 'address'", str(ctx.exception))

    def test_disabled_provider_and_channel_helpers(self):
        data = {
            "sources": [
                {
                    "provider": "telegram",
                    "enabled": True,
                    "channels": [
                        {"id": "-1000000000001", "enabled": True},
                        {"id": "-1000000000002", "enabled": False},
                    ],
                },
            ]
        }
        cfg = parse_and_validate_config(data)
        self.assertNotIn("telegram", cfg.get_disabled_provider_names())

        disabled_chans = cfg.get_disabled_telegram_channel_ids()
        self.assertIn("-1000000000002", disabled_chans)
        self.assertIn("0000000002", disabled_chans)
        self.assertNotIn("-1000000000001", disabled_chans)


class TestTelegramOnlyValidation(unittest.TestCase):
    """The core is Telegram-only: every other provider is rejected up front."""

    def _source(self, provider, **over):
        src = {"provider": provider}
        src.update(over)
        return {"sources": [src]}

    def test_malformed_provider_id_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_and_validate_config(self._source("sample-provider"))
        self.assertIn("lowercase letters and digits", str(ctx.exception))

    def test_non_telegram_provider_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_and_validate_config(self._source("sampleprovider", address="https://example.com"))
        self.assertIn("Telegram-only", str(ctx.exception))

    def test_non_telegram_provider_rejected_without_address(self):
        with self.assertRaises(ConfigError) as ctx:
            parse_and_validate_config(self._source("sampleprovider"))
        self.assertIn("Telegram-only", str(ctx.exception))


class ConfigFileSignatureTest(unittest.TestCase):
    def test_parent_path_that_is_a_file_counts_as_absent(self):
        import tempfile
        from common.config_loader import _config_file_signature

        with tempfile.TemporaryDirectory() as tmp:
            plain_file = Path(tmp) / "plain.txt"
            plain_file.write_text("not a directory", encoding="utf-8")
            self.assertIsNone(_config_file_signature(plain_file / "overrides.json"))


if __name__ == "__main__":
    unittest.main()
