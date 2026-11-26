"""The channel label and the studio are two different columns."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from sync.channel_backfill import channel_studio_map, reconcile_channel_names
from tests.pg_helper import PgTestCase


def _config(pairs):
    channels = [SimpleNamespace(id=channel_id, studio=label) for channel_id, label in pairs]
    return SimpleNamespace(
        get_enabled_sources=lambda: [SimpleNamespace(provider="telegram", channels=channels)]
    )


class ChannelStudioMapTests(unittest.TestCase):
    def test_only_labelled_telegram_channels_are_mapped(self):
        config = SimpleNamespace(get_enabled_sources=lambda: [
            SimpleNamespace(provider="telegram", channels=[
                SimpleNamespace(id="-1001", studio=" Alpha "),
                SimpleNamespace(id="-1002", studio=""),
            ]),
            SimpleNamespace(provider="stashdb", channels=[
                SimpleNamespace(id="scene-1", studio="Ignored"),
            ]),
        ])

        self.assertEqual(channel_studio_map(config), {"-1001": "Alpha"})

    def test_missing_configuration_maps_nothing(self):
        self.assertEqual(channel_studio_map(None), {})


class ChannelReconciliationTests(PgTestCase):
    def setUp(self):
        super().setUp()
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id,provider,title,duration,playable,studio,channel_name) VALUES "
                # channel label in studio from the legacy sync
                "('telegram:-1001:1:1','telegram','One',900,1,'Alpha',''),"
                # no studio at all
                "('telegram:-1001:2:2','telegram','Two',900,1,'',NULL),"
                # a real studio stays
                "('telegram:-1001:3:3','telegram','Three',900,1,'BelAmi',''),"
                # an operator cleared the studio with an override
                "('telegram:-1002:4:4','telegram','Four',900,1,'Beta',''),"
                # another channel label
                "('telegram:-1002:5:5','telegram','Five',900,1,'Beta',NULL),"
                # a label from a channel this item does not belong to
                "('telegram:-1002:7:7','telegram','Seven',900,1,'Alpha',''),"
                # a channel dropped from the config kept only its handle
                "('telegram:abandoned:8:8','telegram','Eight',900,1,'abandoned',''),"
                # a channel named after the studio it publishes keeps the studio
                "('telegram:belami:9:9','telegram','Nine',900,1,'belami',''),"
                "('other:6:6','other','Six',900,1,'Alpha','')"
            )
            self.conn.execute(
                "INSERT INTO video_metadata_overrides (video_id,fields,revision) "
                "VALUES ('telegram:-1002:4:4',%s::jsonb,1)",
                (json.dumps({"studio": ""}),),
            )
        self.config = _config([("-1001", "Alpha"), ("-1002", "Beta")])

    def _row(self, video_id):
        row = self.conn.execute(
            "SELECT coalesce(studio,'<null>'), coalesce(channel_name,'<null>') FROM videos WHERE id=%s",
            (video_id,),
        ).fetchone()
        return str(row[0]), str(row[1])

    def test_channel_labels_move_out_of_studio(self):
        labelled, cleared = reconcile_channel_names(self.conn, self.config)

        self.assertEqual(labelled, 6)
        self.assertEqual(cleared, 4)
        self.assertEqual(self._row("telegram:-1001:1:1"), ("", "Alpha"))
        self.assertEqual(self._row("telegram:-1002:5:5"), ("", "Beta"))
        self.assertEqual(self._row("telegram:-1001:3:3"), ("BelAmi", "Alpha"))
        self.assertEqual(self._row("telegram:-1002:7:7"), ("", "Beta"))

    def test_an_override_protects_the_studio_value(self):
        reconcile_channel_names(self.conn, self.config)

        self.assertEqual(self._row("telegram:-1002:4:4"), ("Beta", "Beta"))

    def test_other_providers_are_not_labelled(self):
        reconcile_channel_names(self.conn, self.config)

        self.assertEqual(self._row("other:6:6"), ("Alpha", ""))

    def test_an_abandoned_channel_handle_leaves_the_studio(self):
        reconcile_channel_names(self.conn, self.config)

        self.assertEqual(self._row("telegram:abandoned:8:8"), ("", ""))

    def test_a_channel_named_after_its_studio_keeps_it(self):
        reconcile_channel_names(self.conn, self.config)

        self.assertEqual(self._row("telegram:belami:9:9"), ("belami", ""))

    def test_a_second_pass_changes_nothing(self):
        reconcile_channel_names(self.conn, self.config)

        self.assertEqual(reconcile_channel_names(self.conn, self.config), (0, 0))


class MissingMetadataRuleTests(unittest.TestCase):
    def test_studio_is_only_reported_when_a_scene_match_can_fill_it(self):
        from sync.duplicate_job_library_audit import _missing_metadata_fields

        unmatched = _missing_metadata_fields({"title": "T", "studio": ""}, {"thumbnail": "x"})
        matched = _missing_metadata_fields(
            {"title": "T", "studio": ""}, {"thumbnail": "x", "scene_matched_at": "2026-01-01"}
        )

        self.assertNotIn("studio", unmatched)
        self.assertIn("studio", matched)


class ChannelLabelHeuristicTests(unittest.TestCase):
    def test_a_channel_title_is_not_a_studio(self):
        from sync.studio_tags import looks_like_channel_label

        self.assertTrue(looks_like_channel_label("Lukayan SHOW"))
        self.assertTrue(looks_like_channel_label("naughtyandhorny"))
        self.assertTrue(looks_like_channel_label("telegram"))

    def test_a_known_production_studio_is_not_a_channel_label(self):
        from sync.studio_tags import looks_like_channel_label

        self.assertFalse(looks_like_channel_label("BelAmi"))
        self.assertFalse(looks_like_channel_label("Men.com"))

    def test_an_empty_value_is_not_a_label(self):
        from sync.studio_tags import looks_like_channel_label

        self.assertFalse(looks_like_channel_label(""))
        self.assertFalse(looks_like_channel_label(None))


class OwnChannelLabelTests(unittest.TestCase):
    def test_a_value_equal_to_the_items_channel_is_a_channel_name(self):
        from sync.video_persistence import _is_own_channel_label

        video = {"channel_name": "Lukayan SHOW"}
        self.assertTrue(_is_own_channel_label("lukayan show", video))
        self.assertFalse(_is_own_channel_label("BelAmi", video))
        self.assertFalse(_is_own_channel_label("Lukayan SHOW", {}))

    def test_a_studio_named_channel_keeps_its_studio(self):
        from sync.video_persistence import _is_own_channel_label

        self.assertFalse(_is_own_channel_label("Men.com", {"channel_name": "Men.com"}))


if __name__ == "__main__":
    unittest.main()
