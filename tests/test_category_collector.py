import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "sync"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from category_collector import (
    CategoryCollectorServices,
    _CATEGORY_SKIP,
    register_category_skip,
    fetch_provider_categories,
    fetch_provider_category_videos,
    collect_provider_categories,
)


class TestCategoryCollector(unittest.TestCase):
    def setUp(self):
        self._orig_skips = set(_CATEGORY_SKIP)
        _CATEGORY_SKIP.clear()

    def tearDown(self):
        _CATEGORY_SKIP.clear()
        _CATEGORY_SKIP.update(self._orig_skips)

    def test_register_category_skip(self):
        register_category_skip("testprov", "latest")
        self.assertIn(("testprov", "latest"), _CATEGORY_SKIP)

        # Blank provider or category ignored
        register_category_skip("", "latest")
        register_category_skip("testprov", "")
        self.assertEqual(len(_CATEGORY_SKIP), 1)

    def test_fetch_provider_categories(self):
        fetch_mock = MagicMock(return_value=[{"id": "cat1", "title": "Cat 1"}])
        res = fetch_provider_categories("testprov", "http://resolver:8090", fetch_mock)
        self.assertEqual(res, [{"id": "cat1", "title": "Cat 1"}])
        fetch_mock.assert_called_once_with(
            "http://resolver:8090/admin/api/categories?provider=testprov",
            timeout=20.0,
            desc="categories/testprov",
        )

    def test_fetch_provider_category_videos(self):
        fetch_mock = MagicMock(return_value=[{"id": "vid1"}])
        res = fetch_provider_category_videos("testprov", "cat/with/slash", 2, "http://resolver:8090", fetch_mock)
        self.assertEqual(res, [{"id": "vid1"}])
        fetch_mock.assert_called_once_with(
            "http://resolver:8090/admin/api/category/cat%2Fwith%2Fslash?provider=testprov&page=2",
            timeout=20.0,
            desc="category/testprov/cat/with/slash/p.2",
        )

    def test_collect_provider_categories_empty(self):
        services = MagicMock()
        services.fetch_categories.return_value = []
        collected = {}
        ok = collect_provider_categories(collected, "testprov", 1, services)
        self.assertTrue(ok)
        self.assertEqual(collected, {})

    def test_collect_provider_categories_success(self):
        skips = {("testprov", "skipped_cat")}
        services = CategoryCollectorServices(
            fetch_categories=MagicMock(return_value=[
                {"id": "skipped_cat", "title": "Skipped"},
                {"id": "cat1", "title": "Action"},
            ]),
            fetch_category_videos=MagicMock(return_value=[
                {
                    "provider": "testprov",
                    "id": "testprov:101",
                    "title": "Video 1",
                    "categories": ["Raw Action"],
                }
            ]),
            get_provider_info=MagicMock(return_value={"name": "TestProv"}),
            format_category_name=lambda c: c.capitalize(),
            map_category=lambda c: c.lower(),
            merge_video_fields=MagicMock(),
            category_skips=skips,
            logger=logging.getLogger("test"),
            sleep_fn=MagicMock(),
        )

        collected = {}
        ok = collect_provider_categories(collected, "testprov", 1, services)
        self.assertTrue(ok)
        self.assertIn("testprov:101", collected)
        item = collected["testprov:101"]
        self.assertEqual(item["video"]["title"], "Video 1")
        self.assertIn("TestProv", item["tags"])
        self.assertIn("action", item["genres"])
        self.assertIn("raw action", item["genres"])
        services.sleep_fn.assert_called_once_with(1.0)

    def test_collect_provider_categories_handles_fetch_error(self):
        services = CategoryCollectorServices(
            fetch_categories=MagicMock(return_value=[{"id": "cat1", "title": "Cat 1"}]),
            fetch_category_videos=MagicMock(side_effect=RuntimeError("API error")),
            get_provider_info=MagicMock(return_value={"name": "TestProv"}),
            format_category_name=lambda c: c,
            map_category=lambda c: c,
            merge_video_fields=MagicMock(),
            category_skips=set(),
            logger=logging.getLogger("test"),
            sleep_fn=MagicMock(),
        )

        collected = {}
        ok = collect_provider_categories(collected, "testprov", 1, services)
        self.assertFalse(ok)
        self.assertEqual(collected, {})


if __name__ == "__main__":
    unittest.main()
