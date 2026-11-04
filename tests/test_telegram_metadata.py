"""Tests for telegram metadata extraction, signature handling, and deployment contract."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch
import yaml

from common.telegram_metadata import extract_telegram_metadata, resolve_explicit_cast


ROOT = Path(__file__).resolve().parents[1]


class TestTelegramMetadataSignature(unittest.TestCase):
    def test_signature_empty_when_env_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            caption = "@secret_channel\nPerformer One, Performer Two\nSome Title"
            result = extract_telegram_metadata(caption)
            self.assertEqual(result["signature_name_candidates"], [])

    def test_signature_recognized_when_env_matches(self):
        with patch.dict(os.environ, {"TELEGRAM_SIGNATURE_CHANNEL": "@trusted_channel"}):
            caption = "@trusted_channel\nPerformer One, Performer Two\nGreat Scene"
            result = extract_telegram_metadata(caption)
            self.assertEqual(result["signature_name_candidates"], ["Performer One", "Performer Two"])

    def test_signature_case_insensitive_matching(self):
        with patch.dict(os.environ, {"TELEGRAM_SIGNATURE_CHANNEL": "@Trusted_Channel"}):
            caption = "@trusted_channel\nPerformer One & Performer Two\nScene Name"
            result = extract_telegram_metadata(caption)
            self.assertEqual(result["signature_name_candidates"], ["Performer One", "Performer Two"])

    def test_signature_absent_when_caption_does_not_contain_channel(self):
        with patch.dict(os.environ, {"TELEGRAM_SIGNATURE_CHANNEL": "@trusted_channel"}):
            caption = "@different_channel\nPerformer One, Performer Two\nGreat Scene"
            result = extract_telegram_metadata(caption)
            self.assertEqual(result["signature_name_candidates"], [])

    def test_labeled_cast_takes_precedence_over_signature(self):
        with patch.dict(os.environ, {"TELEGRAM_SIGNATURE_CHANNEL": "@trusted_channel"}):
            caption = "Performer Alpha, Performer Beta\nCast: Labeled One, Labeled Two\n@trusted_channel"
            result = extract_telegram_metadata(caption)
            self.assertEqual(result["performer_names"], ["Labeled One", "Labeled Two"])
            self.assertEqual(result["signature_name_candidates"], [])

    def test_downstream_resolve_explicit_cast_with_signature_names(self):
        catalog = [
            {"id": "stashdb:111", "name": "Alice Wonder", "aliases": ["Alice W"]},
            {"id": "stashdb:222", "name": "Bob Builder", "aliases": []},
        ]
        names = ["Alice W", "Unknown Performer"]
        resolved_names, resolved_ids = resolve_explicit_cast(names, catalog)
        self.assertEqual(resolved_names, ["Alice Wonder", "Unknown Performer"])
        self.assertEqual(resolved_ids, ["stashdb:111", ""])


class TestDeploymentConfigContract(unittest.TestCase):
    def setUp(self):
        compose_file = ROOT / "docker-compose.yml"
        self.assertTrue(compose_file.exists(), "docker-compose.yml must exist")
        with open(compose_file, encoding="utf-8") as f:
            self.compose_data = yaml.safe_load(f)

    def _get_service_env_dict(self, service_name: str) -> dict[str, str]:
        services = self.compose_data.get("services", {})
        self.assertIn(service_name, services, f"Service '{service_name}' must exist in docker-compose.yml")
        env_list = services[service_name].get("environment", [])
        env_dict = {}
        for entry in env_list:
            if isinstance(entry, str) and "=" in entry:
                key, val = entry.split("=", 1)
                env_dict[key.strip()] = val.strip()
        return env_dict

    def test_resolver_has_both_metadata_variables(self):
        env = self._get_service_env_dict("resolver")
        self.assertIn("TELEGRAM_SIGNATURE_CHANNEL", env, "resolver must receive TELEGRAM_SIGNATURE_CHANNEL")
        self.assertEqual(
            env["TELEGRAM_SIGNATURE_CHANNEL"],
            "${TELEGRAM_SIGNATURE_CHANNEL:-}",
            "TELEGRAM_SIGNATURE_CHANNEL must use safe empty default interpolation",
        )
        self.assertIn("TELEGRAM_CHANNEL_NAME_HINTS", env, "resolver must receive TELEGRAM_CHANNEL_NAME_HINTS")
        self.assertEqual(
            env["TELEGRAM_CHANNEL_NAME_HINTS"],
            "${TELEGRAM_CHANNEL_NAME_HINTS:-}",
            "TELEGRAM_CHANNEL_NAME_HINTS must use safe empty default interpolation",
        )

    def test_sync_has_both_metadata_variables(self):
        env = self._get_service_env_dict("sync")
        self.assertIn("TELEGRAM_SIGNATURE_CHANNEL", env, "sync must receive TELEGRAM_SIGNATURE_CHANNEL")
        self.assertEqual(
            env["TELEGRAM_SIGNATURE_CHANNEL"],
            "${TELEGRAM_SIGNATURE_CHANNEL:-}",
            "TELEGRAM_SIGNATURE_CHANNEL must use safe empty default interpolation",
        )
        self.assertIn("TELEGRAM_CHANNEL_NAME_HINTS", env, "sync must receive TELEGRAM_CHANNEL_NAME_HINTS")
        self.assertEqual(
            env["TELEGRAM_CHANNEL_NAME_HINTS"],
            "${TELEGRAM_CHANNEL_NAME_HINTS:-}",
            "TELEGRAM_CHANNEL_NAME_HINTS must use safe empty default interpolation",
        )

    def test_env_example_documents_both_variables(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_SIGNATURE_CHANNEL=", env_example)
        self.assertIn("TELEGRAM_CHANNEL_NAME_HINTS=", env_example)
