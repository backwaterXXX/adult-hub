"""Telegram resolution capture: the attribute reaches the message dict and the item."""

import os
import unittest
from types import SimpleNamespace

from pg_helper import TEST_URL

if TEST_URL:
    os.environ.setdefault("DATABASE_URL", TEST_URL)

from resolver.providers.telegram_messages import (
    _dimension_of,
    message_object_to_dict,
)
from resolver.providers.telegram_item_mapping import message_to_video_impl


class _VideoAttribute:
    """Minimal stand-in for Telethon's DocumentAttributeVideo."""

    def __init__(self, width, height, duration=1800):
        self.w = width
        self.h = height
        self.duration = duration


class _Document:
    def __init__(self, attributes, size=2_700_000_000, mime_type="video/mp4", doc_id=555):
        self.attributes = attributes
        self.size = size
        self.mime_type = mime_type
        self.id = doc_id


def _message(document=None, caption="Scene title", file_size=None):
    return SimpleNamespace(
        id=42,
        message=caption,
        media=SimpleNamespace(document=document) if document is not None else None,
        file=SimpleNamespace(size=file_size) if file_size is not None else None,
    )


class TestTelegramResolutionCapture(unittest.TestCase):
    def test_message_dict_carries_the_reported_resolution(self):
        document = _Document([_VideoAttribute(3840, 2160)])
        payload = message_object_to_dict(_message(document), "-1001234567890")
        self.assertEqual(payload["width"], 3840)
        self.assertEqual(payload["height"], 2160)
        # The nested media dict keeps the same facts for the mapping helpers.
        self.assertEqual(payload["video"]["width"], 3840)
        self.assertEqual(payload["video"]["height"], 2160)

    def test_message_without_video_attribute_reports_nothing(self):
        payload = message_object_to_dict(_message(_Document([])), "-1001234567890")
        self.assertIsNone(payload["width"])
        self.assertIsNone(payload["height"])

    def test_dimension_reader_prefers_the_first_positive_value(self):
        self.assertEqual(_dimension_of({"width": 0, "video": {"width": 1920}}, "width"), 1920)
        self.assertEqual(_dimension_of({"height": "2160"}, "height"), 2160)
        self.assertIsNone(_dimension_of({"height": -5}, "height"))
        self.assertIsNone(_dimension_of({}, "height"))

    def test_item_mapping_keeps_the_resolution(self):
        message = {
            "channel_id": "-1001234567890",
            "message_id": 42,
            "caption": "Scene title",
            "duration": 1800,
            "filename": "scene.mp4",
            "file_unique_id": "AQADabc123unique",
            "width": 3840,
            "height": 2160,
        }
        item = message_to_video_impl(
            message,
            "Kanal",
            prepared_known_map={},
            prepared_catalog=None,
            load_known_performers_fn=lambda: {},
            load_alias_map_fn=lambda: {},
            load_performer_catalog_fn=lambda: {},
            prepare_legacy_catalog_fn=lambda catalog: {},
        )
        self.assertIsNotNone(item)
        self.assertEqual((item.width, item.height), (3840, 2160))


if __name__ == "__main__":
    unittest.main()
