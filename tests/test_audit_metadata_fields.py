"""Metadata gaps are only reported when the source can actually fill them."""

from __future__ import annotations

import unittest

from sync.duplicate_job_library_audit import _missing_metadata_fields


def _source(**overrides):
    base = {"title": "Scene", "studio": "Studio", "thumbnail": "http://thumb/x.jpg",
            "scene_matched_at": None}
    base.update(overrides)
    return base


def _effective(**overrides):
    base = {"title": "Scene", "studio": "Studio", "performers": [], "plot": ""}
    base.update(overrides)
    return base


class MissingMetadataFieldsTest(unittest.TestCase):
    def test_unmatched_item_does_not_report_plot_or_performers(self):
        fields = _missing_metadata_fields(_effective(), _source())

        self.assertEqual(fields, [])

    def test_unmatched_item_still_reports_studio_title_and_thumbnail(self):
        fields = _missing_metadata_fields(
            _effective(studio="", title=""), _source(studio="", thumbnail=""))

        self.assertEqual(fields, ["title", "studio", "thumbnail"])

    def test_matched_item_reports_plot_and_performers(self):
        fields = _missing_metadata_fields(
            _effective(), _source(scene_matched_at="2026-11-01 10:00:00+00"))

        self.assertEqual(fields, ["performers", "plot"])

    def test_matched_item_with_metadata_reports_nothing(self):
        fields = _missing_metadata_fields(
            _effective(plot="A plot", performers=["Jane Doe"]),
            _source(scene_matched_at="2026-11-01 10:00:00+00"))

        self.assertEqual(fields, [])

    def test_blank_scene_match_is_not_enrichable(self):
        fields = _missing_metadata_fields(_effective(), _source(scene_matched_at="   "))

        self.assertEqual(fields, [])

    def test_overrides_fill_the_gap(self):
        fields = _missing_metadata_fields(
            _effective(plot="", performers=[]),
            _source(scene_matched_at="2026-11-01 10:00:00+00"))

        self.assertEqual(fields, ["performers", "plot"])
        fields = _missing_metadata_fields(
            _effective(plot="Override plot", performers=["Jane Doe"]),
            _source(scene_matched_at="2026-11-01 10:00:00+00"))
        self.assertEqual(fields, [])


if __name__ == "__main__":
    unittest.main()
