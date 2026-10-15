"""Atomic managed-source overlay and shared-loader tests."""

from __future__ import annotations

import os
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from common.config_loader import ConfigError, load_config
from common.managed_sources import (
    ManagedAuditPending,
    ManagedConfigConflict,
    audit_outbox_path,
    clear_pending_source_audit,
    read_pending_source_audit,
    read_overrides,
    update_overrides,
)
from resolver import admin_data


BASE = {
    "sync": {"interval_minutes": 60},
    "sources": [{
        "provider": "telegram",
        "enabled": True,
        "defaults": {"stream_mode": "proxy", "max_pages": 12,
                     "min_duration_seconds": 420, "history_batch_size": 50,
                     "tags_whitelist": [], "tags_blacklist": []},
        "channels": [{"id": "-1001", "enabled": True, "studio": "Base",
                       "max_pages": 5, "min_duration_seconds": 400,
                       "stream_mode": "proxy", "history_batch_size": 10}],
    }],
}


class TestManagedSourceOverrides(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.base_path = self.root / "config.yaml"
        self.overlay_path = self.root / "source-overrides.json"
        self.base_path.write_text(yaml.safe_dump(BASE), encoding="utf-8")
        self.previous_overlay_path = os.environ.get("ADMIN_SOURCE_OVERRIDES_PATH")
        os.environ["ADMIN_SOURCE_OVERRIDES_PATH"] = str(self.overlay_path)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self._restore_overlay_env)

    def _restore_overlay_env(self):
        if self.previous_overlay_path is None:
            os.environ.pop("ADMIN_SOURCE_OVERRIDES_PATH", None)
        else:
            os.environ["ADMIN_SOURCE_OVERRIDES_PATH"] = self.previous_overlay_path

    def test_overlay_inherits_base_and_omitting_override_resets_it(self):
        first = update_overrides(
            self.base_path,
            {"telegram": {"channels": {"-1001": {"studio": "Managed"}}}},
            expected_revision=0,
            path=self.overlay_path,
        )
        self.assertEqual(first["revision"], 1)
        self.assertEqual(load_config(str(self.base_path), force_reload=True).sources["telegram"].channels[0].studio,
                         "Managed")

        reset = update_overrides(self.base_path, {}, expected_revision=1, path=self.overlay_path)
        self.assertEqual(reset["revision"], 2)
        self.assertEqual(load_config(str(self.base_path), force_reload=True).sources["telegram"].channels[0].studio,
                         "Base")

    def test_invalid_effective_config_does_not_replace_overlay(self):
        current = update_overrides(
            self.base_path, {"telegram": {"enabled": False}},
            expected_revision=0, path=self.overlay_path,
        )
        before = self.overlay_path.read_bytes()
        with self.assertRaises(ConfigError):
            update_overrides(
                self.base_path,
                {"telegram": {"channels": {"-1001": {"max_pages": 0}}}},
                expected_revision=current["revision"],
                path=self.overlay_path,
            )
        self.assertEqual(self.overlay_path.read_bytes(), before)

    def test_channel_id_is_immutable_and_unknown_fields_are_rejected(self):
        for patch in (
            {"telegram": {"channels": {"-1002": {"enabled": False}}}},
            {"telegram": {"channels": {"-1001": {"id": "-1002"}}}},
            {"telegram": {"channels": {"-1001": {"api_key": "secret"}}}},
        ):
            with self.subTest(patch=patch), self.assertRaises(ConfigError):
                update_overrides(self.base_path, patch, expected_revision=0, path=self.overlay_path)
        self.assertFalse(self.overlay_path.exists())

    def test_new_telegram_channel_is_shared_by_config_loader_and_base_is_unchanged(self):
        original_base = self.base_path.read_bytes()
        saved = update_overrides(
            self.base_path,
            {"telegram": {"channels": {"@new_channel": {"studio": "New Studio", "enabled": True}}}},
            expected_revision=0,
            path=self.overlay_path,
        )
        self.assertEqual(saved["revision"], 1)
        config = load_config(str(self.base_path), force_reload=True)
        self.assertEqual([channel.id for channel in config.sources["telegram"].channels], ["-1001", "@new_channel"])
        self.assertEqual(config.sources["telegram"].channels[1].studio, "New Studio")
        self.assertEqual(self.base_path.read_bytes(), original_base)

    def test_new_channel_requires_studio_and_failure_keeps_overlay_untouched(self):
        for patch in (
            {"telegram": {"channels": {"-1002": {"enabled": True}}}},
        ):
            with self.subTest(patch=patch):
                before = self.overlay_path.read_bytes() if self.overlay_path.exists() else None
                with self.assertRaises(ConfigError):
                    update_overrides(self.base_path, patch, expected_revision=0, path=self.overlay_path)
                self.assertEqual(self.overlay_path.read_bytes() if self.overlay_path.exists() else None, before)

    def test_stale_revision_conflicts_and_concurrent_writers_publish_once(self):
        update_overrides(self.base_path, {}, expected_revision=0, path=self.overlay_path)
        with self.assertRaises(ManagedConfigConflict):
            update_overrides(self.base_path, {}, expected_revision=0, path=self.overlay_path)

        barrier = threading.Barrier(2)

        def write(studio):
            barrier.wait(timeout=3)
            try:
                return update_overrides(
                    self.base_path,
                    {"telegram": {"channels": {"-1001": {"studio": studio}}}},
                    expected_revision=1,
                    path=self.overlay_path,
                )["revision"]
            except ManagedConfigConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(write, ("First", "Second")))
        self.assertEqual(sorted(str(x) for x in outcomes), ["2", "conflict"])
        self.assertEqual(read_overrides(self.overlay_path)["revision"], 2)

    def test_audit_outbox_is_revision_bound_and_blocks_replacement_until_cleared(self):
        update_overrides(
            self.base_path, {"telegram": {"enabled": False}}, expected_revision=0,
            audit_event={"changes": [{"path": ["telegram", "enabled"], "before": True, "after": False}],
                         "truncated": False},
            path=self.overlay_path,
        )
        pending = read_pending_source_audit(self.overlay_path)
        self.assertEqual(pending["revision"], 1)
        self.assertEqual(read_overrides(self.overlay_path), {"revision": 1, "overrides": {"telegram": {"enabled": False}}})
        with self.assertRaises(ManagedConfigConflict):
            update_overrides(self.base_path, {}, expected_revision=0, path=self.overlay_path)
        with self.assertRaises(ManagedAuditPending):
            update_overrides(self.base_path, {}, expected_revision=1, path=self.overlay_path)
        self.assertFalse(clear_pending_source_audit(
            expected_revision=2, audit_revision=1, path=self.overlay_path,
        ))
        self.assertTrue(clear_pending_source_audit(
            expected_revision=1, audit_revision=1, path=self.overlay_path,
        ))
        self.assertIsNone(read_pending_source_audit(self.overlay_path))
        self.assertFalse(audit_outbox_path(self.overlay_path).exists())

    def test_invalid_audit_outbox_structures_are_rejected(self):
        for pending in (
            {"revision": 0, "changes": [], "truncated": False},
            {"revision": 1, "changes": [{"path": ["telegram", "enabled"]}], "truncated": False},
            {"revision": 1, "changes": [{"path": ["telegram", "enabled"], "before": True,
                                             "after": False, "raw": "extra"}], "truncated": False},
        ):
            with self.subTest(pending=pending):
                audit_outbox_path(self.overlay_path).write_text(json.dumps(pending), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    read_pending_source_audit(self.overlay_path)

    def test_orphan_outbox_for_unpublished_revision_is_discarded(self):
        audit_outbox_path(self.overlay_path).write_text(json.dumps({
            "revision": 1,
            "changes": [{"path": ["telegram", "enabled"], "before": True, "after": False}],
            "truncated": False,
        }), encoding="utf-8")
        self.assertEqual(read_overrides(self.overlay_path), {"revision": 0, "overrides": {}})
        self.assertTrue(clear_pending_source_audit(
            expected_revision=0, audit_revision=1, path=self.overlay_path,
        ))
        self.assertIsNone(read_pending_source_audit(self.overlay_path))

    def test_config_loader_and_two_service_callers_observe_same_revision(self):
        update_overrides(
            self.base_path, {"telegram": {"enabled": False}},
            expected_revision=0, path=self.overlay_path,
        )
        os.environ["ADMIN_SOURCE_OVERRIDES_PATH"] = str(self.overlay_path)
        try:
            observed = []

            def service_load():
                config = load_config(str(self.base_path), force_reload=True)
                observed.append((config.managed_revision, config.sources["telegram"].enabled))

            threads = [threading.Thread(target=service_load) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
            self.assertEqual(observed, [(1, False), (1, False)])
            public = admin_data.configured_sources(self.base_path)
            self.assertEqual(public["managed_revision"], 1)
            self.assertFalse(public["sources"][0]["enabled"])
        finally:
            os.environ["ADMIN_SOURCE_OVERRIDES_PATH"] = str(self.overlay_path)
