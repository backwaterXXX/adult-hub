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

        self.assertEqual(labelled, 5)
        self.assertEqual(cleared, 2)
        self.assertEqual(self._row("telegram:-1001:1:1"), ("", "Alpha"))
        self.assertEqual(self._row("telegram:-1002:5:5"), ("", "Beta"))
        self.assertEqual(self._row("telegram:-1001:3:3"), ("BelAmi", "Alpha"))

    def test_an_override_protects_the_studio_value(self):
        reconcile_channel_names(self.conn, self.config)

        self.assertEqual(self._row("telegram:-1002:4:4"), ("Beta", "Beta"))

    def test_other_providers_are_not_labelled(self):
        reconcile_channel_names(self.conn, self.config)

        self.assertEqual(self._row("other:6:6"), ("Alpha", ""))

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


if __name__ == "__main__":
    unittest.main()
