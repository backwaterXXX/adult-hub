import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from provider_registry import (
    _PROVIDER_INFO,
    stream_mode_for_provider,
    get_provider_info,
    known_provider_names,
    dead_stream_providers,
    performer_backfill_providers,
    performer_audit_providers,
    _telegram_channel_of_video,
    is_cleanup_allowed,
)


class TestProviderRegistry(unittest.TestCase):
    def setUp(self):
        self._orig_info = dict(_PROVIDER_INFO)

    def tearDown(self):
        _PROVIDER_INFO.clear()
        _PROVIDER_INFO.update(self._orig_info)

    def test_stream_mode_fallback_and_config_override(self):
        self.assertEqual(stream_mode_for_provider("telegram:123"), "proxy")
        self.assertEqual(stream_mode_for_provider("unknown:123"), "redirect")

        # Config override
        cfg = MagicMock()
        mock_source = MagicMock()
        mock_source.stream_mode = "redirect"
        cfg.get_source.return_value = mock_source
        self.assertEqual(stream_mode_for_provider("telegram:123", config=cfg), "redirect")

    def test_provider_info_telegram_and_fallback(self):
        self.assertIn("telegram", known_provider_names())
        tg_info = get_provider_info("telegram")
        self.assertEqual(tg_info["name"], "Telegram")
        self.assertEqual(tg_info["domain"], "t.me")

        # Historical fallback
        fallback = get_provider_info("historical")
        self.assertEqual(fallback["name"], "Historical")
        self.assertEqual(fallback["domain"], "historical")

    def test_upkeep_provider_lists_are_empty_telegram_only(self):
        self.assertEqual(dead_stream_providers(), ())
        self.assertEqual(performer_backfill_providers(), ())
        self.assertEqual(performer_audit_providers(), ())

    def test_telegram_channel_parsing(self):
        self.assertEqual(_telegram_channel_of_video("telegram:-100123456:789"), "-100123456")
        self.assertEqual(_telegram_channel_of_video("telegram:mychan:789"), "mychan")
        self.assertIsNone(_telegram_channel_of_video("telegram:123"))
        self.assertIsNone(_telegram_channel_of_video("other:123"))

    def test_is_cleanup_allowed(self):
        # Protected when providers_succeeded is None
        self.assertFalse(is_cleanup_allowed("telegram:-1001:1", providers_succeeded=None))

        # Unknown provider
        self.assertFalse(is_cleanup_allowed("unknown:1", providers_succeeded={"unknown"}))

    def test_is_cleanup_allowed_telegram_channels(self):
        cfg = MagicMock()
        cfg.canonical_provider_name.side_effect = lambda x: x
        source_mock = MagicMock()
        source_mock.enabled = True
        cfg.get_source.return_value = source_mock
        cfg.is_telegram_channel_enabled.side_effect = lambda ch: ch in ("-100123456", "123456", "enabledchan")

        # Global telegram in success set (channel-level success required)
        self.assertFalse(is_cleanup_allowed("telegram:-100123456:1", providers_succeeded={"telegram"}, config=cfg))

        # Disabled channel
        self.assertFalse(is_cleanup_allowed("telegram:-100999999:1", providers_succeeded={"telegram"}, config=cfg))

        # Channel specific success with -100 prefix matching
        self.assertTrue(is_cleanup_allowed("telegram:-100123456:1", providers_succeeded={"telegram:123456"}, config=cfg))
        self.assertTrue(is_cleanup_allowed("telegram:123456:1", providers_succeeded={"telegram:-100123456"}, config=cfg))


if __name__ == "__main__":
    unittest.main()
