"""Contract tests for source and overridden video metadata semantics."""

import json
import unittest


from tests.pg_helper import PgTestCase

from common.video_metadata import (
    MetadataValidationError,
    load_metadata_state,
    resolve_metadata,
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

    def _save_override(self, fields, revision):
        """Write an override row the way the durable metadata job does."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO video_metadata_overrides (video_id,fields,revision,updated_at) "
                "VALUES ('telegram:10:metadata',%s::jsonb,%s,CURRENT_TIMESTAMP) "
                "ON CONFLICT (video_id) DO UPDATE SET fields=EXCLUDED.fields,"
                "revision=EXCLUDED.revision,updated_at=CURRENT_TIMESTAMP",
                (json.dumps(fields), revision),
            )

    def test_revisioned_empty_override_survives_source_change_and_reset_inherits(self):
        self._save_override({"title": ""}, 1)
        first = load_metadata_state(self.conn, "telegram:10:metadata")
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["metadata"]["title"], "")

        with self.conn:
            self.conn.execute("UPDATE videos SET title='Latest source title' WHERE id='telegram:10:metadata'")
        current = load_metadata_state(self.conn, "telegram:10:metadata")
        self.assertEqual(current["metadata"]["title"], "")
        self.assertEqual(current["metadata"]["metadata_fields"]["title"]["source"], "Latest source title")

        # Resetting the field drops it from the override row and reveals the source again.
        self._save_override({}, 2)
        reset = load_metadata_state(self.conn, "telegram:10:metadata")
        self.assertEqual(reset["revision"], 2)
        self.assertEqual(reset["metadata"]["title"], "Latest source title")
        self.assertFalse(reset["metadata"]["metadata_fields"]["title"]["overridden"])

    def test_override_state_exposes_raw_fields_and_updated_at(self):
        self._save_override({"plot": "Edited"}, 1)
        state = load_metadata_state(self.conn, "telegram:10:metadata")
        self.assertEqual(state["overrides"], {"plot": "Edited"})
        self.assertIsNotNone(state["updated_at"])
        self.assertEqual(state["metadata"]["plot"], "Edited")
        self.assertEqual(state["metadata"]["metadata_fields"]["plot"]["source"], "Source plot")

    def test_array_and_date_overrides_keep_source_values_separate(self):
        with self.conn:
            self.conn.execute(
                "UPDATE videos SET performers='[\"Source actor\"]',tags='[\"Source tag\"]',"
                "studio='Source studio',date_released='2020-01-01' WHERE id='telegram:10:metadata'"
            )
        self._save_override({"performers": ["Manual actor"], "tags": [],
                             "studio": "Manual studio", "date_released": "2026-02-03"}, 1)
        saved = load_metadata_state(self.conn, "telegram:10:metadata")
        self.assertEqual(saved["metadata"]["performers"], ["Manual actor"])
        self.assertEqual(saved["metadata"]["tags"], [])
        self.assertEqual(saved["metadata"]["studio"], "Manual studio")
        self.assertEqual(saved["metadata"]["date_released"], "2026-02-03")
        self.assertEqual(saved["metadata"]["metadata_fields"]["performers"]["source"], ["Source actor"])


if __name__ == "__main__":
    unittest.main()
