"""Small helpers behind the channel metadata report and backfill scripts."""

from __future__ import annotations

import unittest

from scripts.backfill_channel_performers import _clean_names
from scripts.backfill_channel_scenes import _title_variants
from scripts.metadata_quality import build_report

from tests.pg_helper import PgTestCase


class TitleVariantTests(unittest.TestCase):
    def test_studio_prefix_and_scene_suffix_are_removed(self):
        variants = _title_variants("Raw Strokes – Bareback Pursuit s1")
        self.assertIn("Bareback Pursuit", variants)
        self.assertIn("Raw Strokes – Bareback Pursuit", variants)

    def test_year_and_multi_part_title_produce_a_short_variant(self):
        variants = _title_variants("Gordi Films Switzerland – Raw & Kinky (2008) s2")
        self.assertIn("Raw & Kinky", variants)

    def test_plain_title_is_kept_once(self):
        self.assertEqual(_title_variants("Lustful Desires"), ["Lustful Desires"])


class CastCandidateFilterTests(unittest.TestCase):
    def test_known_labels_and_blank_names_are_dropped(self):
        self.assertEqual(
            _clean_names(["Malik Delgaty", "Naughty", "Young", "", None, "Malik Delgaty"]),
            ["Malik Delgaty"],
        )


class MetadataReportTests(PgTestCase):
    def test_counts_overall_and_by_channel(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_duplicate_jobs (id,action,state,payload,created_at,updated_at) "
                "VALUES ('audit-job','metadata_quality_audit','done','{}',now(),now())"
            )
            self.conn.execute(
                "INSERT INTO videos (id,provider,title,duration,playable,channel_name,plot,studio,performers) VALUES "
                "('telegram:1:1:1','telegram','One',900,1,'Channel A','plot','Studio','[\"Malik Delgaty\"]'::jsonb),"
                "('telegram:1:2:2','telegram','Two',900,1,'Channel A','','','[]'::jsonb),"
                "('telegram:2:3:3','telegram','Three',900,1,'Channel B','plot','','[]'::jsonb)"
            )
            self.conn.execute(
                "INSERT INTO admin_audit_items (job_id,video_id,kind,status,evidence,details) VALUES "
                "('audit-job','telegram:1:2:2','missing_metadata','open','missing','{}')"
            )

        report = build_report(self.conn, limit=10)
        self.assertEqual(report["overall"]["videos"], 3)
        self.assertEqual(report["overall"]["no_plot"], 1)
        self.assertEqual(report["overall"]["no_studio"], 2)
        self.assertEqual(report["overall"]["no_cast"], 2)
        self.assertEqual(report["overall"]["open_missing_metadata"], 1)
        channels = {row["channel"]: row for row in report["channels"]}
        self.assertEqual(channels["Channel A"]["no_cast"], 1)
        self.assertEqual(channels["Channel A"]["open_missing_metadata"], 1)
        self.assertEqual(channels["Channel B"]["no_studio"], 1)


if __name__ == "__main__":
    unittest.main()
