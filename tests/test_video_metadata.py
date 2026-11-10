"""Contract tests for source and overridden video metadata semantics."""

import unittest


from tests.pg_helper import PgTestCase

from common.video_metadata import (
    MetadataValidationError,
    load_metadata_state,
    resolve_metadata,
    update_metadata_state,
    validate_override_patch,
)


class TestVideoMetadataOverrides(unittest.TestCase):
    def test_missing_override_inherits_latest_source(self):
        source = {"title": "First title", "plot": "Source description"}
        first = resolve_metadata(source, {})
        self.assertEqual(first["title"], "First title")
        self.assertFalse(first["metadata_fields"]["title"]["overridden"])

        source["title"] = "Updated by sync"
        refreshed = resolve_metadata(source, {})
        self.assertEqual(refreshed["title"], "Updated by sync")

    def test_empty_text_is_an_override_and_survives_source_change(self):
        source = {"title": "Source title", "plot": "Source description"}
        overrides = {"plot": ""}
        resolved = resolve_metadata(source, overrides)
        self.assertEqual(resolved["plot"], "")
        self.assertEqual(resolved["metadata_fields"]["plot"], {
            "source": "Source description", "effective": "", "overridden": True,
        })

        source["plot"] = "New source description"
        self.assertEqual(resolve_metadata(source, overrides)["plot"], "")

    def test_reset_removes_override_and_reveals_latest_source(self):
        patch, resets = validate_override_patch({}, ["title"])
        overrides = {"title": "Manual title", "plot": "Manual plot"}
        for field in resets:
            overrides.pop(field, None)
        self.assertEqual(patch, {})
        self.assertEqual(resolve_metadata({"title": "Latest title", "plot": "Manual plot"}, overrides)["title"], "Latest title")

    def test_validator_preserves_clear_and_rejects_unknown_or_oversized_values(self):
        patch, resets = validate_override_patch({"title": "  ", "plot": "Line one\nLine two"})
        self.assertEqual(patch, {"title": "", "plot": "Line one\nLine two"})
        self.assertEqual(resets, [])
        with self.assertRaises(MetadataValidationError):
            validate_override_patch({"thumbnail": "https://example.invalid/image.jpg"})
        with self.assertRaises(MetadataValidationError):
            validate_override_patch({"title": "x" * 501})
        with self.assertRaises(MetadataValidationError):
            validate_override_patch({"title": "A"}, ["title"])

    def test_validator_supports_lists_studio_and_iso_release_date(self):
        patch, _ = validate_override_patch({
            "studio": " Studio ", "performers": [" Alex ", "Alex", "Sam"],
            "tags": [], "date_released": "2026-02-03",
        })
        self.assertEqual(patch, {
            "studio": "Studio", "performers": ["Alex", "Sam"],
            "tags": [], "date_released": "2026-02-03",
        })
        with self.assertRaises(MetadataValidationError):
            validate_override_patch({"performers": ["", "Actor"]})
        with self.assertRaises(MetadataValidationError):
            validate_override_patch({"date_released": "03/02/2026"})


class TestPersistentVideoMetadataOverrides(PgTestCase):
    def setUp(self):
        super().setUp()
        with self.conn:
            self.conn.execute(
                "INSERT INTO videos (id,provider,title,plot,performers,categories,tags) "
                "VALUES ('telegram:10:metadata','telegram','Source title','Source plot','[]','[]','[]')"
            )

    def test_revisioned_empty_override_survives_source_change_and_reset_inherits(self):
        with self.conn:
            first = update_metadata_state(self.conn, "telegram:10:metadata", 0, {"title": ""})
        self.assertEqual(first["outcome"], "saved")
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["metadata"]["title"], "")

        with self.conn:
            self.conn.execute("UPDATE videos SET title='Latest source title' WHERE id='telegram:10:metadata'")
        current = load_metadata_state(self.conn, "telegram:10:metadata")
        self.assertEqual(current["metadata"]["title"], "")
        self.assertEqual(current["metadata"]["metadata_fields"]["title"]["source"], "Latest source title")

        with self.conn:
            reset = update_metadata_state(self.conn, "telegram:10:metadata", 1, {}, ["title"])
        self.assertEqual(reset["revision"], 2)
        self.assertEqual(reset["metadata"]["title"], "Latest source title")
        self.assertFalse(reset["metadata"]["metadata_fields"]["title"]["overridden"])

    def test_stale_revision_is_rejected_without_changing_the_override(self):
        with self.conn:
            first = update_metadata_state(self.conn, "telegram:10:metadata", 0, {"plot": "Edited"})
        with self.conn:
            stale = update_metadata_state(self.conn, "telegram:10:metadata", 0, {"plot": "Lost update"})
        self.assertEqual(first["revision"], 1)
        self.assertEqual(stale["outcome"], "stale")
        self.assertEqual(stale["revision"], 1)
        self.assertEqual(load_metadata_state(self.conn, "telegram:10:metadata")["metadata"]["plot"], "Edited")

    def test_array_and_date_overrides_keep_source_values_separate(self):
        with self.conn:
            self.conn.execute(
                "UPDATE videos SET performers='[\"Source actor\"]',tags='[\"Source tag\"]',"
                "studio='Source studio',date_released='2020-01-01' WHERE id='telegram:10:metadata'"
            )
            saved = update_metadata_state(self.conn, "telegram:10:metadata", 0, {
                "performers": ["Manual actor"], "tags": [],
                "studio": "Manual studio", "date_released": "2026-02-03",
            })
        self.assertEqual(saved["metadata"]["performers"], ["Manual actor"])
        self.assertEqual(saved["metadata"]["tags"], [])
        self.assertEqual(saved["metadata"]["studio"], "Manual studio")
        self.assertEqual(saved["metadata"]["date_released"], "2026-02-03")
        self.assertEqual(saved["metadata"]["metadata_fields"]["performers"]["source"], ["Source actor"])


if __name__ == "__main__":
    unittest.main()
