"""An empty studio is filled from the channel label the configuration defines."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from sync.studio_backfill import backfill_channel_studio, channel_studio_map
from tests.pg_helper import PgTestCase


def _config(pairs):
    channels = [SimpleNamespace(id=channel_id, studio=studio) for channel_id, studio in pairs]
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


class StudioBackfillTests(PgTestCase):
    def setUp(self):
        super().setUp()
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id,provider,title,duration,playable,studio) VALUES "
                "('telegram:-1001:1:1','telegram','One',900,1,''),"
                "('telegram:-1001:2:2','telegram','Two',900,1,NULL),"
                "('telegram:-1001:3:3','telegram','Three',900,1,'Own Studio'),"
                "('telegram:-1002:4:4','telegram','Four',900,1,''),"
                "('telegram:-1002:5:5','telegram','Five',900,1,''),"
                "('other:6:6','other','Six',900,1,'')"
            )
            self.conn.execute(
                "INSERT INTO video_metadata_overrides (video_id,fields,revision) "
                "VALUES ('telegram:-1002:5:5',%s::jsonb,1)",
                (json.dumps({"studio": ""}),),
            )
        self.config = _config([("-1001", "Alpha"), ("-1002", "Beta")])

    def _studios(self):
        rows = self.conn.execute(
            "SELECT id, coalesce(studio,'<null>') FROM videos ORDER BY id"
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def test_empty_studios_inherit_the_channel_label(self):
        updated = backfill_channel_studio(self.conn, self.config)

        studios = self._studios()
        self.assertEqual(updated, 3)
        self.assertEqual(studios["telegram:-1001:1:1"], "Alpha")
        self.assertEqual(studios["telegram:-1001:2:2"], "Alpha")
        self.assertEqual(studios["telegram:-1001:3:3"], "Own Studio")
        self.assertEqual(studios["telegram:-1002:4:4"], "Beta")

    def test_an_explicit_studio_override_keeps_the_field_empty(self):
        backfill_channel_studio(self.conn, self.config)

        self.assertEqual(self._studios()["telegram:-1002:5:5"], "")
        overrides = self.conn.execute(
            "SELECT fields FROM video_metadata_overrides WHERE video_id='telegram:-1002:5:5'"
        ).fetchone()[0]
        self.assertEqual(json.loads(overrides), {"studio": ""})

    def test_other_providers_are_not_touched(self):
        backfill_channel_studio(self.conn, self.config)

        self.assertEqual(self._studios()["other:6:6"], "")

    def test_a_second_pass_finds_nothing_to_fill(self):
        backfill_channel_studio(self.conn, self.config)

        self.assertEqual(backfill_channel_studio(self.conn, self.config), 0)


if __name__ == "__main__":
    unittest.main()
